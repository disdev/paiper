"""Guided setup uses real files; only platform, OS, network and hardware are faked."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_install as install_tests

installer = install_tests.installer
installation = install_tests.installation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup():
    path = ROOT / "deploy/setup.py"
    assert path.exists(), "Guided setup is not implemented"
    spec = importlib.util.spec_from_file_location("guided_setup", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def supported():
    return {
        "model": "Raspberry Pi 5 Model B Rev 1.0",
        "machine": "aarch64",
        "bits": 64,
        "os": {"ID": "debian", "VERSION_ID": "12", "VERSION_CODENAME": "bookworm"},
        "pi_os": True,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"model": "Raspberry Pi 4 Model B"},
        {"machine": "armv7l", "bits": 32},
        {"os": {"ID": "ubuntu", "VERSION_ID": "24"}},
        {"os": {"ID": "debian", "VERSION_ID": "11"}},
        {"pi_os": False},
    ],
)
def test_unsupported_platform_rejected_before_side_effects(
    setup, tmp_path, monkeypatch, change
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    facts = supported() | change

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported platform reached external effects or prompts")

    assert setup.main([], probe=lambda: facts, runner=forbidden, prompt=forbidden) == 1
    assert list(tmp_path.iterdir()) == []


def test_root_refusal_before_probe(setup, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)

    def forbidden(*args, **kwargs):
        pytest.fail("root reached platform/files/processes")

    assert setup.main([], probe=forbidden, runner=forbidden) == 1
    assert list(tmp_path.iterdir()) == []


def vendor_zip():
    content = io.BytesIO()
    prefix = "4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/"
    with zipfile.ZipFile(content, "w") as archive:
        for name in ("__init__.py", "epd4in0e.py", "epdconfig.py"):
            archive.writestr(
                prefix + name, "# vendor notice\nraise RuntimeError('no import')\n"
            )
        archive.writestr(prefix + "unsafe.so", "never extract")
        archive.writestr("../../escape", "never extract")
    return content.getvalue()


def test_vendor_checksum_mismatch_precedes_extraction(setup, tmp_path):
    destination = tmp_path / "vendor"
    with pytest.raises(ValueError, match="checksum"):
        setup.extract_vendor(vendor_zip(), destination)
    assert list(tmp_path.iterdir()) == []


def test_vendor_allowlist_and_custom_files_preserved(setup, tmp_path, monkeypatch):
    data = vendor_zip()
    monkeypatch.setattr(setup, "VENDOR_SHA256", hashlib.sha256(data).hexdigest())
    destination = tmp_path / "vendor"
    setup.extract_vendor(data, destination)
    package = destination / "waveshare_epd"
    assert sorted(p.name for p in package.iterdir()) == [
        "__init__.py",
        "epd4in0e.py",
        "epdconfig.py",
    ]
    assert all(p.read_text().startswith("# vendor notice") for p in package.iterdir())
    (package / "epdconfig.py").write_text("custom configuration")
    with pytest.raises(ValueError, match="exists"):
        setup.extract_vendor(data, destination)
    assert (package / "epdconfig.py").read_text() == "custom configuration"


def test_help_is_safe_on_mac():
    result = subprocess.run(
        [sys.executable, str(ROOT / "deploy/setup.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--state" in result.stdout


class OSRunner:
    def __init__(self, missing=(), spi="0", groups=()):
        self.missing = set(missing)
        self.spi = spi
        self.groups = groups
        self.commands = []

    def __call__(self, argv, **kwargs):
        argv = list(map(str, argv))
        self.commands.append(argv)
        output = ""
        code = 0
        if argv[0] == "dpkg-query":
            output = "install ok installed" if argv[-1] not in self.missing else ""
            code = int(not output)
        elif argv[:3] == ["raspi-config", "nonint", "get_spi"]:
            output = self.spi
        elif argv[:2] == ["id", "-nG"]:
            output = " ".join(self.groups)
        elif argv[:3] == ["sudo", "apt-get", "install"]:
            self.missing.clear()
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code, output.encode(), b"")


def test_denied_packages_never_elevate(setup, tmp_path):
    runner = OSRunner(missing=["python3-lgpio"])
    questions = []

    def deny(question):
        questions.append(question)
        return "n"

    with pytest.raises(ValueError, match="prerequisites"):
        setup.prerequisites(tmp_path, runner, deny)
    assert any(
        "python3-lgpio" in question and "apt-get update" in question
        for question in questions
    )
    assert not any(cmd[0] == "sudo" for cmd in runner.commands)


def test_spi_reboot_and_group_login_are_rechecked(setup, tmp_path):
    runner = OSRunner(spi="1", groups=["spi", "gpio"])
    assert setup.prerequisites(tmp_path, runner, lambda _: "y") == 2
    assert ["sudo", "raspi-config", "nonint", "do_spi", "0"] in runner.commands
    assert not any("reboot" in cmd for cmd in runner.commands)
    runner.spi = "0"
    runner.groups = ["spi"]
    assert setup.prerequisites(tmp_path, runner, lambda _: "y") == 2
    assert any(cmd[:4] == ["sudo", "usermod", "-aG", "gpio"] for cmd in runner.commands)
    runner.groups = ["spi", "gpio"]
    assert setup.prerequisites(tmp_path, runner, lambda _: "n") == 0


def test_denied_spi_never_enables_it(setup, tmp_path):
    runner = OSRunner(spi="1", groups=["spi", "gpio"])
    with pytest.raises(ValueError, match="SPI"):
        setup.prerequisites(tmp_path, runner, lambda _: "n")
    assert not any(cmd[0] == "sudo" for cmd in runner.commands)


def test_manual_paths_persist_across_reboot_without_environment(
    setup, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    root = tmp_path / "runtime"
    root.mkdir()
    config = tmp_path / "manual.yaml"
    config.write_text("panel:\n  rotation: 90\n  confirmed: false\n")
    state = tmp_path / "original.sqlite3"
    from paiper.state import DisplayState

    with DisplayState(state):
        pass
    args = SimpleNamespace(config=config, state=state, vendor_lib=None)
    choices = setup.choose_paths(root, args, lambda _: "y")
    empty = SimpleNamespace(config=None, state=None, vendor_lib=None)
    resumed = setup.choose_paths(root, empty, lambda _: pytest.fail("asked again"))
    assert resumed == choices
    assert choices["state"] == str(state)
    assert choices["config"] == str(config)
    assert config.read_text() == "panel:\n  rotation: 90\n  confirmed: false\n"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("provenance", ["current", "legacy", "unknown"])
def test_lost_selected_state_blocks_resume_before_commands(
    setup, tmp_path, monkeypatch, managed, provenance
):
    from paiper.state import DisplayState

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    root = tmp_path / ".local/share/paiper"
    root.mkdir(parents=True)
    config, state = tmp_path / "original.yaml", tmp_path / "original.db"
    config.write_text("# keep\npanel:\n  confirmed: false\n")
    with DisplayState(state) as database:
        database.reserve(1000, "original")
        database.succeed(1040, "original")
    original = state.read_bytes()
    setup.choose_paths(
        root,
        SimpleNamespace(config=config, state=state, vendor_lib=None),
        lambda _: "y",
    )
    choices_file = root / "setup-choices.json"
    if provenance != "current":
        choices = json.loads(choices_file.read_text())
        choices.pop("state_phase", None)
        choices_file.write_text(json.dumps(choices))
    if managed:
        setup.module("manager").write_json(
            root / "settings.json",
            {
                "version": 1,
                "repository": "https://github.com/disdev/paiper.git",
                "branch": "deploy",
                "python": "/usr/bin/python3",
                "config": str(config),
                "state": str(state),
                "vendor_lib": None,
                "scene": "stillness",
            },
        )
        if provenance == "unknown":
            choices_file.unlink()
    saved = {path: path.read_bytes() for path in root.glob("*.json")}
    state.rename(tmp_path / "unmounted.db")

    def forbidden(*args, **kwargs):
        pytest.fail("Missing original state reached commands or prompts")

    assert setup.main([], probe=supported, runner=forbidden, prompt=forbidden) == 1
    assert not state.exists()
    assert (tmp_path / "unmounted.db").read_bytes() == original
    for path, content in saved.items():
        if path.name == "settings.json" or provenance == "current":
            assert path.read_bytes() == content
    assert config.read_text() == "# keep\npanel:\n  confirmed: false\n"


@pytest.mark.parametrize("action", ["remaining", "calibrate", "confirm"])
def test_missing_candidate_state_never_recreated(candidate, panel, action):
    from paiper.state import DisplayState

    config, state, _, transfers = panel
    with DisplayState(state):
        pass
    state.rename(state.with_suffix(".original"))
    before = config.read_bytes()
    with pytest.raises((ValueError, RuntimeError, OSError)):
        candidate.operate(action, config, state)
    assert not state.exists()
    assert config.read_bytes() == before
    assert not list(config.parent.glob("*.bak"))
    assert not transfers


def test_first_use_initialization_is_consumed_across_reboot(
    setup, candidate, tmp_path, monkeypatch
):
    from paiper.state import DisplayState

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    root = tmp_path / "runtime"
    root.mkdir()
    args = SimpleNamespace(config=None, state=None, vendor_lib=None)
    choices = setup.choose_paths(
        root, args, lambda q: "" if "Existing manual config" in q else "y"
    )
    assert (
        setup.choose_paths(root, args, lambda _: pytest.fail("asked again")) == choices
    )
    config, state = Path(choices["config"]), Path(choices["state"])
    record = root / "setup-choices.json"
    assert not state.exists()
    assert candidate.operate("initialize", config, state, choices_path=record) == {
        "initialized": True
    }
    with DisplayState(state) as database:
        assert database.status()["cooldown_at"] is None
    before = record.read_bytes()
    state.unlink()
    with pytest.raises((ValueError, RuntimeError, OSError)):
        candidate.operate("initialize", config, state, choices_path=record)
    assert not state.exists()
    assert record.read_bytes() == before


@pytest.mark.parametrize("observed_at_selection", [False, True])
def test_observing_existing_database_consumes_first_use_permission(
    setup, tmp_path, monkeypatch, observed_at_selection
):
    from paiper.state import DisplayState

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    root = tmp_path / "runtime"
    root.mkdir()
    state = tmp_path / ".local/state/paiper/display.sqlite3"
    if observed_at_selection:
        with DisplayState(state):
            pass
    setup.choose_paths(
        root,
        SimpleNamespace(config=None, state=None, vendor_lib=None),
        lambda q: "" if "Existing manual config" in q else "y",
    )
    if not observed_at_selection:
        with DisplayState(state):
            pass
        setup.check_setup_state(root, state, allow_pending=True)
    assert (
        json.loads((root / "setup-choices.json").read_text())["state_phase"]
        == "required"
    )
    state.unlink()
    with pytest.raises(setup.module("manager").DeploymentError, match="missing"):
        setup.check_setup_state(root, state, allow_pending=True)
    assert not state.exists()


@pytest.mark.parametrize("database_exists", [False, True])
def test_interrupted_initialization_resume_is_conservative(
    setup, candidate, tmp_path, monkeypatch, database_exists
):
    from paiper.state import DisplayState

    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".local/share/paiper"
    root.mkdir(parents=True)
    config, state = tmp_path / "config.yaml", tmp_path / "state.db"
    config.write_text("panel:\n  confirmed: false\n")
    if database_exists:
        with DisplayState(state):
            pass
    record = root / "setup-choices.json"
    record.write_text(
        json.dumps(
            {
                "config": str(config),
                "state": str(state),
                "vendor_lib": None,
                "state_phase": "initializing",
            }
        )
    )
    before = record.read_bytes(), state.read_bytes() if database_exists else None
    args = SimpleNamespace(config=None, state=None, vendor_lib=None)
    with pytest.raises((ValueError, RuntimeError), match="initializ"):
        setup.run_setup(
            args,
            runner=lambda *a, **k: pytest.fail("OS command"),
            prompt=lambda _: pytest.fail("prompt"),
        )
    with pytest.raises((ValueError, RuntimeError), match="initializ"):
        candidate.operate("initialize", config, state, choices_path=record)
    assert (
        record.read_bytes(),
        state.read_bytes() if state.exists() else None,
    ) == before


@pytest.mark.parametrize("stage", ["before_open", "after_schema"])
def test_initialization_crash_durably_consumes_permission(
    setup, candidate, tmp_path, monkeypatch, stage
):
    from paiper.state import DisplayState

    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "runtime"
    root.mkdir()
    config, state = tmp_path / "config.yaml", tmp_path / "state.db"
    config.write_text("panel:\n  confirmed: false\n")
    record = root / "setup-choices.json"
    record.write_text(
        json.dumps(
            {
                "config": str(config),
                "state": str(state),
                "vendor_lib": None,
                "state_phase": "pending",
            }
        )
    )
    if stage == "before_open":

        def crash(*args, **kwargs):
            raise OSError("simulated crash before SQLite opens")

        monkeypatch.setattr(candidate, "DisplayState", crash)
    else:

        def crash(path):
            raise OSError("simulated crash after schema creation")

        monkeypatch.setattr(
            candidate,
            "sync_directory",
            lambda path: crash(path) if path == state.parent else None,
        )
    with pytest.raises(OSError, match="simulated crash"):
        candidate.operate("initialize", config, state, choices_path=record)
    assert json.loads(record.read_text())["state_phase"] == "initializing"
    if stage == "after_schema":
        with DisplayState(state) as database:
            assert database.status()["in_progress"] == 0
        state.unlink()
    with pytest.raises(ValueError, match="initializ"):
        candidate.operate("initialize", config, state, choices_path=record)
    assert not state.exists()


def test_required_candidate_sqlite_open_cannot_create_after_existence_check(
    candidate, panel, monkeypatch
):
    from paiper import state as state_module

    config, state, _, transfers = panel
    with state_module.DisplayState(state):
        pass
    connect = state_module.sqlite3.connect

    def disappear(*args, **kwargs):
        state.unlink()
        return connect(*args, **kwargs)

    monkeypatch.setattr(state_module.sqlite3, "connect", disappear)
    with pytest.raises(state_module.StateCorruptError):
        candidate.operate("remaining", config, state)
    assert not state.exists() and not transfers


@pytest.mark.parametrize("action", ["remaining", "calibrate", "confirm"])
@pytest.mark.parametrize("disappear_at_launch", [False, True])
def test_real_candidate_bridge_initialization_and_missing_state(
    setup, tmp_path, monkeypatch, action, disappear_at_launch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    root = tmp_path / "runtime"
    root.mkdir()
    choices = setup.choose_paths(
        root,
        SimpleNamespace(config=None, state=None, vendor_lib=None),
        lambda q: "" if "Existing manual config" in q else "y",
    )
    shared = setup.module("manager")
    shared.write_json(
        root / "settings.json",
        {
            **choices,
            "version": 1,
            "repository": "https://github.com/disdev/paiper.git",
            "branch": "deploy",
            "python": "/usr/bin/python3",
            "scene": "stillness",
        },
    )
    manager = shared.Manager(root)
    release = root / "releases" / ("a" * 40)
    (release / "app").mkdir(parents=True)
    state = Path(choices["state"])
    launched = []

    def runner(argv, **kwargs):
        assert argv[1] == "-c"
        launched.append(argv[-5])
        if disappear_at_launch and argv[-5] == action:
            state.rename(state.with_suffix(".original"))
        return subprocess.run(
            [sys.executable, *argv[1:]], check=kwargs.pop("check"), **kwargs
        )

    assert setup.candidate_operation(manager, release, runner, "initialize") == {
        "initialized": True
    }
    shared.validate_state(state)
    assert (
        json.loads((root / "setup-choices.json").read_text())["state_phase"]
        == "required"
    )
    original = state.read_bytes()
    config_before = Path(choices["config"]).read_bytes()
    settings_before = (root / "settings.json").read_bytes()
    if not disappear_at_launch:
        state.rename(state.with_suffix(".original"))
    with pytest.raises((shared.DeploymentError, subprocess.CalledProcessError)):
        setup.candidate_operation(manager, release, runner, action)
    assert launched == (
        ["initialize", action] if disappear_at_launch else ["initialize"]
    )
    assert not state.exists()
    assert state.with_suffix(".original").read_bytes() == original
    assert Path(choices["config"]).read_bytes() == config_before
    assert (root / "settings.json").read_bytes() == settings_before
    with pytest.raises(shared.DeploymentError):
        setup.candidate_operation(manager, release, runner, "initialize")
    assert not state.exists()


def test_existing_settings_authoritative_and_bytes_unchanged(setup, tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    settings = {
        "version": 1,
        "repository": "https://github.com/disdev/paiper.git",
        "branch": "deploy",
        "python": "/usr/bin/python3",
        "state": str(tmp_path / "original.db"),
        "config": str(tmp_path / "original.yaml"),
        "vendor_lib": None,
        "scene": "stillness",
    }
    path = root / "settings.json"
    path.write_text(json.dumps(settings, indent=4))
    before = path.read_bytes()
    args = SimpleNamespace(config=None, state=None, vendor_lib=None)
    result = setup.choose_paths(
        root, args, lambda _: pytest.fail("should use stored settings")
    )
    assert result["state"] == settings["state"]
    assert path.read_bytes() == before
    args.state = tmp_path / "replacement.db"
    with pytest.raises(ValueError, match="state"):
        setup.choose_paths(root, args, lambda _: "y")
    assert path.read_bytes() == before


def test_browser_provisioning_uses_stable_constrained_venv_only_after_consent(
    setup, tmp_path
):
    runner = OSRunner(groups=["spi", "gpio"])
    setup.browser_dependencies(tmp_path, runner, lambda _: "n")
    assert runner.commands == []
    setup.browser_dependencies(tmp_path, runner, lambda _: "y")
    assert runner.commands[0] == [
        "/usr/bin/python3",
        "-m",
        "venv",
        "--system-site-packages",
        str(tmp_path / "bootstrap-venv"),
    ]
    pip = next(cmd for cmd in runner.commands if "pip" in cmd)
    assert "--constraint" in pip and "-e" not in pip and "sudo" not in pip
    assert runner.commands[-1][-4:] == ["-m", "playwright", "install-deps", "chromium"]


@pytest.fixture
def candidate(setup):
    path = ROOT / "deploy/setup_candidate.py"
    assert path.is_file(), "Candidate calibration bridge is missing"
    return setup.module("setup_candidate")


@pytest.fixture
def panel(tmp_path, monkeypatch, candidate):
    from PIL import Image

    from paiper import display
    from paiper.state import DisplayState, StateLockedError

    config = tmp_path / "config.yaml"
    config.write_text("# original\npanel:\n  rotation: 270\n  confirmed: false\n")
    state = tmp_path / "safety.sqlite3"
    with DisplayState(state):
        pass
    clock = [1000.0]
    transfers = []
    monkeypatch.setattr(candidate.time, "time", lambda: clock[0])

    def image(*args):
        # Asymmetric image ensures rotating changes the actual payload digest.
        frame = Image.new("RGB", (600, 400), "white")
        frame.putpixel((0, 0), (0, 0, 0))
        return frame

    class Driver:
        def init(self):
            with pytest.raises(StateLockedError), DisplayState(state):
                pass
            return 0

        def display(self, data):
            transfers.append((clock[0], data))
            clock[0] += 40

        def sleep(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(display, "WaveshareDriver", Driver)
    monkeypatch.setattr(candidate, "render_scene", image)
    return config, state, clock, transfers


def test_candidate_transfer_skip_and_guarded_profile_edit(candidate, panel):
    config, state, clock, transfers = panel
    before = config.read_bytes()
    profile = candidate.operate("profile", config, state)
    assert candidate.operate("calibrate", config, state, 270, profile["digest"]) == {
        "transferred": True
    }
    assert config.read_bytes() == before
    assert candidate.operate("calibrate", config, state, 90, profile["digest"]) == {
        "transferred": False
    }
    assert len(transfers) == 1
    assert candidate.operate("remaining", config, state)["seconds"] >= 299
    clock[0] += 300
    assert candidate.operate("calibrate", config, state, 90, profile["digest"]) == {
        "transferred": True
    }
    database = state.read_bytes()
    candidate.operate("confirm", config, state, 90, profile["digest"])
    from paiper.config import load_profile

    assert load_profile(config).rotation == 90
    assert load_profile(config).confirmed is True
    assert state.read_bytes() == database
    assert [p.read_bytes() for p in config.parent.glob("config.yaml.setup-*.bak")] == [
        before
    ]


def test_candidate_refuses_concurrent_profile_change(candidate, panel):
    config, state, _, transfers = panel
    profile = candidate.operate("profile", config, state)
    config.write_text("panel:\n  confirmed: false\n")
    with pytest.raises(ValueError, match="changed"):
        candidate.operate("calibrate", config, state, 270, profile["digest"])
    with pytest.raises(ValueError, match="changed"):
        candidate.operate("confirm", config, state, 270, profile["digest"])
    assert transfers == []
    assert not list(config.parent.glob("*.bak"))


@pytest.mark.parametrize("action", ["remaining", "calibrate", "confirm"])
def test_candidate_interrupted_history_never_refreshes(candidate, panel, action):
    from paiper.state import DisplayState, IncompleteAttemptError

    config, state, _, transfers = panel
    with DisplayState(state) as db:
        db.reserve(900, "interrupted")
        db.begin_attempt()
    before = state.read_bytes(), config.read_bytes()
    with pytest.raises(IncompleteAttemptError):
        candidate.operate(action, config, state)
    assert (state.read_bytes(), config.read_bytes()) == before
    assert transfers == []


def test_calibration_requires_label_and_actual_transfer(setup, tmp_path):
    calls = []
    profile = {"rotation": 270, "confirmed": False, "digest": "abc"}

    def operation(action, *args):
        calls.append((action, args))
        return {
            "profile": profile,
            "remaining": {"seconds": 0},
            "calibrate": {"transferred": False},
        }[action]

    assert setup.calibrate(operation, lambda _: "n", lambda _: None) is False
    assert not any(action == "calibrate" for action, _ in calls)
    calls.clear()
    questions = []

    def approve_all(question):
        questions.append(question)
        return "y"

    assert setup.calibrate(operation, approve_all, lambda _: None) is False
    assert any(
        "six-color E6" in question and "wiring" in question for question in questions
    )
    assert not any(action == "confirm" for action, _ in calls)


def test_alternate_rotation_waits_and_only_confirms_after_visual_approval(
    setup, candidate, panel
):
    config, state, clock, transfers = panel
    answers = iter(["y", "n", "y", "y", "y"])
    waits = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds

    def operation(action, rotation=270, digest=""):
        return candidate.operate(action, config, state, rotation, digest)

    assert setup.calibrate(operation, lambda _: next(answers), wait) is True
    assert waits and sum(waits) >= 299
    assert len(transfers) == 2
    assert transfers[1][0] - (transfers[0][0] + 40) >= 300
    from paiper.config import load_profile

    assert load_profile(config).rotation == 90


def test_timer_declines_preserve_actual_enabled_state(setup, capsys):
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        output = b"enabled\n" if "is-enabled" in argv else b"active\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    setup.offer_timer("update", runner, lambda _: "n")
    assert not any("disable" in cmd or "stop" in cmd for cmd in commands)
    assert "enabled" in capsys.readouterr().out


def test_dropin_conflict_is_never_overwritten(setup, tmp_path):
    units = tmp_path / "units"
    dropin = units / "paiper-display.service.d/10-paiper-gpio.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("[Service]\nEnvironment=GPIOZERO_PIN_FACTORY=custom\n")
    before = dropin.read_bytes()
    with pytest.raises(ValueError, match="drop-in"):
        setup.pin_factory(units, OSRunner(), lambda _: "y")
    assert dropin.read_bytes() == before


def test_candidate_bridge_forwards_authoritative_paths_and_distinguishes_skip(
    setup, tmp_path
):
    root = tmp_path / "root"
    root.mkdir()
    config, state = tmp_path / "manual.yaml", tmp_path / "original.db"
    settings = {
        "version": 1,
        "repository": "https://github.com/disdev/paiper.git",
        "branch": "deploy",
        "python": "/usr/bin/python3",
        "config": str(config),
        "state": str(state),
        "vendor_lib": str(tmp_path / "vendor"),
        "scene": "stillness",
    }
    (root / "settings.json").write_text(json.dumps(settings))
    from paiper.state import DisplayState

    with DisplayState(state):
        pass
    setup.choose_paths(
        root,
        SimpleNamespace(config=None, state=None, vendor_lib=None),
        lambda _: pytest.fail("prompt"),
    )
    manager = setup.module("manager").Manager(root)
    release = root / "releases" / ("a" * 40)

    def runner(argv, **kwargs):
        assert argv[0] == str(release / ".venv/bin/python")
        assert argv[-5:] == ["calibrate", str(config), str(state), "90", "digest"]
        assert kwargs["timeout"] is None
        assert kwargs["env"]["GPIOZERO_PIN_FACTORY"] == "lgpio"
        assert kwargs["env"]["PYTHONPATH"] == settings["vendor_lib"]
        return subprocess.CompletedProcess(
            argv, 3, b'noise\nPAIPER_SETUP_RESULT={"transferred":false}\n', b""
        )

    assert setup.candidate_operation(
        manager, release, runner, "calibrate", 90, "digest"
    ) == {"transferred": False}


def test_setup_offline_install_preserves_existing_state_and_asks_before_timers(
    setup, installation, monkeypatch
):
    _, home, root, builds_runner, systemctl, _ = installation
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    os_runner = OSRunner(spi="1", groups=["spi", "gpio"])
    answers = []

    def prompt(question):
        answers.append(question)
        if "Existing manual config" in question:
            return ""
        if "FIRST" in question or "Enable SPI" in question:
            return "y"
        return "n"

    def runner(argv, **kwargs):
        if (
            argv[:3] == ["sudo", "raspi-config", "nonint"]
            or argv[0] in ("dpkg-query", "id", "raspi-config")
            or (argv[0] == "/usr/bin/python3" and "-c" in argv)
        ):
            return os_runner(argv, **kwargs)
        if argv[0] == "systemctl" and any(
            key in argv for key in ("is-active", "is-enabled")
        ):
            return subprocess.CompletedProcess(
                argv, 1, b"inactive\n" if "is-active" in argv else b"disabled\n", b""
            )
        return builds_runner(argv, **kwargs)

    assert setup.main([], runner=runner, prompt=prompt, probe=supported) == 2
    choices = json.loads((root / "setup-choices.json").read_text())
    assert choices["state_phase"] == "pending"
    assert not Path(choices["state"]).exists()
    config_before = Path(choices["config"]).read_bytes()
    os_runner.spi = "0"
    assert setup.main([], runner=runner, prompt=prompt, probe=supported) == 0
    assert Path(choices["config"]).read_bytes() == config_before
    assert (
        json.loads((root / "setup-choices.json").read_text())["state_phase"]
        == "required"
    )
    setup.module("manager").validate_state(Path(choices["state"]))
    settings = json.loads((root / "settings.json").read_text())
    assert settings["state"] == str(home / ".local/state/paiper/display.sqlite3")
    assert (root / "current/ready.json").is_file()
    assert not any(
        "enable" in cmd or "disable" in cmd or "stop" in cmd for cmd in systemctl
    )
    assert "confirmed: false" in Path(settings["config"]).read_text()
    original = (root / "settings.json").read_bytes()
    assert setup.main([], runner=runner, prompt=prompt, probe=supported) == 0
    assert (root / "settings.json").read_bytes() == original


def test_vendor_null_edit_requires_permission_and_keeps_backup(
    setup, tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    settings = {
        "version": 1,
        "repository": "https://github.com/disdev/paiper.git",
        "branch": "deploy",
        "python": "/usr/bin/python3",
        "config": str(tmp_path / "config"),
        "state": str(tmp_path / "original.db"),
        "vendor_lib": None,
        "scene": "stillness",
    }
    path = root / "settings.json"
    path.write_text(json.dumps(settings, indent=4))
    original = path.read_bytes()
    manager = setup.module("manager").Manager(root)
    fetches = []
    data = vendor_zip()
    monkeypatch.setattr(setup, "VENDOR_SHA256", hashlib.sha256(data).hexdigest())

    def fetch(url):
        fetches.append(url)
        return data

    assert setup.configure_vendor(manager, None, lambda _: "n", fetch) is False
    assert path.read_bytes() == original and fetches == []
    assert setup.configure_vendor(manager, None, lambda _: "y", fetch) is True
    assert Path(json.loads(path.read_text())["vendor_lib"]).is_dir()
    assert [p.read_bytes() for p in root.glob("settings.json.setup-*.bak")] == [
        original
    ]
    after = path.read_bytes()
    assert (
        setup.configure_vendor(
            manager, None, lambda _: pytest.fail("configured vendor"), fetch
        )
        is True
    )
    assert path.read_bytes() == after and len(fetches) == 1


def test_active_timer_pause_decline_never_stops_timer_or_calls_hardware(setup):
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"active\n", b"")

    assert setup.pause_display(runner, lambda _: "n") is False
    assert not any("stop" in command for command in commands)
    assert setup.pause_display(runner, lambda _: "y") is True
    assert ["systemctl", "--user", "stop", "paiper-display.timer"] in commands
    assert not any("paiper-display.service" in command for command in commands)


def test_prerequisite_changes_refuse_existing_display_state_lock(
    setup, installation, monkeypatch
):
    from paiper.state import DisplayState

    _, home, root, _, _, _ = installation
    state = home / ".local/state/paiper/display.sqlite3"
    calls = []
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "/usr/bin/python3":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        pytest.fail("OS provisioning ran while display state was locked")

    with DisplayState(state):
        assert (
            setup.main(
                [],
                runner=runner,
                probe=supported,
                prompt=lambda q: "" if "Existing manual config" in q else "y",
            )
            == 1
        )
    assert not (root / "settings.json").exists()


@pytest.mark.parametrize("corrupt", [False, True])
def test_unsafe_history_blocks_os_provisioning(setup, installation, corrupt):
    from paiper.state import DisplayState

    _, home, _, _, _, _ = installation
    config = home / "existing.yaml"
    config.write_text("panel:\n  confirmed: false\n")
    state = home / "original.db"
    if corrupt:
        state.write_bytes(b"corrupt history")
    else:
        with DisplayState(state) as database:
            database.reserve(1000, "interrupted")
            database.begin_attempt()
    before = state.read_bytes()

    def runner(argv, **kwargs):
        if argv[0] == "/usr/bin/python3":
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        pytest.fail("Unsafe history reached OS provisioning")

    assert (
        setup.main(
            ["--config", str(config), "--state", str(state)],
            runner=runner,
            probe=supported,
            prompt=lambda _: "y",
        )
        == 1
    )
    assert state.read_bytes() == before


def test_old_distro_python_refuses_before_files_or_prompts(
    setup, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)

    def runner(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    assert (
        setup.main(
            [],
            runner=runner,
            probe=supported,
            prompt=lambda _: pytest.fail("old Python prompted"),
        )
        == 1
    )
    assert list(tmp_path.iterdir()) == []


def test_dropin_environment_file_is_preserved_without_enabling(setup, tmp_path):
    def runner(argv, **kwargs):
        output = (
            b"/user/custom.env (ignore_errors=no)\n"
            if "--property=EnvironmentFiles" in argv
            else b""
        )
        return subprocess.CompletedProcess(argv, 0, output, b"")

    with pytest.raises(ValueError, match="EnvironmentFile"):
        setup.pin_factory(tmp_path, runner, lambda _: "y")
    assert list(tmp_path.iterdir()) == []


def test_root_does_not_even_load_deployment_module(setup, monkeypatch):
    monkeypatch.setattr(setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        setup, "module", lambda _: pytest.fail("root imported deployment source")
    )
    assert setup.main([]) == 1


@pytest.mark.parametrize("state", ["relative.db", None, 17])
def test_invalid_resume_choices_fail_before_using_paths(setup, tmp_path, state):
    (tmp_path / "setup-choices.json").write_text(
        json.dumps(
            {
                "config": str(tmp_path / "config.yaml"),
                "state": state,
                "vendor_lib": None,
            }
        )
    )
    with pytest.raises(ValueError, match="choices"):
        setup.choose_paths(
            tmp_path,
            SimpleNamespace(config=None, state=None, vendor_lib=None),
            lambda _: pytest.fail("should not prompt"),
        )


def test_missing_saved_manual_config_is_not_recreated(setup, installation, monkeypatch):
    _, home, root, builds_runner, _, _ = installation
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)
    root.mkdir(parents=True)
    missing = home / "deleted-manual.yaml"
    (root / "setup-choices.json").write_text(
        json.dumps(
            {
                "config": str(missing),
                "state": str(home / "original.db"),
                "vendor_lib": None,
            }
        )
    )
    os_runner = OSRunner(groups=["spi", "gpio"])

    def runner(argv, **kwargs):
        if argv[0] in ("dpkg-query", "id", "raspi-config") or (
            argv[0] == "/usr/bin/python3" and "-c" in argv
        ):
            return os_runner(argv, **kwargs)
        return builds_runner(argv, **kwargs)

    assert setup.main([], runner=runner, probe=supported, prompt=lambda _: "n") == 1
    assert not missing.exists()


def test_failed_candidate_diagnostics_use_shared_sanitizer(
    setup, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SERVICE_TOKEN", "NeverRevealThis")
    monkeypatch.setattr(setup.os, "geteuid", lambda: 1000)

    def runner(argv, **kwargs):
        raise subprocess.CalledProcessError(
            9,
            [*argv, "ArgumentSecret"],
            stderr=b"DNS lookup failed; token=NeverRevealThis\n" + b"x" * 5000,
        )

    assert setup.main([], runner=runner, probe=supported, prompt=lambda _: "n") == 1
    output = capsys.readouterr().err
    assert (
        "exit 9" in output and "DNS lookup failed" in output and "truncated" in output
    )
    assert "NeverRevealThis" not in output and "ArgumentSecret" not in output
    assert len(output) <= 2100
    assert list(tmp_path.iterdir()) == []


def test_interrupt_while_waiting_preserves_unconfirmed_profile_and_history(
    setup, candidate, panel
):
    config, state, _, transfers = panel
    from paiper.state import DisplayState

    with DisplayState(state) as database:
        database.reserve(900, "original")
        database.begin_attempt()
        database.succeed(940, "original")
    before = config.read_bytes(), state.read_bytes()

    def operation(action, rotation=270, digest=""):
        return candidate.operate(action, config, state, rotation, digest)

    def interrupt(_):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        setup.calibrate(operation, lambda _: "y", interrupt)
    assert (config.read_bytes(), state.read_bytes()) == before
    assert transfers == []


def test_terminal_prompt_ignores_redirected_stdin(setup, tmp_path, monkeypatch):
    # A pseudo-terminal exercises actual /dev/tty-style line IO without stdin.
    import pty
    import select
    import threading

    master, slave = pty.openpty()
    original_open = open
    monkeypatch.setattr(
        setup,
        "open",
        lambda path, mode: original_open(
            os.dup(slave) if path == "/dev/tty" else path, mode
        ),
        raising=False,
    )

    def respond():
        if select.select([master], [], [], 1)[0]:
            os.read(master, 4096)
            os.write(master, b"y\n")

    thread = threading.Thread(target=respond)
    thread.start()
    try:
        assert setup.terminal_prompt("Confirm?") == "y"
    finally:
        thread.join(timeout=2)
        os.close(master)
        os.close(slave)


def test_manual_tilde_path_is_used_without_creating_relative_files(
    setup, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "manual.yaml"
    config.write_text("# original\npanel:\n  confirmed: false\n")
    state = tmp_path / "original.db"
    state.write_bytes(b"existing state")
    root = tmp_path / "root"
    root.mkdir()
    result = setup.choose_paths(
        root,
        SimpleNamespace(config=Path("~/manual.yaml"), state=state, vendor_lib=None),
        lambda _: "y",
    )
    assert result["config"] == str(config)
    assert not (tmp_path / "~").exists()


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "skip", "declined-label", "reuse-no-access"]
)
def test_guided_hardware_path_uses_candidate_and_never_enables_unverified_display(
    setup,
    candidate,
    panel,
    installation,
    monkeypatch,
    outcome,
):
    from paiper import display
    from paiper.config import load_profile
    from paiper.state import DisplayState

    _, home, root, builds_runner, _, _ = installation
    config, state, clock, transfers = panel
    if outcome == "reuse-no-access":
        config.write_text("panel:\n  confirmed: true\n")
    with DisplayState(state):
        pass
    if outcome == "skip":
        candidate.operate("calibrate", config, state)
        clock[0] += 301
        transfers.clear()
    if outcome == "failure":

        def broken(_self, _data):
            raise OSError("fixture transfer failed")

        monkeypatch.setattr(display.WaveshareDriver, "display", broken)
    vendor = home / "custom-vendor"
    package = vendor / "waveshare_epd"
    package.mkdir(parents=True)
    for name in ("__init__.py", "epd4in0e.py", "epdconfig.py"):
        (package / name).write_text(
            "raise AssertionError('do not import to detect hardware')\n"
        )
    before = config.read_bytes()
    monkeypatch.setattr(setup, "device_access", lambda: outcome != "reuse-no-access")
    os_runner = OSRunner(groups=["spi", "gpio"])
    timers = {"display": False, "update": False}
    prompts, actions = [], []

    def prompt(question):
        prompts.append(question)
        if "Chromium OS" in question or "enable-linger" in question:
            return "n"
        if "six-color E6 label" in question and outcome == "declined-label":
            return "n"
        return "y"

    def runner(argv, **kwargs):
        if argv[0] in ("dpkg-query", "id", "raspi-config") or (
            argv[0] == "/usr/bin/python3" and "-c" in argv
        ):
            return os_runner(argv, **kwargs)
        if "-c" in argv and "PAIPER_SETUP_RESULT" in argv[2]:
            action, config_arg, state_arg, rotation, digest = argv[-5:]
            assert config_arg == str(config) and state_arg == str(state)
            shared = setup.module("manager")
            with pytest.raises(shared.Busy), shared.lock(root / "lifecycle.lock"):
                pass
            if action == "calibrate":
                assert any("six-color E6 label" in q for q in prompts)
                assert kwargs["env"]["GPIOZERO_PIN_FACTORY"] == "lgpio"
                assert kwargs["timeout"] is None
            actions.append(action)
            try:
                result = candidate.operate(
                    action, Path(config_arg), Path(state_arg), int(rotation), digest
                )
            except OSError as error:
                raise subprocess.CalledProcessError(
                    1, ["candidate"], stderr=str(error).encode()
                ) from error
            code = 3 if result.get("transferred") is False else 0
            return subprocess.CompletedProcess(
                argv, code, ("PAIPER_SETUP_RESULT=" + json.dumps(result)).encode(), b""
            )
        if argv[0] == "systemctl":
            purpose = (
                "display" if any("paiper-display" in arg for arg in argv) else "update"
            )
            if "is-enabled" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, b"enabled" if timers[purpose] else b"disabled", b""
                )
            if "is-active" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, b"active" if timers[purpose] else b"inactive", b""
                )
            if "show" in argv:
                dropin = (
                    home
                    / ".config/systemd/user/paiper-display.service.d/10-paiper-gpio.conf"
                )
                output = (
                    b"GPIOZERO_PIN_FACTORY=lgpio"
                    if "--property=Environment" in argv and dropin.exists()
                    else b""
                )
                return subprocess.CompletedProcess(argv, 0, output, b"")
            if "enable" in argv:
                if purpose == "display":
                    assert load_profile(config).confirmed is True
                    assert outcome == "success"
                    assert "confirm" in actions
                timers[purpose] = True
        return builds_runner(argv, **kwargs)

    result = setup.main(
        ["--config", str(config), "--state", str(state), "--vendor-lib", str(vendor)],
        runner=runner,
        prompt=prompt,
        probe=supported,
    )
    assert result == (1 if outcome in ("failure", "reuse-no-access") else 0)
    assert timers["display"] is (outcome == "success")
    if outcome != "success":
        assert config.read_bytes() == before
        assert "confirm" not in actions
    if outcome == "declined-label":
        assert "calibrate" not in actions and transfers == []
    if outcome == "failure":
        with DisplayState(state) as database:
            row = database.status()
            assert row["last_attempt_at"] is not None
            assert row["last_success_at"] is None
    assert not any(cmd[0] == "sudo" for cmd in os_runner.commands)
