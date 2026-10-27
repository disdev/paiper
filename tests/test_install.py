"""Exercise installation in a fake home; only external builds/systemd are injected."""

import configparser
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_deployment import (
    FAILURE_CASES,
    LocalRunner,
    diagnostic_failure,
    git,
    sensitive_diagnostics,
)

from paiper.state import DisplayState

ROOT = Path(__file__).resolve().parents[1]


def installed_vendor(home):
    vendor = home / "stable vendor/python/lib"
    package = vendor / "waveshare_epd"
    package.mkdir(parents=True)
    for name in ("epd4in0e.py", "epdconfig.py"):
        (package / name).write_text("raise AssertionError('must never import GPIO')\n")
    return vendor


@pytest.fixture
def installer():
    path = ROOT / "deploy/install.py"
    assert path.is_file(), "Task 2 installer is missing"
    spec = importlib.util.spec_from_file_location("paiper_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installation(tmp_path, monkeypatch, installer):
    home = tmp_path / "home with spaces"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "deploy")
    git(repo, "config", "user.email", "tests@example.invalid")
    git(repo, "config", "user.name", "Installer tests")
    (repo / "requirements.lock").write_text("Pillow==12.3.0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    root = home / ".local/share/paiper"
    builds = LocalRunner(repo, root)
    systemctl = []

    def runner(argv, **kwargs):
        if argv[:2] == ["systemctl", "--user"]:
            # Enabling anything before a healthy release is an installer bug.
            assert (root / "current/ready.json").is_file()
            systemctl.append(argv[2:])
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[0] == "/usr/bin/python3":
            argv = [sys.executable, *argv[1:]]
        return builds(argv, **kwargs)

    return installer, home, root, runner, systemctl, builds


def test_initial_install_prepares_before_enabling_updater_only(installation):
    installer, home, root, runner, systemctl, _ = installation
    assert installer.main([], runner=runner) == 0
    settings = json.loads((root / "settings.json").read_text())
    assert settings == {
        "version": 1,
        "repository": "https://github.com/disdev/paiper.git",
        "branch": "deploy",
        "python": "/usr/bin/python3",
        "config": str(home / ".config/paiper/config.yaml"),
        "state": str(home / ".local/state/paiper/display.sqlite3"),
        "vendor_lib": None,
        "scene": "stillness",
    }
    assert not Path(settings["state"]).exists()
    assert "confirmed: false" in Path(settings["config"]).read_text()
    assert (root / "manager.py").read_bytes() == (
        ROOT / "deploy/manager.py"
    ).read_bytes()
    assert systemctl == [
        ["daemon-reload"],
        ["enable", "--now", "paiper-update.timer"],
    ]
    for name in ("update.service", "update.timer", "display.service", "display.timer"):
        unit = "paiper-" + name
        assert (home / ".config/systemd/user" / unit).read_bytes() == (
            ROOT / "deploy/systemd" / unit
        ).read_bytes()


@pytest.mark.parametrize("xdg", ["absolute", "relative", "empty"])
def test_installer_keeps_original_cli_state_default(installation, monkeypatch, xdg):
    installer, home, root, runner, _, _ = installation
    value = str(home / "persistent state") if xdg == "absolute" else xdg
    monkeypatch.setenv("XDG_STATE_HOME", "" if xdg == "empty" else value)
    assert installer.main([], runner=runner) == 0
    settings = json.loads((root / "settings.json").read_text())
    base = Path(value) if xdg == "absolute" else home / ".local/state"
    assert settings["state"] == str(base / "paiper/display.sqlite3")


@pytest.mark.parametrize("xdg", ["absolute", "relative", "empty", "unset"])
def test_installer_units_follow_effective_xdg_config_home(
    installation, monkeypatch, xdg
):
    installer, home, _, runner, systemctl, _ = installation
    base = home / "custom config" if xdg == "absolute" else home / ".config"
    if xdg != "unset":
        monkeypatch.setenv(
            "XDG_CONFIG_HOME",
            str(base) if xdg == "absolute" else "" if xdg == "empty" else "relative",
        )
    assert installer.main([], runner=runner) == 0
    for name in ("update.service", "update.timer", "display.service", "display.timer"):
        unit = "paiper-" + name
        assert (base / "systemd/user" / unit).read_bytes() == (
            ROOT / "deploy/systemd" / unit
        ).read_bytes()
    if xdg == "absolute":
        assert not (home / ".config/systemd/user").exists()
    assert systemctl == [
        ["daemon-reload"],
        ["enable", "--now", "paiper-update.timer"],
    ]


def test_rerun_preserves_settings_config_state_and_display_timer(installation):
    installer, home, root, runner, systemctl, _ = installation
    assert installer.main([], runner=runner) == 0
    settings = json.loads((root / "settings.json").read_text())
    settings["scene"] = "calibration"
    settings["config"] = str(home / "already calibrated.yaml")
    Path(settings["config"]).write_text("panel:\n  rotation: 90\n  confirmed: false\n")
    (root / "settings.json").write_text(json.dumps(settings, indent=4) + "\n")
    before = (root / "settings.json").read_bytes()
    config = Path(settings["config"]).read_bytes()
    with DisplayState(Path(settings["state"])) as state:
        state.reserve(1000, "real-existing-state")
        state.begin_attempt()
        state.succeed(1040, "real-existing-state")
    database = Path(settings["state"]).read_bytes()
    systemctl.clear()
    assert installer.main([], runner=runner) == 0
    assert (root / "settings.json").read_bytes() == before
    assert Path(settings["config"]).read_bytes() == config
    assert Path(settings["state"]).read_bytes() == database
    assert systemctl == [
        ["daemon-reload"],
        ["enable", "--now", "paiper-update.timer"],
    ]


def test_calibrated_config_and_explicit_vendor_enable_display(installation):
    installer, home, root, runner, systemctl, _ = installation
    source = home / "calibrated.yaml"
    source.write_text("panel:\n  rotation: 90\n  confirmed: true\n")
    vendor = installed_vendor(home)
    assert (
        installer.main(
            ["--config", str(source), "--vendor-lib", str(vendor), "--enable-display"],
            runner=runner,
        )
        == 0
    )
    settings = json.loads((root / "settings.json").read_text())
    assert Path(settings["config"]).read_bytes() == source.read_bytes()
    assert settings["vendor_lib"] == str(vendor)
    assert systemctl[-1] == ["enable", "--now", "paiper-display.timer"]


@pytest.mark.parametrize(
    "config",
    [
        "panel:\n  confirmed: false\n",
        "panel:\n  confirmed: 'true'\n",
        "panel:\n  rotation: 180\n  confirmed: true\n",
    ],
)
def test_display_opt_in_validates_real_config(installation, config):
    installer, home, _, runner, systemctl, _ = installation
    source = home / "source.yaml"
    source.write_text(config)
    vendor = installed_vendor(home)
    assert (
        installer.main(
            ["--config", str(source), "--vendor-lib", str(vendor), "--enable-display"],
            runner=runner,
        )
        == 1
    )
    assert not any("paiper-display.timer" in command for command in systemctl)


def test_display_requires_installed_external_vendor(installation):
    installer, home, _, runner, systemctl, _ = installation
    source = home / "source.yaml"
    source.write_text("panel:\n  confirmed: true\n")
    assert (
        installer.main(["--config", str(source), "--enable-display"], runner=runner)
        == 1
    )
    assert not any("paiper-display.timer" in command for command in systemctl)


def test_rerun_uses_existing_config_not_new_confirmation(installation):
    installer, home, root, runner, systemctl, _ = installation
    assert installer.main([], runner=runner) == 0
    before = (home / ".config/paiper/config.yaml").read_bytes()
    source = home / "confirmed.yaml"
    source.write_text("panel:\n  confirmed: true\n")
    assert (
        installer.main(["--config", str(source), "--enable-display"], runner=runner)
        == 1
    )
    assert (home / ".config/paiper/config.yaml").read_bytes() == before
    assert not any("paiper-display.timer" in command for command in systemctl)
    assert (root / "current").is_symlink()


def test_existing_vendor_cannot_be_silently_replaced(installation):
    installer, home, root, runner, _, _ = installation
    assert installer.main([], runner=runner) == 0
    before = (root / "settings.json").read_bytes()
    assert (
        installer.main(["--vendor-lib", str(home / "new vendor")], runner=runner) == 1
    )
    assert (root / "settings.json").read_bytes() == before


@pytest.mark.parametrize("missing", ["epdconfig.py", "epd4in0e.py"])
def test_display_rejects_incomplete_vendor_install(installation, missing):
    installer, home, _, runner, systemctl, _ = installation
    source = home / "confirmed.yaml"
    source.write_text("panel:\n  confirmed: true\n")
    vendor = installed_vendor(home)
    (vendor / "waveshare_epd" / missing).unlink()
    assert (
        installer.main(
            ["--config", str(source), "--vendor-lib", str(vendor), "--enable-display"],
            runner=runner,
        )
        == 1
    )
    assert systemctl == []


def test_display_rerun_checks_stored_profile_even_without_config_argument(installation):
    installer, home, root, runner, systemctl, _ = installation
    vendor = installed_vendor(home)
    assert installer.main(["--vendor-lib", str(vendor)], runner=runner) == 0
    systemctl.clear()
    assert installer.main(["--enable-display"], runner=runner) == 1
    assert systemctl == []
    settings = json.loads((root / "settings.json").read_text())
    Path(settings["config"]).write_text("panel:\n  rotation: 90\n  confirmed: true\n")
    assert installer.main(["--enable-display"], runner=runner) == 0
    assert systemctl[-1] == ["enable", "--now", "paiper-display.timer"]


def test_existing_invalid_settings_are_not_repaired(installation):
    installer, _, root, runner, systemctl, _ = installation
    root.mkdir(parents=True)
    (root / "settings.json").write_text('{"unknown": "retain me"}')
    assert installer.main([], runner=runner) == 1
    assert (root / "settings.json").read_text() == '{"unknown": "retain me"}'
    assert systemctl == []


def test_vendor_inside_managed_release_is_rejected(installation):
    installer, _, root, runner, systemctl, _ = installation
    vendor = root / "releases" / ("a" * 40) / "driver"
    assert installer.main(["--vendor-lib", str(vendor)], runner=runner) == 1
    assert not (root / "settings.json").exists()
    assert systemctl == []


def test_systemctl_failure_reports_failure_without_losing_healthy_release(installation):
    installer, _, root, runner, _, _ = installation

    def unavailable(argv, **kwargs):
        if argv[0] == "systemctl":
            raise subprocess.CalledProcessError(1, argv)
        return runner(argv, **kwargs)

    assert installer.main([], runner=unavailable) == 1
    assert (root / "current/ready.json").is_file()
    assert (root / "settings.json").is_file()
    assert installer.main([], runner=runner) == 0


@pytest.mark.parametrize(("kind", "expected"), FAILURE_CASES)
def test_installer_preserves_command_failure_diagnostics(
    installer, monkeypatch, capsys, kind, expected
):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)

    def fail(argv, **kwargs):
        raise diagnostic_failure(kind, [*argv, "NeverEchoArgvSecret"])

    assert installer.main([], runner=fail) == 1
    error = capsys.readouterr().err
    assert expected in error
    assert "NeverEchoArgvSecret" not in error


@pytest.mark.parametrize("kind", ["exit", "timeout", "oserror"])
def test_installer_command_diagnostics_are_sanitized_and_bounded(
    installer, monkeypatch, capsys, kind
):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    monkeypatch.setenv("SERVICE_TOKEN", "EnvironmentSecret")
    text, secrets = sensitive_diagnostics()
    text += "\nEnvironmentSecret\n" + "x" * 4000

    def fail(argv, **kwargs):
        argv = [*argv, "NeverEchoArgvSecret"]
        if kind == "timeout":
            raise subprocess.TimeoutExpired(argv, 60, stderr=text.encode())
        if kind == "oserror":
            raise OSError(28, text)
        raise subprocess.CalledProcessError(1, argv, stderr=text)

    assert installer.main([], runner=fail) == 1
    error = capsys.readouterr().err
    assert "Could not resolve host" in error
    assert "truncated" in error
    assert len(error) <= 2100
    for secret in [*secrets, "EnvironmentSecret", "NeverEchoArgvSecret"]:
        assert secret not in error


def test_failed_first_release_never_enables_timers(installation):
    installer, _, root, runner, systemctl, builds = installation
    builds.fail = "install"
    assert installer.main([], runner=runner) == 1
    assert not (root / "current").exists()
    assert systemctl == []


def test_known_failed_first_release_never_enables_timers(installation):
    installer, _, _, runner, systemctl, builds = installation
    builds.fail = "install"
    assert installer.main([], runner=runner) == 1
    builds.fail = None
    assert installer.main([], runner=runner) == 1
    assert systemctl == []


def test_root_refusal_precedes_all_filesystem_and_process_work(
    installation, monkeypatch
):
    installer, _, root, _, _, _ = installation
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)

    def forbidden(*args, **kwargs):
        pytest.fail("Root launched a process")

    assert installer.main([], runner=forbidden) == 1
    assert not root.exists()


def test_old_distro_python_refuses_install(installation):
    installer, _, root, _, systemctl, _ = installation

    def old_python(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    assert installer.main([], runner=old_python) == 1
    assert not root.exists()
    assert systemctl == []


def test_units_keep_stable_manager_and_slow_refresh_contract():
    for purpose in ("update", "display"):
        service = configparser.ConfigParser(interpolation=None)
        path = ROOT / f"deploy/systemd/paiper-{purpose}.service"
        assert path.is_file(), "Task 2 units are missing"
        service.read(path)
        command = "update" if purpose == "update" else "run"
        assert service["Service"]["ExecStart"] == (
            f'/usr/bin/python3 "%h/.local/share/paiper/manager.py" {command}'
        )
        assert service["Service"]["Type"] == "oneshot"
        assert service["Service"]["StandardOutput"] == "journal"
        assert service["Service"]["StandardError"] == "journal"
        assert "User" not in service["Service"]
        assert service["Service"]["TimeoutStartSec"] == "infinity"
        timer = configparser.ConfigParser(interpolation=None)
        timer.read(ROOT / f"deploy/systemd/paiper-{purpose}.timer")
        assert timer["Timer"]["OnBootSec"] == (
            "2min" if purpose == "update" else "1min"
        )
        assert timer["Timer"]["OnUnitInactiveSec"] == "5min"
        assert timer["Timer"]["Unit"] == f"paiper-{purpose}.service"
        assert timer["Timer"].get("RandomizedDelaySec", "0") == (
            "20s" if purpose == "update" else "0"
        )
        assert timer["Install"]["WantedBy"] == "timers.target"
