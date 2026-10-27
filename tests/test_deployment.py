"""Deployment tests use real Git, SQLite, links and flock; never GPIO."""

import fcntl
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from PIL import Image

from paiper.state import DisplayState

MANAGER = Path(__file__).resolve().parents[1] / "deploy" / "manager.py"

FAILURE_CASES = [
    ("dns", "Could not resolve host"),
    ("disk", "No space left on device"),
    ("dependency", "No matching distribution found"),
    ("timeout", "timed out"),
    ("oserror", "Permission denied"),
]


def diagnostic_failure(kind, argv):
    if kind == "timeout":
        return subprocess.TimeoutExpired(
            argv, 60, output=b"Downloading Chromium", stderr=b"Connection stalled"
        )
    if kind == "oserror":
        return PermissionError(13, "Permission denied", "/offline/browser")
    message = dict(FAILURE_CASES)[kind]
    return subprocess.CalledProcessError(
        1,
        argv,
        output=message.encode() if kind == "dependency" else b"",
        stderr=b"" if kind == "dependency" else message.encode(),
    )


def sensitive_diagnostics():
    secrets = [
        "url-user:UrlPassword",
        "QuerySecret",
        "BasicSecret",
        "BearerSecret",
        "CookieSecret",
        "HeaderSecret",
        "Password With Spaces",
        "ClientSecret",
        "ApiSecret",
        "AccessSecret",
        "ghp_" + "a" * 36,
        "github_pat_" + "b" * 60,
        "glpat-" + "c" * 20,
        "xoxb-" + "d" * 30,
        "AKIA" + "E" * 16,
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature",
    ]
    text = (
        f"Could not resolve host https://{secrets[0]}@example.invalid/path\n"
        f"https://example.invalid/?token={secrets[1]}\n"
        f"Authorization: Basic {secrets[2]}\n"
        f"Proxy-Authorization: Bearer {secrets[3]}\n"
        f"Cookie: session={secrets[4]}\n"
        f"X-Api-Key: {secrets[5]}\n"
        f'password="{secrets[6]}" client_secret={secrets[7]}\n'
        f'{{"api_key": "{secrets[8]}", "access_token": "{secrets[9]}"}}\n'
        + "\n".join(secrets[10:])
    )
    return text, secrets


@pytest.fixture
def module():
    assert MANAGER.is_file(), "Task 1 stable deployment manager is missing"
    spec = importlib.util.spec_from_file_location("deployment_manager", MANAGER)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout.strip()


class LocalRunner:
    """Only substitute the remote and expensive builds/browser render calls."""

    def __init__(self, repo, root):
        self.repo, self.root = repo, root
        self.calls = []
        self.fail = None
        self.fail_after_switch = False
        self.fail_fetch = False
        self.on_display = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "git":
            if "fetch" in argv:
                if self.fail_fetch:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                argv = [
                    str(self.repo) if x.startswith("https://github.com/") else x
                    for x in argv
                ]
            return subprocess.run(argv, check=kwargs.pop("check"), **kwargs)
        if "venv" in argv:
            target = Path(argv[-1])
            (target / "bin").mkdir(parents=True)
            (target / "bin" / "python").symlink_to(sys.executable)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "pip" in argv or "playwright" in argv:
            if self.fail == "install":
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "display" in argv:
            if self.on_display:
                self.on_display(argv, kwargs)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "preview" in argv:
            active = (self.root / "current").resolve().name
            candidate = Path(argv[0]).parents[2].name
            if self.fail == "smoke" or (self.fail_after_switch and active == candidate):
                raise subprocess.CalledProcessError(1, argv)
            output = Path(argv[argv.index("--output") + 1])
            size = (400, 600) if "--native" in argv else (600, 400)
            Image.new("RGB", size, "white").save(output)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        # Real candidate status and real independent PNG validation.
        return subprocess.run(
            [sys.executable, *argv[1:]], check=kwargs.pop("check"), **kwargs
        )


@pytest.fixture
def prepared(tmp_path, module):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "deploy")
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "user.name", "Deployment tests")
    (repo / "requirements.lock").write_text("Pillow==12.3.0\n")
    (repo / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    root = tmp_path / "runtime"
    root.mkdir()
    state = tmp_path / "state.sqlite3"
    with DisplayState(state) as display:
        display.reserve(1000, "existing")
        display.begin_attempt()
        display.succeed(1040, "existing")
    config = tmp_path / "config.yaml"
    config.write_text("panel:\n  confirmed: false\n")
    settings = {
        "version": 1,
        "repository": "https://github.com/disdev/paiper.git",
        "branch": "deploy",
        "python": sys.executable,
        "config": str(config),
        "state": str(state),
        "vendor_lib": None,
        "scene": "stillness",
    }
    (root / "settings.json").write_text(json.dumps(settings))
    runner = LocalRunner(repo, root)
    manager = module.Manager(root, runner=runner)
    return manager, runner, first, settings


def advance(runner):
    (runner.repo / "new.txt").write_text("next release\n")
    git(runner.repo, "add", ".")
    git(runner.repo, "commit", "-m", "next")
    return git(runner.repo, "rev-parse", "HEAD")


def test_successful_activation_preserves_cooldown_database(prepared):
    manager, runner, first, settings = prepared
    original = Path(settings["state"]).read_bytes()
    config = Path(settings["config"]).read_bytes()
    assert manager.update() == "activated"
    assert manager.status()["current"] == first
    second = advance(runner)
    assert manager.update() == "activated"
    assert manager.status()["current"] == second
    assert manager.status()["previous"] == first
    assert Path(settings["state"]).read_bytes() == original
    assert Path(settings["config"]).read_bytes() == config
    assert manager.update() == "unchanged"


@pytest.mark.parametrize(("kind", "expected"), FAILURE_CASES)
def test_command_failure_preserves_useful_diagnostics(prepared, module, kind, expected):
    manager, _, _, _ = prepared

    def fail(argv, **kwargs):
        raise diagnostic_failure(kind, argv)

    manager.runner = fail
    with pytest.raises(module.DeploymentError) as caught:
        manager._execute(["unused", "NeverEchoArgvSecret"], timeout=60, label="Health")
    message = str(caught.value)
    assert "Health" in message
    assert expected in message
    assert "NeverEchoArgvSecret" not in message
    if kind == "timeout":
        assert "Downloading Chromium" in message
        assert "Connection stalled" in message
    if kind not in ("timeout", "oserror"):
        assert "exit 1" in message


@pytest.mark.parametrize("stage", ["pip", "preview"])
def test_failed_candidate_diagnostics_are_safe_persisted_and_visible(
    prepared, module, monkeypatch, capsys, stage
):
    manager, runner, first, _ = prepared
    text, secrets = sensitive_diagnostics()
    monkeypatch.setenv("PRIVATE_PASSWORD", "EnvironmentSecret")
    text += "\nEnvironmentSecret\n" + "x" * 4000 + " password=BoundarySecret"

    def fail(argv, **kwargs):
        if stage in argv:
            raise subprocess.CalledProcessError(
                1,
                [*argv, "NeverEchoArgvSecret"],
                output=b"dependency installation context",
                stderr=text.encode() + b"\xff\x1b[31m",
            )
        return runner(argv, **kwargs)

    manager.runner = fail
    monkeypatch.setattr(module, "Manager", lambda root: manager)
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    assert module.main(["--root", str(manager.root), "update"]) == 1
    stderr = capsys.readouterr().err
    reason = manager.status()["failed"][first]["reason"]
    assert "Could not resolve host" in reason
    assert "dependency installation context" in reason
    assert "truncated" in reason
    assert len(reason) <= 2048
    assert reason in stderr
    assert manager.status()["current"] is None
    assert manager.update() == "known-failed"
    assert module.main(["--root", str(manager.root), "status"]) == 0
    status_output = capsys.readouterr().out
    for output in (stderr, status_output, (manager.root / "failed.json").read_text()):
        for secret in [
            *secrets,
            "EnvironmentSecret",
            "BoundarySecret",
            "NeverEchoArgvSecret",
        ]:
            assert secret not in output
        assert "\x1b" not in output


def test_redaction_precedes_output_truncation(prepared, module):
    manager, _, _, _ = prepared
    # A secret spanning an output boundary must not leave a visible prefix.
    secret = "BoundarySecret" + "s" * 5000

    def fail(argv, **kwargs):
        raise subprocess.CalledProcessError(
            1,
            argv,
            stderr=("password=" + secret + "\nNo space left on device").encode(),
        )

    manager.runner = fail
    with pytest.raises(module.DeploymentError) as caught:
        manager._execute(["unused"], timeout=60, label="Build")
    message = str(caught.value)
    assert "No space left on device" in message
    assert "BoundarySecret" not in message
    assert len(message) <= 2048


def test_long_command_output_keeps_initial_context_and_final_failure(module):
    message = module.command_failure(
        "Dependencies",
        subprocess.CalledProcessError(
            1,
            ["unused"],
            output="Collecting dependencies\n"
            + "." * 5000
            + "\nNo matching distribution found",
            stderr="Checking storage\n" + "." * 5000 + "\nNo space left on device",
        ),
    )
    for useful in (
        "Collecting dependencies",
        "No matching distribution found",
        "Checking storage",
        "No space left on device",
    ):
        assert useful in message
    assert "truncated" in message
    assert len(message) <= 2048


@pytest.mark.parametrize(
    "payload",
    [
        r'password="Quoted\"PasswordTail"',
        r"client_secret='Quoted\'PasswordTail'",
    ],
)
def test_escaped_quoted_secrets_do_not_leak_suffixes(module, payload):
    message = module.command_failure(
        "Build",
        subprocess.CalledProcessError(
            1, ["unused"], stderr=payload + "\nNo matching distribution found"
        ),
    )
    assert "Quoted" not in message
    assert "PasswordTail" not in message
    assert "No matching distribution found" in message


@pytest.mark.parametrize("line", sensitive_diagnostics()[0].splitlines())
def test_each_secret_is_redacted_without_relying_on_truncation(module, line):
    _, secrets = sensitive_diagnostics()
    message = module.command_failure(
        "Build",
        subprocess.CalledProcessError(
            1, ["unused"], output=line + "\nNo space left on device"
        ),
    )
    assert "truncated" not in message
    assert "No space left on device" in message
    for secret in secrets:
        assert secret not in message


def test_display_child_output_remains_uncaptured(prepared, module, capfd):
    manager, _, _, _ = prepared
    manager.runner = subprocess.run
    with pytest.raises(module.DeploymentError) as caught:
        manager._execute(
            [
                sys.executable,
                "-c",
                (
                    "import sys; print('display output'); "
                    "print('display stderr', file=sys.stderr); sys.exit(2)"
                ),
            ],
            timeout=None,
            display=True,
            label="Managed display",
        )
    captured = capfd.readouterr()
    assert captured.out == "display output\n"
    assert captured.err == "display stderr\n"
    assert "exit 2" in str(caught.value)
    assert "display stderr" not in str(caught.value)


@pytest.mark.parametrize("failure", ["install", "smoke", "after-switch"])
def test_failed_candidate_keeps_pointers_and_state(prepared, module, failure):
    manager, runner, first, settings = prepared
    manager.update()
    second = advance(runner)
    before = Path(settings["state"]).read_bytes()
    runner.fail = failure
    runner.fail_after_switch = failure == "after-switch"
    with pytest.raises(module.DeploymentError):
        manager.update()
    assert manager.status()["current"] == first
    assert manager.status()["previous"] is None
    assert second in manager.status()["failed"]
    assert Path(settings["state"]).read_bytes() == before
    runner.fail = None
    runner.fail_after_switch = False
    assert manager.update() == "known-failed"
    assert manager.update(retry=True) == "activated"
    assert second not in manager.status()["failed"]


def test_first_install_post_switch_failure_leaves_no_pointer(prepared, module):
    manager, runner, first, _ = prepared
    runner.fail_after_switch = True
    with pytest.raises(module.DeploymentError):
        manager.update()
    assert manager.status()["current"] is None
    assert manager.status()["previous"] is None
    assert first in manager.status()["failed"]


def test_rollback_persists_failed_commit_suppression(prepared, module):
    manager, runner, first, _ = prepared
    with pytest.raises(module.DeploymentError, match="previous"):
        manager.rollback()
    manager.update()
    second = advance(runner)
    manager.update()
    assert manager.rollback() == "activated"
    again = module.Manager(manager.root, runner=runner)
    assert again.status()["current"] == first
    assert again.status()["previous"] == second
    assert second in again.status()["failed"]
    assert again.update() == "known-failed"


@pytest.mark.parametrize("lock", ["update.lock", "lifecycle.lock", "state"])
def test_busy_lock_skips_without_marking_candidate_failed(prepared, lock):
    manager, _, first, settings = prepared
    path = Path(settings["state"] + ".lock") if lock == "state" else manager.root / lock
    with path.open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert manager.update() == "busy"
        assert manager.status()["current"] is None
        assert manager.status()["failed"] == {}
        if lock != "update.lock":
            assert (manager.root / "releases" / first / "ready.json").is_file()
    assert manager.update() == "activated"


def test_managed_run_resolves_release_once_and_holds_lifecycle(prepared, module):
    manager, runner, first, settings = prepared
    manager.update()
    with (manager.root / "lifecycle.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = len(runner.calls)
        assert manager.run() == "busy"
        assert len(runner.calls) == before

    def during_display(argv, kwargs):
        with (
            (manager.root / "lifecycle.lock").open("a+b") as other,
            pytest.raises(BlockingIOError),
        ):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert argv[0] == str(manager.root / "releases" / first / ".venv/bin/python")
        assert argv[-2:] == ["--state", settings["state"]]
        assert kwargs["timeout"] is None  # Never kill an in-progress refresh.
        assert kwargs["capture_output"] is False

    runner.on_display = during_display
    assert manager.run() == "completed"
    (manager.root / "current").unlink()
    with pytest.raises(module.DeploymentError, match="current"):
        manager.run()


def test_build_argv_environment_and_final_venv_location(prepared, monkeypatch):
    manager, runner, first, settings = prepared
    monkeypatch.setenv("PYTHONPATH", "/bad/source")
    monkeypatch.setenv("VIRTUAL_ENV", "/bad/venv")
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    manager.update()
    release = manager.root / "releases" / first
    builds = [(a, k) for a, k in runner.calls if a[0] != "git"]
    assert any(
        a
        == [
            settings["python"],
            "-m",
            "venv",
            "--system-site-packages",
            str(release / ".venv"),
        ]
        for a, _ in builds
    )
    installs = [a for a, _ in builds if "pip" in a]
    assert len(installs) == 1
    assert installs[0][-3:] == [
        "--constraint",
        str(release / "app/requirements.lock"),
        str(release / "app"),
    ]
    assert any(a[-4:] == ["-m", "playwright", "install", "chromium"] for a, _ in builds)
    for argv, kwargs in runner.calls:
        assert isinstance(argv, list)
        assert kwargs.get("shell", False) is False
        assert 0 < kwargs["timeout"] <= 1800
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert "PYTHONPATH" not in kwargs["env"]
        assert "PYTHONHOME" not in kwargs["env"]
        assert "VIRTUAL_ENV" not in kwargs["env"]
        if argv[0] != "git":
            assert kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(
                release / "browsers"
            )
    previews = [a for a, _ in builds if "preview" in a]
    assert {a[a.index("--scene") + 1] for a in previews} == {"stillness", "calibration"}
    assert any("--native" in a for a in previews)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("unknown", 1),
        ("repository", "https://token@github.com/disdev/paiper.git"),
        ("repository", "file:///source"),
        ("repository", "/source"),
        ("repository", "https://github.com/disdev/paiper?token=secret"),
        ("repository", "ssh://git@github.com/disdev/paiper"),
        ("branch", "-bad"),
        ("branch", "refs/heads/../deploy"),
        ("branch", "deploy.lock"),
        ("branch", "bad@{ref"),
        ("python", "python3"),
        ("config", None),
        ("state", "relative"),
        ("vendor_lib", 5),
        ("scene", "live"),
    ],
)
def test_settings_fail_closed(prepared, module, field, value):
    manager, runner, _, settings = prepared
    settings[field] = value
    (manager.root / "settings.json").write_text(json.dumps(settings))
    with pytest.raises(module.DeploymentError):
        module.Manager(manager.root, runner=runner)


@pytest.mark.parametrize("state_kind", ["empty", "garbage", "version", "in-progress"])
def test_invalid_or_interrupted_state_never_activates(prepared, module, state_kind):
    manager, _, _, settings = prepared
    state = Path(settings["state"])
    if state_kind == "empty":
        state.write_bytes(b"")
    elif state_kind == "garbage":
        state.write_bytes(b"not sqlite")
    else:
        with sqlite3.connect(state) as db:
            db.execute(
                "PRAGMA user_version=2"
                if state_kind == "version"
                else "UPDATE display_state SET in_progress=1"
            )
    original = state.read_bytes()
    with pytest.raises(module.DeploymentError):
        manager.update()
    assert manager.status()["current"] is None
    assert state.read_bytes() == original


def test_missing_state_stays_missing_during_install(prepared):
    manager, _, _, settings = prepared
    state = Path(settings["state"])
    state.unlink()
    manager.update()
    assert not state.exists()


def test_fetch_failure_is_not_attributed_to_previous_candidate(prepared, module):
    manager, runner, first, _ = prepared
    manager.update()
    runner.fail_fetch = True
    with pytest.raises(module.DeploymentError, match="fetch"):
        manager.update()
    assert manager.status()["current"] == first
    assert manager.status()["failed"] == {}


@pytest.mark.parametrize("kind", ["outside", "not-ready", "not-link", "release-link"])
def test_invalid_pointers_fail_closed(prepared, module, kind, tmp_path):
    manager, _, first, _ = prepared
    pointer = manager.root / "current"
    release = manager.root / "releases" / first
    release.parent.mkdir(exist_ok=True)
    if kind == "not-link":
        pointer.write_text(first)
    elif kind == "release-link":
        release.symlink_to(tmp_path, target_is_directory=True)
        pointer.symlink_to(release, target_is_directory=True)
    else:
        if kind == "not-ready":
            release.mkdir()
        pointer.symlink_to(tmp_path if kind == "outside" else release)
    with pytest.raises(module.DeploymentError):
        manager.status()


@pytest.mark.parametrize(
    "kind", ["traversal", "absolute", "symlink", "hardlink", "fifo"]
)
def test_archive_rejects_unsafe_members(module, tmp_path, kind):
    archive = tmp_path / "unsafe.tar"
    member = tarfile.TarInfo(
        "../escape"
        if kind == "traversal"
        else "/escape"
        if kind == "absolute"
        else "entry"
    )
    member.type = {
        "symlink": tarfile.SYMTYPE,
        "hardlink": tarfile.LNKTYPE,
        "fifo": tarfile.FIFOTYPE,
    }.get(kind, tarfile.REGTYPE)
    member.linkname = "../escape"
    with tarfile.open(archive, "w") as stream:
        stream.addfile(member, io.BytesIO(b""))
    destination = tmp_path / "app"
    destination.mkdir()
    with pytest.raises(module.DeploymentError):
        module.extract_archive(archive, destination)
    assert not (tmp_path / "escape").exists()


def test_incomplete_owned_release_can_rebuild_but_unowned_cannot(prepared, module):
    manager, _runner, first, _ = prepared
    release = manager.root / "releases" / first
    release.mkdir(parents=True)
    sentinel = release / "operator-file"
    sentinel.write_text("preserve")
    with pytest.raises(module.DeploymentError, match="owned"):
        manager.update()
    assert sentinel.read_text() == "preserve"
    (release / "owner.json").write_text(json.dumps({"version": 1, "sha": first}))
    assert manager.update(retry=True) == "activated"
    assert not sentinel.exists()


@pytest.mark.parametrize("command", ["update", "run", "rollback", "status"])
def test_root_cli_refuses_before_manager_or_subprocess(
    module, monkeypatch, tmp_path, capsys, command
):
    root = tmp_path / "runtime"
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    def forbidden(*args, **kwargs):
        pytest.fail("Root CLI must not construct Manager or launch a subprocess")

    monkeypatch.setattr(module, "Manager", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    assert module.main(["--root", str(root), command]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "non-root" in output.err
    assert not root.exists()


def test_status_cli_is_offline_and_returns_errors(
    prepared, module, capsys, monkeypatch
):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    manager, runner, _, _ = prepared
    before = len(runner.calls)
    assert module.main(["--root", str(manager.root), "status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["root"] == str(manager.root)
    assert output["current"] is None
    assert len(runner.calls) == before
    (manager.root / "failed.json").write_text('{"bad": {"reason": "secret"}}')
    assert module.main(["--root", str(manager.root), "status"]) == 1
    assert "secret" not in capsys.readouterr().err


def test_interrupted_switch_is_recovered_before_display(prepared):
    manager, runner, first, _ = prepared
    manager.update()
    second = advance(runner)
    manager.update()
    (manager.root / "activation.json").write_text(
        json.dumps(
            {
                "version": 1,
                "current": first,
                "previous": None,
                "candidate": second,
            }
        )
    )
    assert manager.run() == "completed"
    assert manager.status()["current"] == first
    assert manager.status()["previous"] is None
    assert second in manager.status()["failed"]
    assert not (manager.root / "activation.json").exists()


@pytest.mark.parametrize("command", ["update", "run", "rollback"])
def test_dangling_activation_journal_is_not_ignored(prepared, module, command):
    manager, _, first, _ = prepared
    manager.update()
    (manager.root / "activation.json").symlink_to("missing-journal")
    with pytest.raises(module.DeploymentError, match="activation"):
        getattr(manager, command)()
    assert manager.status()["current"] == first


def test_ready_version_must_not_accept_boolean(prepared, module):
    manager, _, first, _ = prepared
    manager.update()
    marker = manager.root / "releases" / first / "ready.json"
    marker.write_text(json.dumps({"version": True, "sha": first}))
    with pytest.raises(module.DeploymentError):
        manager.status()


def test_post_switch_failure_restores_both_prior_pointers(prepared, module):
    manager, runner, first, settings = prepared
    manager.update()
    second = advance(runner)
    manager.update()
    (runner.repo / "third.txt").write_text("third\n")
    git(runner.repo, "add", ".")
    git(runner.repo, "commit", "-m", "third")
    third = git(runner.repo, "rev-parse", "HEAD")
    before = Path(settings["state"]).read_bytes()
    runner.fail_after_switch = True
    with pytest.raises(module.DeploymentError):
        manager.update()
    assert manager.status()["current"] == second
    assert manager.status()["previous"] == first
    assert third in manager.status()["failed"]
    assert Path(settings["state"]).read_bytes() == before


def test_failed_manual_rollback_restores_working_release(prepared, module):
    manager, runner, first, _ = prepared
    manager.update()
    second = advance(runner)
    manager.update()
    runner.fail_after_switch = True
    with pytest.raises(module.DeploymentError):
        manager.rollback()
    assert manager.status()["current"] == second
    assert manager.status()["previous"] == first
    assert first in manager.status()["failed"]


def test_rejects_status_migration_on_copy_and_never_touches_real_state(
    prepared, module
):
    manager, runner, first, settings = prepared
    manager.update()
    second = advance(runner)
    before = Path(settings["state"]).read_bytes()

    def migrating_candidate(argv, **kwargs):
        result = runner(argv, **kwargs)
        if "status" in argv:
            path = Path(argv[-1])
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA user_version=2")
        return result

    manager.runner = migrating_candidate
    with pytest.raises(module.DeploymentError, match="version"):
        manager.update()
    assert manager.status()["current"] == first
    assert second in manager.status()["failed"]
    assert Path(settings["state"]).read_bytes() == before


def test_wal_backup_includes_latest_cooldown_and_is_read_only(prepared):
    manager, runner, _, settings = prepared
    state = Path(settings["state"])
    observed = []

    def inspect_snapshot(argv, **kwargs):
        if "status" in argv and Path(argv[-1]).exists():
            with sqlite3.connect(argv[-1]) as snapshot:
                observed.append(
                    snapshot.execute(
                        "SELECT cooldown_at FROM display_state"
                    ).fetchone()[0]
                )
        return runner(argv, **kwargs)

    manager.runner = inspect_snapshot
    connection = sqlite3.connect(state)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("UPDATE display_state SET cooldown_at=2000, clock_at=2000")
        connection.commit()
        original = state.read_bytes()
        wal = Path(str(state) + "-wal").read_bytes()
        manager.update()
        assert observed == [2000, 2000]
        assert state.read_bytes() == original
        assert Path(str(state) + "-wal").read_bytes() == wal
    finally:
        connection.close()


def test_run_uses_only_explicit_vendor_and_candidate_browser(
    prepared, module, monkeypatch
):
    manager, runner, first, settings = prepared
    settings["vendor_lib"] = str(manager.root.parent / "stable-vendor")
    (manager.root / "settings.json").write_text(json.dumps(settings))
    manager = module.Manager(manager.root, runner=runner)
    manager.update()
    monkeypatch.setenv("PYTHONPATH", "/untrusted/checkout")
    manager.run()
    argv, kwargs = runner.calls[-1]
    assert "display" in argv
    assert kwargs["env"]["PYTHONPATH"] == settings["vendor_lib"]
    assert kwargs["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(
        manager.root / "releases" / first / "browsers"
    )


def test_another_process_cannot_display_while_lifecycle_is_locked(prepared):
    manager, _, first, _ = prepared
    manager.update()
    with (manager.root / "lifecycle.lock").open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            [sys.executable, str(MANAGER), "--root", str(manager.root), "run"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert "busy" in result.stderr
    assert manager.status()["current"] == first


@pytest.mark.parametrize("field", ["config", "state", "vendor_lib", "python"])
def test_persistent_paths_cannot_live_in_releases(prepared, module, field):
    manager, runner, first, settings = prepared
    settings[field] = str(manager.root / "releases" / first / field)
    (manager.root / "settings.json").write_text(json.dumps(settings))
    with pytest.raises(module.DeploymentError, match="outside releases"):
        module.Manager(manager.root, runner=runner)


def test_symlinked_state_uses_resolved_sibling_lock(prepared):
    manager, _, _, settings = prepared
    state = Path(settings["state"])
    target = state.with_name("physical.sqlite3")
    state.rename(target)
    state.symlink_to(target)
    with Path(str(target) + ".lock").open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert manager.update() == "busy"
        assert manager.status()["failed"] == {}


def test_nonpalette_png_cannot_be_activated(prepared, module):
    manager, runner, first, _ = prepared

    def bad_png(argv, **kwargs):
        result = runner(argv, **kwargs)
        if "preview" in argv:
            Image.new("RGB", (600, 400), (123, 123, 123)).save(
                argv[argv.index("--output") + 1]
            )
        return result

    manager.runner = bad_png
    with pytest.raises(module.DeploymentError, match="PNG"):
        manager.update()
    assert manager.status()["current"] is None
    assert first in manager.status()["failed"]


@pytest.mark.parametrize("value", [b"../escape\n", b"f" * 39, b"G" * 40, b""])
def test_fetched_sha_is_validated_before_any_release_write(prepared, module, value):
    manager, runner, _, _ = prepared

    def bad_commit(argv, **kwargs):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, value, b"")
        return runner(argv, **kwargs)

    manager.runner = bad_commit
    with pytest.raises(module.DeploymentError, match="SHA"):
        manager.update()
    assert list((manager.root / "releases").iterdir()) == []
    assert manager.status()["failed"] == {}


def test_incomplete_release_symlink_never_deletes_external_files(prepared, module):
    manager, _, first, _ = prepared
    external = manager.root.parent / "external"
    external.mkdir()
    (external / "owner.json").write_text(json.dumps({"version": 1, "sha": first}))
    (manager.root / "releases" / first).symlink_to(external)
    with pytest.raises(module.DeploymentError, match="symlink"):
        manager.update()
    assert (external / "owner.json").is_file()


def test_later_display_failure_never_rolls_back_release(prepared, module):
    manager, runner, first, _ = prepared
    manager.update()
    second = advance(runner)
    manager.update()

    def hardware_failure(argv, kwargs):
        raise subprocess.CalledProcessError(1, argv)

    runner.on_display = hardware_failure
    with pytest.raises(module.DeploymentError, match="Managed display"):
        manager.run()
    assert manager.status()["current"] == second
    assert manager.status()["previous"] == first
    assert manager.status()["failed"] == {}


@pytest.mark.parametrize("contents", ['{"version":1,"version":1}', "[]", "{"])
def test_malformed_settings_are_not_repaired(prepared, module, contents):
    manager, runner, _, _ = prepared
    path = manager.root / "settings.json"
    path.write_text(contents)
    with pytest.raises(module.DeploymentError):
        module.Manager(manager.root, runner=runner)
    assert path.read_text() == contents


def test_candidate_missing_constraints_is_rejected(prepared, module):
    manager, runner, _, _ = prepared
    git(runner.repo, "rm", "requirements.lock")
    git(runner.repo, "commit", "-m", "missing constraints")
    sha = git(runner.repo, "rev-parse", "HEAD")
    with pytest.raises(module.DeploymentError, match="requirements.lock"):
        manager.update()
    assert sha in manager.status()["failed"]
    assert not (manager.root / "releases" / sha / "ready.json").exists()


def test_mirror_symlink_is_rejected_before_running_git(prepared, module):
    manager, runner, _, _ = prepared
    (manager.root / "repository.git").symlink_to(manager.root.parent / "outside")
    with pytest.raises(module.DeploymentError):
        manager.update()
    assert len(runner.calls) == 0
