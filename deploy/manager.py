"""Stable, stdlib-only pull deployment manager; never imports a release or GPIO.

Settings version 1 and the display database version 1 are deliberately fixed.
The operator provisions OS/browser/GPIO dependencies and updates this manager.
Downloaded releases cannot migrate, reset, or replace persistent configuration.
"""

import argparse
import fcntl
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

LOG = logging.getLogger("paiper.deploy")
SCENES = ("stillness", "calibration")
SHA = re.compile(r"[0-9a-f]{40}")
SETTINGS_KEYS = {
    "version",
    "repository",
    "branch",
    "python",
    "config",
    "state",
    "vendor_lib",
    "scene",
}

# This executes with the candidate interpreter, not the stable manager's Python.
PNG_CHECK = """
import sys
from PIL import Image
palette = {(0,0,0), (255,255,255), (255,255,0),
           (255,0,0), (0,0,255), (0,255,0)}
with Image.open(sys.argv[1]) as image:
    image.load()
    colors = image.getcolors(maxcolors=6)
    if (image.format != 'PNG' or image.mode != 'RGB'
        or image.size != (int(sys.argv[2]), int(sys.argv[3]))
        or not colors or any(color not in palette for _, color in colors)):
        raise ValueError('Invalid offline six-color preview')
"""


class DeploymentError(RuntimeError):
    """An operational or validation failure; no unsafe fallback is permitted."""


class Busy(DeploymentError):
    """An existing lifecycle owner must be allowed to finish."""


def sanitize_diagnostic(value, *, limit=2048):
    """Redact before bounding; child output is untrusted and may contain credentials."""
    text = (
        value.decode("utf-8", errors="replace")
        if isinstance(value, bytes)
        else str(value)
    )
    secret_name = r"[\w-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|credential)[\w-]*"
    for name, secret in os.environ.items():
        if secret and re.search(secret_name, name, re.IGNORECASE):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = "".join(char for char in text if char.isprintable() or char in "\n\r\t")
    # Whole URLs avoid exposing userinfo, query credentials or signed path components.
    text = re.sub(
        r"""\b[a-z][a-z0-9+.-]*://[^\s<>"']+""",
        "[REDACTED URL]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"""(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)["']?\s*[:=][^\r\n]*""",
        "[REDACTED HEADER]",
        text,
    )
    text = re.sub(
        rf"""\b{secret_name}["']?\s*[:=]\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,;}}\]\r\n]+)""",
        "[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+", "[REDACTED]", text)
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
        r"glpat-[\w-]+|xox[baprs]-[\w-]+|sk-[\w-]+|"
        r"(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b",
        "[REDACTED]",
        text,
    )
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    marker = " ... [truncated] ... "
    head = (limit - len(marker)) // 2
    return text[:head] + marker + text[-(limit - len(marker) - head) :]


def command_failure(label, error):
    """Shared installer/manager failure summary; never stringify a subprocess argv."""
    if isinstance(error, subprocess.TimeoutExpired):
        summary = f"{label} timed out"
    elif isinstance(error, subprocess.CalledProcessError):
        summary = f"{label} failed (exit {error.returncode})"
    else:
        return sanitize_diagnostic(f"Cannot execute {label}: {error}")
    for stream in ("stderr", "stdout"):
        output = getattr(error, stream, None)
        if output:
            summary += f"; {stream}: {sanitize_diagnostic(output, limit=850)}"
    return sanitize_diagnostic(summary)


def valid_sha(value):
    if not isinstance(value, str) or SHA.fullmatch(value) is None:
        raise DeploymentError("Invalid release commit SHA")
    return value


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Not a regular file")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    except (OSError, ValueError) as error:
        raise DeploymentError(f"Invalid {path.name}") from error


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    staging = path.with_name(f".{path.name}.{uuid4().hex}")
    try:
        with staging.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
        sync_directory(path.parent)
    finally:
        staging.unlink(missing_ok=True)


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Busy(f"Locked: {path.name}") from error
        yield
    finally:
        os.close(fd)


def extract_archive(archive, destination):
    """Extract data ourselves: tar links, traversal and special files are errors."""
    try:
        with tarfile.open(archive, "r:") as stream:
            members = stream.getmembers()
            for member in members:
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or "\\" in member.name
                    or not name.parts
                    or not (member.isdir() or member.isreg())
                ):
                    raise DeploymentError("Unsafe Git archive member")
                target = destination.joinpath(*name.parts)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise DeploymentError("Git archive escapes release")
            for member in members:
                target = destination.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(member) as source, target.open("xb") as out:
                        shutil.copyfileobj(source, out)
                    target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, OSError) as error:
        raise DeploymentError("Cannot extract Git archive") from error


def validate_state(path):
    """Independently enforce the existing safety schema, without writing it."""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise DeploymentError("Existing display state is empty or missing")
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        ) as db:
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise DeploymentError("Unsupported display state version (expected 1)")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DeploymentError("Display state failed integrity check")
            rows = db.execute("SELECT * FROM display_state").fetchall()
            expected = {
                "id",
                "last_attempt_at",
                "last_attempt_digest",
                "last_success_at",
                "last_success_digest",
                "cooldown_at",
                "clock_at",
                "in_progress",
            }
            if len(rows) != 1 or set(rows[0].keys()) != expected:
                raise DeploymentError("Invalid display state schema")
            row = dict(rows[0])
            if row["id"] != 1 or row["in_progress"] not in (0, 1):
                raise DeploymentError("Invalid display state singleton")
            for key in (
                "last_attempt_at",
                "last_success_at",
                "cooldown_at",
                "clock_at",
            ):
                value = row[key]
                if value is not None and (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise DeploymentError("Invalid display timestamp")
            for prefix in ("last_attempt", "last_success"):
                at, digest = row[prefix + "_at"], row[prefix + "_digest"]
                if (at is None) != (digest is None) or (
                    digest is not None and (not isinstance(digest, str) or not digest)
                ):
                    raise DeploymentError("Invalid display attempt metadata")
            attempt, cooldown, clock = (
                row["last_attempt_at"],
                row["cooldown_at"],
                row["clock_at"],
            )
            success = row["last_success_at"]
            if attempt is None:
                if (
                    any(v is not None for v in (cooldown, clock, success))
                    or row["in_progress"]
                ):
                    raise DeploymentError("Missing display attempt metadata")
            elif cooldown is None or clock is None or not attempt <= cooldown <= clock:
                raise DeploymentError("Invalid display cooldown ordering")
            if success is not None and success > cooldown:
                raise DeploymentError("Invalid display success timestamp")
            if row["in_progress"]:
                raise DeploymentError(
                    "Interrupted hardware attempt; operator inspection required"
                )
    except sqlite3.Error as error:
        raise DeploymentError("Cannot trust display state") from error


class Manager:
    """Filesystem coordinator with an injectable runner.

    The standalone CLI enforces non-root execution before constructing this class.
    Direct callers must enforce the same policy; the API stays testable in isolation.
    """

    def __init__(self, root, *, runner=subprocess.run):
        self.root = Path(root).expanduser().resolve()
        self.runner = runner
        self.settings = read_json(self.root / "settings.json")
        self._validate_settings()
        self.releases = self.root / "releases"
        self._directory(self.releases)
        self.work = self.root / "work"
        self._directory(self.work)

    def _validate_settings(self):
        s = self.settings
        if not isinstance(s, dict) or set(s) != SETTINGS_KEYS:
            raise DeploymentError("Invalid settings.json keys")
        if type(s["version"]) is not int or s["version"] != 1:
            raise DeploymentError("Unsupported settings.json version")
        repository = s["repository"]
        if not isinstance(repository, str) or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*",
            repository,
        ):
            raise DeploymentError(
                "Repository must be a credential-free HTTPS GitHub URL"
            )
        branch = s["branch"]
        if (
            not isinstance(branch, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", branch)
            or ".." in branch
            or any(
                not part or part.startswith(".") or part.endswith((".", ".lock"))
                for part in branch.split("/")
            )
        ):
            raise DeploymentError("Invalid deploy branch ref")
        if not isinstance(s["scene"], str) or s["scene"] not in SCENES:
            raise DeploymentError("Unknown offline scene")
        for key in ("python", "config", "state", "vendor_lib"):
            value = s[key]
            if key == "vendor_lib" and value is None:
                continue
            if (
                not isinstance(value, str)
                or "\0" in value
                or not Path(value).is_absolute()
            ):
                raise DeploymentError(f"Settings {key} must be an absolute path")
            if Path(value).resolve().is_relative_to(self.root / "releases"):
                raise DeploymentError(f"Settings {key} must be outside releases")

    @staticmethod
    def _directory(path):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise DeploymentError(f"Unsafe managed directory: {path.name}")
        path.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _scratch(self):
        path = self.work / uuid4().hex
        path.mkdir()
        try:
            yield path
        finally:
            shutil.rmtree(path)

    def _environment(self, release=None, *, display=False):
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "GIT_")) and key != "VIRTUAL_ENV"
        }
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "PYTHONNOUSERSITE": "1",
                "TMPDIR": str(self.work),
                "TMP": str(self.work),
                "TEMP": str(self.work),
            }
        )
        if release:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(release / "browsers")
        if display and self.settings["vendor_lib"]:
            env["PYTHONPATH"] = self.settings["vendor_lib"]
        return env

    def _execute(self, argv, *, timeout, release=None, display=False, label):
        try:
            return self.runner(
                [str(arg) for arg in argv],
                check=True,
                capture_output=not display,
                timeout=timeout,
                cwd=str(release / "app" if release else self.root),
                env=self._environment(release, display=display),
            )
        except (
            subprocess.TimeoutExpired,
            subprocess.CalledProcessError,
            OSError,
        ) as error:
            raise DeploymentError(command_failure(label, error)) from error

    def _release(self, sha, *, ready=True):
        path = self.releases / valid_sha(sha)
        if path.is_symlink() or path.resolve() != path:
            raise DeploymentError("Release directory must not be a symlink")
        if ready:
            marker = read_json(path / "ready.json")
            if (
                marker != {"version": 1, "sha": sha}
                or type(marker["version"]) is not int
            ):
                raise DeploymentError("Invalid release readiness marker")
            for name in ("app", ".venv", "browsers"):
                if (path / name).is_symlink() or not (path / name).is_dir():
                    raise DeploymentError("Incomplete ready release")
            if not (path / ".venv/bin/python").is_file():
                raise DeploymentError("Missing release interpreter")
        return path

    def _pointer(self, name):
        pointer = self.root / name
        if not pointer.is_symlink():
            if pointer.exists():
                raise DeploymentError(f"Invalid {name} pointer")
            return None
        target = os.readlink(pointer)
        sha = Path(target).name
        path = self._release(sha)
        if target not in (str(path), f"releases/{sha}"):
            raise DeploymentError(f"Invalid {name} pointer target")
        return sha

    def _set_pointer(self, name, sha):
        path = self.root / name
        if sha is None:
            path.unlink(missing_ok=True)
        else:
            self._release(sha)
            staging = self.root / f".{name}.{uuid4().hex}"
            try:
                staging.symlink_to(f"releases/{sha}", target_is_directory=True)
                os.replace(staging, path)
            finally:
                staging.unlink(missing_ok=True)
        sync_directory(self.root)

    def _failed(self):
        path = self.root / "failed.json"
        if not path.exists() and not path.is_symlink():
            return {}
        data = read_json(path)
        if not isinstance(data, dict):
            raise DeploymentError("Invalid failed.json")
        for sha, record in data.items():
            valid_sha(sha)
            if (
                not isinstance(record, dict)
                or set(record) != {"timestamp", "reason"}
                or not isinstance(record["timestamp"], str)
                or not isinstance(record["reason"], str)
            ):
                raise DeploymentError("Invalid failure record")
            try:
                if datetime.fromisoformat(record["timestamp"]).tzinfo is None:
                    raise ValueError("Missing timezone")
            except ValueError as error:
                raise DeploymentError("Invalid failure timestamp") from error
        return data

    def _record_failure(self, sha, reason):
        records = self._failed()
        records[valid_sha(sha)] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "reason": reason,
        }
        write_json(self.root / "failed.json", records)

    def _clear_failure(self, sha):
        records = self._failed()
        if sha in records:
            del records[sha]
            write_json(self.root / "failed.json", records)

    def status(self):
        return {
            "root": str(self.root),
            "current": self._pointer("current"),
            "previous": self._pointer("previous"),
            "failed": self._failed(),
            "activation_pending": self._journal_pending(),
        }

    def _journal_pending(self):
        return os.path.lexists(self.root / "activation.json")

    def _fetch(self):
        mirror = self.root / "repository.git"
        if mirror.is_symlink():
            raise DeploymentError("Git mirror must not be a symlink")
        if not mirror.exists():
            self._execute(
                ["git", "init", "--bare", mirror], timeout=60, label="Git mirror init"
            )
        self._directory(mirror)
        base = ["git", "--git-dir", str(mirror)]
        self._execute(
            [
                *base,
                "fetch",
                "--no-tags",
                "--force",
                self.settings["repository"],
                f"+refs/heads/{self.settings['branch']}:refs/heads/candidate",
            ],
            timeout=120,
            label="Git fetch",
        )
        result = self._execute(
            [*base, "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
            timeout=30,
            label="Git commit resolution",
        )
        try:
            return valid_sha(result.stdout.decode("ascii").strip())
        except (AttributeError, UnicodeError) as error:
            raise DeploymentError("Invalid fetched commit SHA") from error

    def _prepare(self, sha):
        release = self._release(sha, ready=False)
        if (release / "ready.json").exists():
            return self._release(sha)
        if release.exists():
            if sha in (self._pointer("current"), self._pointer("previous")):
                raise DeploymentError("Cannot rebuild a referenced release")
            try:
                marker = read_json(release / "owner.json")
                owned = (
                    marker == {"version": 1, "sha": sha}
                    and type(marker["version"]) is int
                )
            except DeploymentError:
                owned = False
            if not owned:
                raise DeploymentError("Incomplete release is not owned by this manager")
            shutil.rmtree(release)
        release.mkdir()
        write_json(release / "owner.json", {"version": 1, "sha": sha})
        app = release / "app"
        app.mkdir()
        (release / "browsers").mkdir()
        with self._scratch() as scratch:
            archive = scratch / "source.tar"
            self._execute(
                [
                    "git",
                    "--git-dir",
                    self.root / "repository.git",
                    "archive",
                    "--format=tar",
                    f"--output={archive}",
                    sha,
                ],
                timeout=120,
                label="Git archive",
            )
            extract_archive(archive, app)
        if not (app / "requirements.lock").is_file():
            raise DeploymentError("Release is missing requirements.lock")
        self._execute(
            [
                self.settings["python"],
                "-m",
                "venv",
                "--system-site-packages",
                release / ".venv",
            ],
            timeout=180,
            release=release,
            label="Release venv creation",
        )
        python = release / ".venv/bin/python"
        self._execute(
            [
                python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--constraint",
                app / "requirements.lock",
                app,
            ],
            timeout=1800,
            release=release,
            label="Constrained app installation",
        )
        self._execute(
            [python, "-m", "playwright", "install", "chromium"],
            timeout=1800,
            release=release,
            label="Release Chromium installation",
        )
        self._health(release)
        write_json(release / "ready.json", {"version": 1, "sha": sha})
        return release

    def _health(self, release, snapshot=None, *, previews=True):
        """Exercise candidate CLI against scratch data only, never live state."""
        python = release / ".venv/bin/python"
        with self._scratch() as scratch:
            state = scratch / "display.sqlite3"
            if snapshot is not None:
                shutil.copyfile(snapshot, state)
            before = state.read_bytes() if state.exists() else None
            self._execute(
                [python, "-m", "paiper", "status", "--state", state],
                timeout=60,
                release=release,
                label="Candidate state compatibility",
            )
            validate_state(state)
            if before is not None and state.read_bytes() != before:
                raise DeploymentError(
                    "Candidate modified the state snapshot; migrations forbidden"
                )
            if previews:
                for scene in SCENES:
                    for native in (False, True):
                        output = scratch / f"{scene}-{native}.png"
                        self._execute(
                            [
                                python,
                                "-m",
                                "paiper",
                                "preview",
                                "--scene",
                                scene,
                                "--config",
                                self.settings["config"],
                                "--output",
                                output,
                                *(["--native"] if native else []),
                            ],
                            timeout=120,
                            release=release,
                            label="Offline preview smoke",
                        )
                        size = (400, 600) if native else (600, 400)
                        self._execute(
                            [python, "-c", PNG_CHECK, output, *map(str, size)],
                            timeout=30,
                            release=release,
                            label="Offline PNG validation",
                        )

    def _snapshot(self, destination):
        state = Path(self.settings["state"]).resolve()
        if not state.exists():
            return None
        validate_state(state)
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            if time.monotonic() > deadline:
                raise DeploymentError("Display state backup timed out")

        try:
            with (
                closing(
                    sqlite3.connect(state.as_uri() + "?mode=ro", uri=True, timeout=0)
                ) as source,
                closing(sqlite3.connect(destination)) as target,
            ):
                source.backup(target, pages=128, progress=progress, sleep=0.05)
        except sqlite3.Error as error:
            raise DeploymentError("Cannot back up display state") from error
        validate_state(destination)
        return destination

    @contextmanager
    def _activation_locks(self):
        # Match DisplayState's resolved-path sibling lock, including symlinked DBs.
        state = Path(self.settings["state"]).resolve()
        with (
            lock(self.root / "lifecycle.lock"),
            lock(state.with_name(state.name + ".lock")),
        ):
            yield

    def _recover(self):
        """A journal makes a killed two-pointer switch fail back before any run."""
        journal = self.root / "activation.json"
        if not journal.exists() and not journal.is_symlink():
            return
        data = read_json(journal)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "current", "previous", "candidate"}
            or type(data["version"]) is not int
            or data["version"] != 1
        ):
            raise DeploymentError("Invalid activation journal")
        valid_sha(data["candidate"])
        for name in ("current", "previous"):
            if data[name] is not None:
                self._release(data[name])
        self._set_pointer("current", data["current"])
        self._set_pointer("previous", data["previous"])
        self._record_failure(
            data["candidate"], "Interrupted activation restored prior pointers"
        )
        journal.unlink()
        sync_directory(self.root)
        LOG.warning("Recovered interrupted activation")

    def _activate(self, sha, *, rollback=False):
        with self._activation_locks():
            self._recover()
            release = self._release(sha)
            current, previous = self._pointer("current"), self._pointer("previous")
            with self._scratch() as scratch:
                snapshot = self._snapshot(scratch / "state.sqlite3")
                self._health(release, snapshot, previews=False)
                journal = self.root / "activation.json"
                write_json(
                    journal,
                    {
                        "version": 1,
                        "current": current,
                        "previous": previous,
                        "candidate": sha,
                    },
                )
                try:
                    self._set_pointer("previous", current)
                    self._set_pointer("current", sha)
                    self._health(release, snapshot)
                    self._clear_failure(sha)
                    if rollback and current is not None:
                        self._record_failure(
                            current, "Operator rolled back this release"
                        )
                    journal.unlink()
                    sync_directory(self.root)
                except BaseException:
                    self._recover()
                    raise
        return "activated"

    def update(self, *, retry=False):
        try:
            with lock(self.root / "update.lock"):
                # Recover before unchanged/suppression checks, but only when needed.
                if self._journal_pending():
                    with self._activation_locks():
                        self._recover()
                current = self._pointer("current")
                self._pointer("previous")
                failed = self._failed()
                sha = self._fetch()  # Never attribute a fetch failure to a guessed SHA.
                if sha == current:
                    return self._skip("unchanged")
                if sha in failed and not retry:
                    return self._skip("known-failed")
                try:
                    self._prepare(sha)
                    return self._activate(sha)
                except Busy:
                    raise
                except (DeploymentError, OSError) as error:
                    self._record_failure(sha, sanitize_diagnostic(error))
                    raise
        except Busy:
            return self._skip("busy")

    def rollback(self):
        try:
            with lock(self.root / "update.lock"):
                if self._journal_pending():
                    with self._activation_locks():
                        self._recover()
                previous = self._pointer("previous")
                if previous is None or previous == self._pointer("current"):
                    raise DeploymentError(
                        "No distinct previous release to roll back to"
                    )
                try:
                    return self._activate(previous, rollback=True)
                except Busy:
                    raise
                except (DeploymentError, OSError) as error:
                    self._record_failure(previous, sanitize_diagnostic(error))
                    raise
        except Busy:
            return self._skip("busy")

    def run(self):
        try:
            with lock(self.root / "lifecycle.lock"):
                if self._journal_pending():
                    state = Path(self.settings["state"]).resolve()
                    with lock(state.with_name(state.name + ".lock")):
                        self._recover()
                sha = self._pointer("current")
                if sha is None:
                    raise DeploymentError("No current release")
                release = self._release(sha)
                # The app acquires the inner state lock; holding it here deadlocks it.
                self._execute(
                    [
                        release / ".venv/bin/python",
                        "-m",
                        "paiper",
                        "display",
                        "--scene",
                        self.settings["scene"],
                        "--config",
                        self.settings["config"],
                        "--state",
                        self.settings["state"],
                    ],
                    timeout=None,
                    release=release,
                    display=True,
                    label="Managed display",
                )
                return "completed"
        except Busy:
            return self._skip("busy")

    @staticmethod
    def _skip(reason):
        LOG.info("Deployment skip: %s", reason)
        return reason


def main(argv=None):
    if os.geteuid() == 0:
        print(
            "Deployment error: run as a non-root user; do not use sudo.",
            file=sys.stderr,
        )
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/share/paiper"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser(
        "update", help="Prepare and activate the deploy commit"
    )
    update.add_argument("--retry", action="store_true", help="Retry a rejected commit")
    commands.add_parser("run", help="Run the active release's protected display")
    commands.add_parser("status", help="Report deployment metadata without hardware")
    commands.add_parser("rollback", help="Restore the healthy previous release")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        manager = Manager(args.root)
        if args.command == "status":
            print(json.dumps(manager.status(), indent=2))
        elif args.command == "update":
            manager.update(retry=args.retry)
        else:
            getattr(manager, args.command)()
        return 0
    except (DeploymentError, OSError, sqlite3.Error) as error:
        print(f"Deployment error: {sanitize_diagnostic(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
