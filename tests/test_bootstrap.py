"""Execute the shell entrypoint and its embedded downloader with local archives."""

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


@pytest.fixture
def bootstrap():
    script = ROOT / "deploy/bootstrap.sh"
    assert script.is_file(), "Bootstrap shell entrypoint is missing"
    code = (
        script.read_text()
        .split("<<'PAIPER_BOOTSTRAP'\n", 1)[1]
        .rsplit("\nPAIPER_BOOTSTRAP", 1)[0]
    )
    namespace = {"__name__": "bootstrap_test"}
    exec(compile(code, str(script), "exec"), namespace)  # noqa: S102 -- execute our shell's Python payload
    return namespace


def archive(extra=None):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        for path in (
            "deploy/setup.py",
            "deploy/install.py",
            "deploy/manager.py",
            "deploy/setup_candidate.py",
            "requirements.lock",
        ):
            entry = tarfile.TarInfo("disdev-paiper-aaaaaaa/" + path)
            content = b"# fixture source\n"
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
        if extra:
            tar.addfile(extra, io.BytesIO(b"x") if extra.isreg() else None)
    return payload.getvalue()


def test_bootstrap_resolves_once_downloads_exact_sha_and_only_executes_complete_source(
    bootstrap, tmp_path
):
    calls, launches = [], []

    def fetch(url):
        calls.append(url)
        return json.dumps({"sha": SHA}).encode() if len(calls) == 1 else archive()

    def execute(argv, **kwargs):
        launches.append(argv)
        script = Path(argv[1])
        assert script.is_file()
        assert script.read_bytes() == b"# fixture source\n"
        assert script.is_relative_to(tmp_path)
        return subprocess.CompletedProcess(argv, 2)

    assert (
        bootstrap["bootstrap"](tmp_path, ["--state", "original.db"], fetch, execute)
        == 2
    )
    assert calls == [
        "https://api.github.com/repos/disdev/paiper/commits/deploy",
        f"https://api.github.com/repos/disdev/paiper/tarball/{SHA}",
    ]
    assert len(launches) == 1
    assert launches[0][0] == "/usr/bin/python3"
    assert launches[0][-2:] == ["--state", "original.db"]


@pytest.mark.parametrize(
    "kind",
    ["traversal", "absolute", "symlink", "hardlink", "fifo", "other-root", "duplicate"],
)
def test_unsafe_tar_never_launches_or_escapes_owned_root(bootstrap, tmp_path, kind):
    entry = tarfile.TarInfo(
        {
            "traversal": "disdev-paiper-aaaaaaa/../../escape",
            "absolute": "/escape",
            "other-root": "other/escape",
            "duplicate": "disdev-paiper-aaaaaaa/deploy/setup.py",
        }.get(kind, "disdev-paiper-aaaaaaa/link")
    )
    if kind in ("symlink", "hardlink", "fifo"):
        entry.type = {
            "symlink": tarfile.SYMTYPE,
            "hardlink": tarfile.LNKTYPE,
            "fifo": tarfile.FIFOTYPE,
        }[kind]
        entry.linkname = "../../outside"
    else:
        entry.size = 1
    responses = iter([json.dumps({"sha": SHA}).encode(), archive(entry)])
    with pytest.raises(ValueError):
        bootstrap["bootstrap"](
            tmp_path,
            [],
            lambda _: next(responses),
            lambda *_args, **_kwargs: pytest.fail("unsafe archive executed"),
        )
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.rglob("setup.py"))


def test_partial_download_never_executes(bootstrap, tmp_path):
    responses = iter([json.dumps({"sha": SHA}).encode(), archive()[:40]])
    with pytest.raises((ValueError, tarfile.TarError, EOFError)):
        bootstrap["bootstrap"](
            tmp_path,
            [],
            lambda _: next(responses),
            lambda *_args, **_kwargs: pytest.fail("partial source executed"),
        )
    assert not list(tmp_path.rglob("setup.py"))


def test_existing_symlink_root_is_rejected(bootstrap, tmp_path):
    root = tmp_path / ".local/share/paiper-bootstrap"
    root.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        bootstrap["bootstrap"](
            tmp_path,
            [],
            lambda _: pytest.fail("downloaded into symlink"),
            lambda *_args, **_kwargs: None,
        )
    assert list(outside.iterdir()) == []


def test_shell_help_runs_without_network_on_mac():
    result = subprocess.run(
        ["sh", str(ROOT / "deploy/bootstrap.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Raspberry Pi" in result.stdout


@pytest.mark.parametrize("download_exit", [0, 22])
def test_documented_download_command_never_executes_failed_curl(
    tmp_path, download_exit
):
    command = (ROOT / "README.md").read_text().split("```sh\n", 1)[1].split("```", 1)[0]
    binaries = tmp_path / "bin"
    binaries.mkdir()
    curl = binaries / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "path.write_text('printf done > \"$HOME/bootstrap-ran\"\\n')\n"
        "sys.exit(int(os.environ['DOWNLOAD_EXIT']))\n"
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "DOWNLOAD_EXIT": str(download_exit),
        },
    )
    assert result.returncode == download_exit, result.stderr
    assert (tmp_path / "bootstrap-ran").exists() is (download_exit == 0)
    directories = list(tmp_path.glob(".paiper-setup-*"))
    assert len(directories) == 1
    assert directories[0].stat().st_mode & 0o077 == 0
