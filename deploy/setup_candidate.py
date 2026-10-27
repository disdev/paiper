"""Run only with the healthy candidate interpreter, under setup's lifecycle lock."""

import fcntl
import hashlib
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from paiper.config import PanelProfile, load_profile
from paiper.display import render_and_display
from paiper.render import render_scene
from paiper.state import ClockError, DisplayState, IncompleteAttemptError


def profile_bytes(config, expected=""):
    if config.is_symlink() or not config.is_file():
        raise ValueError("Configuration must be a regular file")
    content = config.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if expected and digest != expected:
        raise ValueError(
            "Configuration changed during setup; rerun without overwriting it"
        )
    return content, digest


def initialize(config, state, choices_path):
    if choices_path is None:
        raise ValueError("State initialization requires saved first-use consent")
    with choices_path.with_name(choices_path.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if choices_path.is_symlink() or not choices_path.is_file():
            raise ValueError("Invalid state initialization provenance")
        saved = json.loads(choices_path.read_text())
        if (
            saved.get("state_phase") != "pending"
            or saved.get("state") != str(state)
            or saved.get("config") != str(config)
        ):
            raise ValueError(
                "State initialization permission absent or already consumed"
            )

        def persist(phase):
            staging = choices_path.with_name("." + choices_path.name + uuid4().hex)
            try:
                with staging.open("x") as stream:
                    json.dump({**saved, "state_phase": phase}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(staging, choices_path)
                sync_directory(choices_path.parent)
            finally:
                staging.unlink(missing_ok=True)

        # Durable consumption precedes even opening SQLite. An ambiguous crash
        # must never turn back into permission to create an empty history.
        existed = state.exists() or state.is_symlink()
        persist("initializing")
        with DisplayState(state, require_existing=existed) as database:
            if database.status()["in_progress"]:
                raise IncompleteAttemptError(
                    "Interrupted hardware attempt; never reset state"
                )
        sync_directory(state.parent)
        persist("required")
    return {"initialized": True}


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def operate(action, config, state, rotation=270, expected="", *, choices_path=None):
    content, digest = profile_bytes(config, expected)
    profile = load_profile(config)
    if action == "initialize":
        return initialize(config, state, choices_path)
    if action == "profile":
        return {
            "rotation": profile.rotation,
            "confirmed": profile.confirmed,
            "digest": digest,
        }
    if action == "calibrate":
        transferred = render_and_display(
            lambda: render_scene("calibration", datetime.now(UTC)),
            PanelProfile(rotation=rotation, confirmed=True),
            state,
            require_existing_state=True,
        )
        return {"transferred": transferred}
    with DisplayState(state, require_existing=True) as database:
        row = database.status()
        if row["in_progress"]:
            raise IncompleteAttemptError(
                "Interrupted hardware attempt; inspect hardware, never reset state"
            )
        if action == "remaining":
            now = time.time()
            if (
                not math.isfinite(now)
                or now < 0
                or (row["clock_at"] is not None and now < row["clock_at"])
            ):
                raise ClockError("Clock moved backwards; refusing calibration")
            remaining = (
                max(0, math.ceil(row["cooldown_at"] + 300 - now))
                if row["cooldown_at"] is not None
                else 0
            )
            return {"seconds": remaining}
        if action != "confirm":
            raise ValueError("Unknown setup action")
        content, _ = profile_bytes(config, expected)
        staging = config.with_name(f".{config.name}.setup-{uuid4().hex}")
        backup = config.with_name(f"{config.name}.setup-{uuid4().hex}.bak")
        try:
            with staging.open("x", encoding="utf-8") as stream:
                stream.write(f"panel:\n  rotation: {rotation}\n  confirmed: true\n")
                stream.flush()
                os.fsync(stream.fileno())
            staging.chmod(0o600)
            load_profile(staging)
            with backup.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            backup.chmod(0o600)
            profile_bytes(config, expected)
            os.replace(staging, config)
            descriptor = os.open(config.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            staging.unlink(missing_ok=True)
        return {"confirmed": True, "rotation": rotation, "backup": str(backup)}


if __name__ == "__main__":
    result = operate(
        sys.argv[2],
        Path(sys.argv[3]),
        Path(sys.argv[4]),
        int(sys.argv[5]),
        sys.argv[6],
        choices_path=Path(sys.argv[1]),
    )
    print("PAIPER_SETUP_RESULT=" + json.dumps(result))
    raise SystemExit(3 if result.get("transferred") is False else 0)
