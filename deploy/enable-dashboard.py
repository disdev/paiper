"""Explicitly upgrade an existing device's stable controller and prefer dashboard.

Run from the healthy current release after the normal protected-deploy update.
This never provisions dependencies, starts services, or touches display hardware.
"""

import argparse
import importlib.util
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent


def module(name):
    spec = importlib.util.spec_from_file_location(
        f"paiper_dashboard_{name}", SOURCE / f"{name}.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def check_provenance(manager, shared):
    path = manager.root / "setup-choices.json"
    if not os.path.lexists(path):
        return  # Old manual installs have no first-use permission to consume.
    saved = shared.read_json(path)
    if (
        not isinstance(saved, dict)
        or set(saved)
        not in (
            {"config", "state", "vendor_lib"},
            {"config", "state", "vendor_lib", "state_phase"},
        )
        or saved.get("state_phase", "required") != "required"
        or any(saved[key] != manager.settings[key] for key in ("config", "state"))
    ):
        raise shared.DeploymentError(
            "Conflicting or unfinished safety state provenance; preserve files for operator review"
        )


def enable(root, *, runner=subprocess.run):
    shared, installer = module("manager"), module("install")
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise shared.DeploymentError(
            "Existing installation not found; run setup for first use"
        )
    # Re-read settings only after excluding installers, updates and display owners.
    with (
        shared.lock(root / "install.lock"),
        shared.lock(root / "update.lock"),
        shared.lock(root / "lifecycle.lock"),
    ):
        manager = shared.Manager(root, runner=runner)
        state = Path(manager.settings["state"]).resolve()
        with shared.lock(state.with_name(state.name + ".lock")):
            shared.validate_state(state)
            check_provenance(manager, shared)
            status = manager.status()
            sha = status["current"]
            if not sha or status["activation_pending"] or sha in status["failed"]:
                raise shared.DeploymentError(
                    "No healthy settled current release; resolve manager status/update first"
                )
            release = manager._release(sha)
            if SOURCE.resolve() != release / "app/deploy":
                raise shared.DeploymentError(
                    "Run current/app/deploy/enable-dashboard.py from the managed current release"
                )
            if manager.settings["branch"] != "deploy":
                raise shared.DeploymentError(
                    "Activation requires the protected deploy branch"
                )
            if "dashboard" not in shared.app_scenes(release):
                raise shared.DeploymentError(
                    "Current release is legacy; update to a dashboard-capable deploy first"
                )
            if manager._fetch() != sha:
                raise shared.DeploymentError(
                    "Current release is not the latest deploy commit; run manager update, "
                    "resolve busy/known-failed status, then retry activation"
                )
            config = Path(manager.settings["config"])
            if config.is_symlink() or not config.is_file():
                raise shared.DeploymentError(
                    "Existing calibrated configuration must be a regular file"
                )
            manager._execute(
                [
                    release / ".venv/bin/python",
                    "-I",
                    "-c",
                    installer.CONFIRMATION_CHECK,
                    config,
                ],
                timeout=60,
                release=release,
                label="Confirmed panel validation",
            )
            with manager._scratch() as scratch:
                snapshot = manager._snapshot(scratch / "state.sqlite3")
                manager._health(release, snapshot)

            controller = root / "manager.py"
            if controller.is_symlink() or not controller.is_file():
                raise shared.DeploymentError(
                    "Existing stable manager must be a regular file"
                )
            # Preserve originals once. All renames and parent directories are fsynced.
            for source, backup in (
                (root / "settings.json", root / "settings.before-dashboard.json"),
                (controller, root / "manager.before-dashboard.py"),
            ):
                if os.path.lexists(backup):
                    if backup.is_symlink() or not backup.is_file():
                        raise shared.DeploymentError(
                            f"Unsafe activation backup: {backup.name}"
                        )
                else:
                    installer._copy_atomic(source, backup)
            installer._copy_atomic(SOURCE / "manager.py", controller)
            # A crash before this rename leaves a new controller with the old scene.
            # A crash after it leaves the complete new pair. Rerun is safe in either case.
            if manager.settings["scene"] != "dashboard":
                shared.write_json(
                    root / "settings.json", {**manager.settings, "scene": "dashboard"}
                )
    return "enabled"


def main(argv=None, *, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/share/paiper"
    )
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print(
            "Dashboard activation error: run as a non-root user, not sudo.",
            file=sys.stderr,
        )
        return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    shared = module("manager")
    try:
        enable(args.root, runner=runner)
    except (
        RuntimeError,
        ValueError,
        OSError,
        sqlite3.Error,
        KeyboardInterrupt,
    ) as error:
        print(
            "Dashboard activation error: "
            + shared.sanitize_diagnostic(str(error) or "Interrupted; rerun safely")
            + ". No services started; fix the reported cause and rerun.",
            file=sys.stderr,
        )
        return 1
    print(
        "Dashboard enabled. Stable controller upgraded; existing timer will use the "
        "next safe slot. No hardware operation or scheduling change was performed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
