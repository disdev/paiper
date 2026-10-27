"""Explicit migration uses real files, Git, flock and candidate config/status."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import test_deployment as deployment_tests
from test_deployment import advance, git

module = deployment_tests.module
prepared = deployment_tests.prepared

SOURCE = Path(__file__).resolve().parents[1] / "deploy"


@pytest.fixture
def activation():
    path = SOURCE / "enable-dashboard.py"
    assert path.is_file(), "Explicit existing-device activation is missing"
    spec = importlib.util.spec_from_file_location("enable_dashboard", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def existing(prepared, activation, monkeypatch):
    manager, runner, _, settings = prepared
    shutil.copytree(
        SOURCE, runner.repo / "deploy", ignore=shutil.ignore_patterns("__pycache__")
    )
    git(runner.repo, "add", ".")
    git(runner.repo, "commit", "-m", "dashboard capable release")
    manager.update()
    release = manager.root / "current"
    monkeypatch.setattr(activation, "SOURCE", (release / "app/deploy").resolve())
    Path(settings["config"]).write_text(
        "# calibrated panel\npanel:\n  confirmed: true\n  rotation: 90\n"
    )
    (manager.root / "manager.py").write_bytes(b"# old stable manager: stillness only\n")
    vendor = runner.repo.parent / "vendor"
    vendor.mkdir()
    (vendor / "untouched.py").write_text("# vendor provenance\n")
    settings["vendor_lib"] = str(vendor)
    (manager.root / "settings.json").write_text(json.dumps(settings))
    (manager.root / "setup-choices.json").write_text(
        json.dumps(
            {
                "config": settings["config"],
                "state": settings["state"],
                "vendor_lib": str(vendor),
                "state_phase": "required",
            }
        )
    )
    (manager.root / "timer.fixture").write_bytes(b"existing cadence and enablement")
    runner.calls.clear()
    return manager, runner, settings


def preserved(existing):
    manager, _, settings = existing
    paths = [
        Path(settings["config"]),
        Path(settings["state"]),
        manager.root / "setup-choices.json",
        manager.root / "timer.fixture",
        Path(settings["vendor_lib"]) / "untouched.py",
    ]
    return {path: path.read_bytes() for path in paths}


def test_activation_replaces_controller_before_scene_and_is_idempotent(
    existing,
    activation,
    monkeypatch,
):
    manager, runner, settings = existing
    before = preserved(existing)
    old_settings = (manager.root / "settings.json").read_bytes()
    assert activation.enable(manager.root, runner=runner) == "enabled"
    assert json.loads((manager.root / "settings.json").read_text()) == {
        **settings,
        "scene": "dashboard",
    }
    assert (manager.root / "manager.py").read_bytes() == (
        SOURCE / "manager.py"
    ).read_bytes()
    assert (
        manager.root / "manager.before-dashboard.py"
    ).read_bytes() == b"# old stable manager: stillness only\n"
    assert (
        manager.root / "settings.before-dashboard.json"
    ).read_bytes() == old_settings
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not any(
        any(
            x in argv
            for x in ("display", "systemctl", "pip", "playwright", "venv", "sudo")
        )
        for argv, _ in runner.calls
    )
    assert activation.enable(manager.root, runner=runner) == "enabled"
    assert (
        manager.root / "settings.before-dashboard.json"
    ).read_bytes() == old_settings
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize(
    "locked", ["install.lock", "update.lock", "lifecycle.lock", "state"]
)
def test_activation_busy_is_failure_without_changes(
    existing, activation, module, locked
):
    manager, runner, settings = existing
    path = (
        manager.root / locked
        if locked != "state"
        else Path(settings["state"] + ".lock")
    )
    before = (manager.root / "settings.json").read_bytes()
    controller = (manager.root / "manager.py").read_bytes()
    with module.lock(path), pytest.raises(RuntimeError, match="Locked:"):
        activation.enable(manager.root, runner=runner)
    assert (manager.root / "settings.json").read_bytes() == before
    assert (manager.root / "manager.py").read_bytes() == controller
    assert runner.calls == []


@pytest.mark.parametrize(
    "broken",
    [
        "missing-state",
        "corrupt-state",
        "interrupted-state",
        "unconfirmed",
        "bad-config",
        "bad-settings",
        "legacy",
        "bad-manifest",
        "failed",
        "pending",
        "provenance",
        "stale-release",
        "wrong-branch",
    ],
)
def test_invalid_existing_device_never_activates(existing, activation, broken):
    manager, runner, settings = existing
    root = manager.root
    if broken == "missing-state":
        Path(settings["state"]).unlink()
    elif broken == "corrupt-state":
        Path(settings["state"]).write_bytes(b"not sqlite")
    elif broken == "interrupted-state":
        import sqlite3

        with sqlite3.connect(settings["state"]) as db:
            db.execute("UPDATE display_state SET in_progress = 1")
    elif broken in ("unconfirmed", "bad-config"):
        Path(settings["config"]).write_text(
            "panel:\n  confirmed: false\n"
            if broken == "unconfirmed"
            else "panel: nope\n"
        )
    elif broken in ("bad-settings", "wrong-branch"):
        settings["scene" if broken == "bad-settings" else "branch"] = "unknown"
        (root / "settings.json").write_text(json.dumps(settings))
    elif broken == "legacy":
        (root / "current/app/deploy/app-capabilities.json").unlink()
    elif broken == "bad-manifest":
        (root / "current/app/deploy/app-capabilities.json").write_text(
            '{"version":true,"scenes":["dashboard"]}'
        )
    elif broken == "failed":
        manager._record_failure(manager._pointer("current"), "failed candidate")
    elif broken == "pending":
        (root / "activation.json").write_text("{}")
    elif broken == "provenance":
        choices = root / "setup-choices.json"
        data = json.loads(choices.read_text())
        data["state_phase"] = "initializing"
        choices.write_text(json.dumps(data))
    elif broken == "stale-release":
        advance(runner)
    before = (root / "settings.json").read_bytes()
    controller = (root / "manager.py").read_bytes()
    with pytest.raises(RuntimeError):
        activation.enable(root, runner=runner)
    assert (root / "settings.json").read_bytes() == before
    assert (root / "manager.py").read_bytes() == controller


@pytest.mark.parametrize("stage", ["controller", "settings", "after-settings"])
def test_interrupted_atomic_replacement_is_safe_and_retryable(
    existing, activation, monkeypatch, stage
):
    manager, runner, _ = existing
    old_controller = (manager.root / "manager.py").read_bytes()
    old_settings = (manager.root / "settings.json").read_bytes()
    real_replace = activation.os.replace

    def interrupt(source, destination):
        target = "manager.py" if stage == "controller" else "settings.json"
        if Path(destination) == manager.root / target:
            if stage != "controller":
                assert (manager.root / "manager.py").read_bytes() == (
                    SOURCE / "manager.py"
                ).read_bytes()
            if stage == "after-settings":
                real_replace(source, destination)
            raise OSError("simulated power loss at atomic rename")
        return real_replace(source, destination)

    monkeypatch.setattr(activation.os, "replace", interrupt)
    with pytest.raises(OSError, match="power loss"):
        activation.enable(manager.root, runner=runner)
    assert json.loads((manager.root / "settings.json").read_text())["scene"] == (
        "dashboard" if stage == "after-settings" else "stillness"
    )
    assert (manager.root / "manager.py").read_bytes() == (
        old_controller
        if stage == "controller"
        else (SOURCE / "manager.py").read_bytes()
    )
    monkeypatch.setattr(activation.os, "replace", real_replace)
    assert activation.enable(manager.root, runner=runner) == "enabled"
    assert (manager.root / "manager.before-dashboard.py").read_bytes() == old_controller
    assert (
        manager.root / "settings.before-dashboard.json"
    ).read_bytes() == old_settings


def test_activation_main_rejects_root_and_reports_busy(
    existing, activation, module, monkeypatch, capsys
):
    manager, runner, _ = existing
    monkeypatch.setattr(activation.os, "geteuid", lambda: 0)
    assert activation.main(["--root", str(manager.root)], runner=runner) == 1
    assert "non-root" in capsys.readouterr().err
    monkeypatch.setattr(activation.os, "geteuid", lambda: 1000)
    with module.lock(manager.root / "update.lock"):
        assert activation.main(["--root", str(manager.root)], runner=runner) == 1
    assert "Locked: update.lock" in capsys.readouterr().err


def test_activation_interruption_reports_safe_retry(activation, monkeypatch, capsys):
    monkeypatch.setattr(activation.os, "geteuid", lambda: 1000)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(activation, "enable", interrupt)
    assert activation.main([]) == 1
    assert "Interrupted; rerun safely" in capsys.readouterr().err


@pytest.mark.parametrize("unsupported", [False, True])
def test_activation_real_candidate_cli_offline_scratch(
    existing, activation, unsupported, monkeypatch
):
    """No preview stub: run the candidate package with installed local Chromium."""
    manager, runner, settings = existing
    release = (manager.root / "current").resolve()
    shutil.copytree(SOURCE.parent / "src", release / "app/src")
    user_state = runner.repo.parent / "user-state"
    user_config = runner.repo.parent / "user-config"
    user_state.mkdir()
    user_config.mkdir()
    (user_state / "sentinel").write_bytes(b"do not touch cache or its locks")
    # This must not be loaded by the isolated smoke.
    (Path(settings["config"]).parent / "dashboard.yaml").write_text(
        "invalid: preferences\n"
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(user_state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user_config))
    browser_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    preview_outputs = []
    real_before = Path(settings["state"]).read_bytes()
    controller = (manager.root / "manager.py").read_bytes()
    if unsupported:
        # The candidate declares support but the actual CLI cannot render dashboard.
        cli = release / "app/src/paiper/cli.py"
        cli.write_text(
            cli.read_text().replace(
                'if args.scene == "dashboard":',
                'if args.scene == "dashboard":\n            raise ValueError("dashboard not supported")',
            )
        )

    def real_preview(argv, **kwargs):
        if "preview" not in argv:
            return runner(argv, **kwargs)
        env = {**kwargs["env"], "PYTHONPATH": str(release / "app/src")}
        if browser_path is None:
            env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            env["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
        result = subprocess.run(
            [sys.executable, *argv[1:]],
            check=kwargs.pop("check"),
            **{**kwargs, "env": env},
        )
        preview_outputs.append(Path(argv[argv.index("--output") + 1]).read_bytes())
        return result

    if unsupported:
        with pytest.raises(RuntimeError, match="dashboard not supported"):
            activation.enable(manager.root, runner=real_preview)
        assert (manager.root / "manager.py").read_bytes() == controller
        assert (
            json.loads((manager.root / "settings.json").read_text())["scene"]
            == "stillness"
        )
    else:
        assert activation.enable(manager.root, runner=real_preview) == "enabled"
        assert len(preview_outputs) == 6
        assert all(image.startswith(b"\x89PNG") for image in preview_outputs)
    assert Path(settings["state"]).read_bytes() == real_before
    assert list(user_config.iterdir()) == []
    assert [path.name for path in user_state.iterdir()] == ["sentinel"]
    assert list(manager.work.iterdir()) == []


@pytest.mark.parametrize("case", ["available", "missing-script", "update-fails"])
def test_documented_short_command_updates_before_activation(tmp_path, case):
    """Execute the delivered shell structure against controlled updater/entrypoints."""
    root = tmp_path / ".local/share/paiper"
    source = root / "current/app/deploy"
    source.mkdir(parents=True)
    (root / "manager.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "assert sys.argv[1:] == ['update']\n"
        "Path(__file__).with_name('updated').write_text('updated')\n"
        f"raise SystemExit({int(case == 'update-fails')})\n"
    )
    if case != "missing-script":
        (source / "enable-dashboard.py").write_text(
            "from pathlib import Path\n"
            "root = Path.home() / '.local/share/paiper'\n"
            "assert (root / 'updated').is_file()\n"
            "(root / 'enabled').write_text('enabled')\n"
        )
    readme = (SOURCE.parent / "README.md").read_text()
    command = next(
        block.split("```")[0]
        for block in readme.split("```sh\n")
        if block.startswith('(p="$HOME/.local/share/paiper";')
    )
    result = subprocess.run(
        ["sh", "-c", command.replace("/usr/bin/python3", sys.executable)],
        env={**os.environ, "HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if case == "available" else 1)
    assert (root / "updated").is_file()
    assert (root / "enabled").exists() == (case == "available")
    if case == "missing-script":
        assert "Dashboard activation unavailable" in result.stderr
