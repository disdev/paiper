#!/bin/sh
# Download this file first, then run it as the ordinary display user.
set -eu
exec /usr/bin/python3 - "$@" <<'PAIPER_BOOTSTRAP'
import io
import json
import os
import platform
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from uuid import uuid4


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": "paiper-guided-setup"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if not response.url.startswith("https://"):
            raise ValueError("Download must remain HTTPS")
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("Source download exceeds size limit")
    return data


def owned_directory(path):
    if path.is_symlink():
        raise ValueError("Refusing a symlink bootstrap directory")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Bootstrap directories must be owned by you and not group/world writable")


def extract(data, destination):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        roots, seen, total = set(), set(), 0
        for entry in members:
            path = PurePosixPath(entry.name)
            if (not path.parts or path.is_absolute() or ".." in path.parts
                    or "\\" in entry.name or not (entry.isdir() or entry.isreg())
                    or path in seen):
                raise ValueError("Unsafe or duplicate source archive member")
            seen.add(path)
            roots.add(path.parts[0])
            total += entry.size
            if len(roots) != 1 or total > 256 * 1024 * 1024:
                raise ValueError("Invalid source archive root or size")
            relative = Path(*path.parts[1:])
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Source archive escapes owned root")
            if len(path.parts) == 1 and not entry.isdir():
                raise ValueError("Source archive root must be a directory")
        if not members:
            raise ValueError("Empty source archive")
        for entry in members:
            target = destination.joinpath(*PurePosixPath(entry.name).parts[1:])
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(entry) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if entry.mode & 0o111 else 0o644)


def bootstrap(home, args, fetch=fetch_bytes, execute=subprocess.run):
    base = home
    for part in (".local", "share", "paiper-bootstrap"):
        base = base / part
        owned_directory(base)
    metadata = json.loads(fetch("https://api.github.com/repos/disdev/paiper/commits/deploy"))
    sha = metadata.get("sha") if isinstance(metadata, dict) else None
    if not isinstance(sha, str) or re.fullmatch("[0-9a-f]{40}", sha) is None:
        raise ValueError("GitHub did not return an exact deploy commit")
    data = fetch("https://api.github.com/repos/disdev/paiper/tarball/" + sha)
    source = base / ("source-" + uuid4().hex)
    source.mkdir(mode=0o700)
    try:
        extract(data, source)
        for name in ("deploy/setup.py", "deploy/install.py", "deploy/manager.py",
                     "deploy/setup_candidate.py", "requirements.lock"):
            if not (source / name).is_file():
                raise ValueError("Deploy has no complete guided setup; wait for its CI promotion")
    except BaseException:
        shutil.rmtree(source)
        raise
    print("Using exact deploy source " + sha, flush=True)
    print("Resume without re-downloading: /usr/bin/python3 "
          + shlex.quote(str(source / "deploy/setup.py")), flush=True)
    # Keep the exact source for reboot/rerun; it is never an editable pip install.
    return execute(
        ["/usr/bin/python3", str(source / "deploy/setup.py"), *args],
        check=False, cwd=str(home),
    ).returncode


def main():
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("Paiper guided setup: Raspberry Pi 5, 64-bit Raspberry Pi OS 12/13.")
        print("Run as the ordinary user. Options: --config PATH --state ORIGINAL_DB --vendor-lib PATH")
        return 0
    if os.geteuid() == 0:
        raise ValueError("Run as the ordinary display user, not root or sudo")
    if sys.version_info < (3, 11):
        raise ValueError("Distro Python 3.11 or newer is required")
    release = platform.freedesktop_os_release()
    if (platform.machine() != "aarch64" or struct.calcsize("P") != 8
            or not Path("/proc/device-tree/model").read_text().startswith("Raspberry Pi 5 Model B")
            or not Path("/etc/rpi-issue").is_file()
            or release.get("ID") not in ("debian", "raspbian")
            or release.get("VERSION_ID") not in ("12", "13")):
        raise ValueError("Supported: Raspberry Pi 5, 64-bit Raspberry Pi OS Bookworm/Trixie (12/13)")
    return bootstrap(Path.home(), sys.argv[1:])


if __name__ == "__main__":
    try:
        result = main()
    except ValueError as error:
        print("Bootstrap error: " + str(error), file=sys.stderr)
        result = 1
    except (OSError, EOFError, tarfile.TarError, KeyboardInterrupt) as error:
        # No trusted manager is available yet. Never echo network bodies/URLs.
        print("Bootstrap failed (" + type(error).__name__ + "); no partial source is executed. Rerun after correcting the connection/platform.", file=sys.stderr)
        result = 1
    raise SystemExit(result)
PAIPER_BOOTSTRAP
