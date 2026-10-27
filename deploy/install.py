"""Install the stable, non-root Paiper manager and opt-in user timers."""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

SOURCE = Path(__file__).resolve().parent
PYTHON = "/usr/bin/python3"
CONFIRMATION_CHECK = """
import sys
from pathlib import Path
from paiper.config import load_profile
if not load_profile(Path(sys.argv[1])).confirmed:
    raise ValueError('panel.confirmed must be true after physical calibration')
"""


def _manager_module():
    spec = importlib.util.spec_from_file_location(
        "paiper_stable_manager", SOURCE / "manager.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _xdg_path(name, fallback):
    value = os.environ.get(name, "")
    return Path(value) if value and Path(value).is_absolute() else fallback


def _copy_atomic(source, destination):
    if destination.is_symlink():
        raise ValueError(f"Refusing symlink: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid4().hex}")
    try:
        with source.open("rb") as incoming, staging.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        staging.chmod(0o600)
        os.replace(staging, destination)
        fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        staging.unlink(missing_ok=True)


def _check(runner, argv, *, cwd=None, env=None, timeout=60):
    return runner(
        [str(item) for item in argv],
        check=True,
        capture_output=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


def _install(args, runner, manager_module):
    home = Path.home()
    root = home / ".local/share/paiper"
    manager_module.Manager._directory(root)
    with manager_module.lock(root / "install.lock"):
        config = _xdg_path("XDG_CONFIG_HOME", home / ".config") / "paiper/config.yaml"
        state = (
            _xdg_path("XDG_STATE_HOME", home / ".local/state")
            / "paiper/display.sqlite3"
        )
        if args.config_path:
            config = args.config_path.expanduser().resolve()
            if not config.is_file():
                raise ValueError("--config-path must name an existing configuration")
        if args.state:
            state = args.state.expanduser().resolve()
        settings_path = root / "settings.json"
        first_install = not os.path.lexists(settings_path)
        if not first_install:
            settings = manager_module.read_json(settings_path)
            # Manager validates the full stored contract before we use its paths.
            manager = manager_module.Manager(root, runner=runner)
            config = Path(settings["config"])
            for value, key in ((args.config_path, "config"), (args.state, "state")):
                if value and str(value.expanduser().resolve()) != settings[key]:
                    raise ValueError(
                        f"Existing {key} differs; settings were not changed"
                    )
            if (
                args.vendor_lib is not None
                and str(args.vendor_lib.resolve()) != settings["vendor_lib"]
            ):
                raise ValueError(
                    "Existing vendor_lib differs; settings.json was not changed"
                )
        else:
            settings = {
                "version": 1,
                "repository": "https://github.com/disdev/paiper.git",
                "branch": "deploy",
                "python": PYTHON,
                "config": str(config.resolve()),
                "state": str(state.resolve()),
                "vendor_lib": str(args.vendor_lib.resolve())
                if args.vendor_lib
                else None,
                "scene": "stillness",
            }
            for key in ("config", "state", "vendor_lib"):
                if settings[key] and Path(settings[key]).is_relative_to(
                    root / "releases"
                ):
                    raise ValueError(f"{key} must stay outside managed releases")
            with settings_path.open("x", encoding="utf-8") as stream:
                json.dump(settings, stream, indent=2)
                stream.write("\n")
            settings_path.chmod(0o600)
            manager = manager_module.Manager(root, runner=runner)

        if config.is_symlink():
            raise ValueError("Configuration must not be a symlink")
        source = (
            args.config.resolve()
            if args.config
            else SOURCE.parent / "config.example.yaml"
        )
        if config.exists():
            if args.config and config.read_bytes() != source.read_bytes():
                raise ValueError(
                    "Existing configuration differs; it was not overwritten"
                )
        else:
            _copy_atomic(source, config)

        if settings["python"] != PYTHON:
            _check(
                runner,
                [
                    settings["python"],
                    "-I",
                    "-c",
                    "import sys; assert sys.version_info >= (3, 11)",
                ],
            )
        _copy_atomic(SOURCE / "manager.py", root / "manager.py")
        outcome = manager.update()
        status = manager.status()
        if not status["current"] or status["activation_pending"] or outcome == "busy":
            raise ValueError(
                "No available healthy active release; timers were not enabled"
            )

        if first_install:
            with manager_module.lock(root / "update.lock"), manager._activation_locks():
                if manager._journal_pending():
                    raise ValueError(
                        "Pending activation; first-install scene was not changed"
                    )
                release = manager._release(manager._pointer("current"))
                if "dashboard" in manager_module.app_scenes(release):
                    settings["scene"] = "dashboard"
                    manager_module.write_json(settings_path, settings)

        if args.enable_display:
            release = root / "releases" / status["current"]
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("PYTHON", "GIT_")) and key != "VIRTUAL_ENV"
            }
            env.update(
                TMPDIR=str(root / "work"),
                TMP=str(root / "work"),
                TEMP=str(root / "work"),
            )
            _check(
                runner,
                [release / ".venv/bin/python", "-I", "-c", CONFIRMATION_CHECK, config],
                cwd=release / "app",
                env=env,
            )
            vendor = settings["vendor_lib"]
            if not vendor or not all(
                (Path(vendor) / "waveshare_epd" / name).is_file()
                for name in ("epd4in0e.py", "epdconfig.py")
            ):
                raise ValueError(
                    "Display needs an installed external --vendor-lib (python/lib)"
                )

        units = _xdg_path("XDG_CONFIG_HOME", home / ".config") / "systemd/user"
        for purpose in ("update", "display"):
            for kind in ("service", "timer"):
                name = f"paiper-{purpose}.{kind}"
                _copy_atomic(SOURCE / "systemd" / name, units / name)
        _check(runner, ["systemctl", "--user", "daemon-reload"])
        if not args.no_update_timer:
            _check(
                runner,
                ["systemctl", "--user", "enable", "--now", "paiper-update.timer"],
            )
        if args.enable_display:
            _check(
                runner,
                ["systemctl", "--user", "enable", "--now", "paiper-display.timer"],
            )
        print(f"Paiper installed at {root}.")
        print(
            "Update timer was not changed."
            if args.no_update_timer
            else "Update timer enabled."
        )
        if not args.enable_display:
            print("Display timer was not changed; first installs leave it disabled.")
        return 0


def main(argv=None, *, runner=subprocess.run):
    if os.geteuid() == 0:
        print(
            "Installation error: run as a non-root user; do not use sudo.",
            file=sys.stderr,
        )
        return 1
    if sys.version_info < (3, 11):  # noqa: UP036 -- standalone bootstrap may use an older Python
        print("Installation error: use distro Python 3.11 or newer.", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, help="Copy a calibrated config only if absent"
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        help="Use an existing config in place on first install",
    )
    parser.add_argument(
        "--state",
        type=Path,
        help="Original manual display state path, first install only",
    )
    parser.add_argument(
        "--no-update-timer",
        action="store_true",
        help="Prepare release and units without changing update timer scheduling",
    )
    parser.add_argument(
        "--vendor-lib", type=Path, help="Persistent Waveshare python/lib directory"
    )
    parser.add_argument(
        "--enable-display",
        action="store_true",
        help="Opt in after physical calibration",
    )
    args = parser.parse_args(argv)
    manager_module = _manager_module()
    try:
        _check(
            runner,
            [PYTHON, "-I", "-c", "import sys; assert sys.version_info >= (3, 11)"],
        )
        return _install(args, runner, manager_module)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        print(
            f"Installation error: {manager_module.command_failure('Installer command', error)}",
            file=sys.stderr,
        )
    except (
        ValueError,
        manager_module.DeploymentError,
    ) as error:
        print(
            f"Installation error: {manager_module.sanitize_diagnostic(error)}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
