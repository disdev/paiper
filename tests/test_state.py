import sqlite3
import subprocess
import sys

import pytest

from paiper.state import (
    ClockError,
    DisplayState,
    IncompleteAttemptError,
    StateCorruptError,
    StateLockedError,
)


def test_failed_attempt_still_reserves_cooldown(tmp_path):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        assert state.reserve(1000, "first")
    with DisplayState(path) as state:
        assert not state.reserve(1299, "second")
        assert state.reserve(1300, "second")
        assert state.status()["last_success_at"] is None


def test_success_is_persistent_and_unchanged_is_logged(tmp_path, caplog):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        assert state.reserve(1000, "first")
        state.succeed(1100, "first")
    with DisplayState(path) as state, caplog.at_level("INFO"):
        assert state.status()["last_success_at"] == 1100
        assert state.status()["last_success_digest"] == "first"
        assert not state.reserve(2000, "first")
        assert "unchanged" in caplog.text


@pytest.mark.parametrize("success", [False, True])
def test_cooldown_starts_at_attempt_completion(tmp_path, success, caplog):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        assert state.reserve(1000, "first")
        state.begin_attempt()
        if success:
            state.succeed(1500, "first")
        else:
            state.finish_attempt(1500)
    with DisplayState(path) as state, caplog.at_level("INFO"):
        assert not state.reserve(1799, "second")
        assert "cooldown" in caplog.text
        assert state.reserve(1800, "second")


def test_backwards_clock_is_rejected_even_after_an_unchanged_skip(tmp_path):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "first")
        state.succeed(1100, "first")
        assert not state.reserve(2000, "first")
    with DisplayState(path) as state, pytest.raises(ClockError):
        state.reserve(1400, "second")


def test_interrupted_hardware_attempt_fails_closed_across_restarts(tmp_path):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "first")
        state.begin_attempt()
    with DisplayState(path) as state:
        assert state.status()["in_progress"]
        with pytest.raises(IncompleteAttemptError):
            state.reserve(999999, "second")


def test_reservation_is_committed_before_hardware(tmp_path):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "first")
        with sqlite3.connect(path) as reader:
            assert reader.execute(
                "SELECT last_attempt_at, last_attempt_digest FROM display_state"
            ).fetchone() == (1000, "first")


def test_lock_is_nonblocking_and_released_after_exceptions(tmp_path):
    path = tmp_path / "state.sqlite3"
    with pytest.raises(RuntimeError, match="primary"), DisplayState(path):
        with pytest.raises(StateLockedError), DisplayState(path):
            pytest.fail("acquired an occupied lock")
        raise RuntimeError("primary")
    with DisplayState(path) as state:
        assert state.reserve(1000, "first")


@pytest.mark.parametrize("contents", [b"", b"not sqlite"])
def test_existing_invalid_database_is_not_replaced(tmp_path, contents):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(contents)
    with pytest.raises(StateCorruptError), DisplayState(path):
        pass
    assert path.read_bytes() == contents


@pytest.mark.parametrize(
    "tamper",
    [
        "DELETE FROM display_state",
        "UPDATE display_state SET cooldown_at = -1",
        "UPDATE display_state SET last_attempt_digest = NULL",
        "UPDATE display_state SET last_success_at = 5",
        "PRAGMA user_version = 99",
        "DROP TABLE display_state",
    ],
)
def test_corrupted_state_is_rejected_without_reset(tmp_path, tamper):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "first")
    with sqlite3.connect(path) as connection:
        connection.execute(tamper)
    with pytest.raises(StateCorruptError), DisplayState(path):
        pass
    # Failed __enter__ must release its lock as well.
    with pytest.raises(StateCorruptError), DisplayState(path):
        pass


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1, True])
def test_invalid_clock_values_never_reserve(tmp_path, now):
    with DisplayState(tmp_path / "state.sqlite3") as state:
        with pytest.raises((ValueError, ClockError)):
            state.reserve(now, "first")
        assert state.status()["last_attempt_at"] is None


def test_success_requires_matching_reservation(tmp_path):
    with DisplayState(tmp_path / "state.sqlite3") as state:
        with pytest.raises(RuntimeError):
            state.succeed(1000, "first")
        state.reserve(1000, "first")
        with pytest.raises(RuntimeError):
            state.succeed(1100, "different")
        with pytest.raises(ClockError):
            state.succeed(900, "first")
        assert state.status()["last_success_at"] is None


def test_failed_later_frame_makes_old_success_uncertain(tmp_path):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "first")
        state.succeed(1100, "first")
    with DisplayState(path) as state:
        state.reserve(1400, "second")
        state.begin_attempt()
        state.finish_attempt(1500)
    with DisplayState(path) as state:
        # A failed transfer may have partially changed the physical screen.
        assert state.reserve(1800, "first")
        assert state.status()["last_success_digest"] == "first"


def test_lock_is_exclusive_in_an_independent_process(tmp_path):
    path = tmp_path / "state.sqlite3"
    script = """
import sys
from pathlib import Path
from paiper.state import DisplayState, StateLockedError
try:
    with DisplayState(Path(sys.argv[1])):
        pass
except StateLockedError:
    sys.exit(23)
"""
    with DisplayState(path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(path)], timeout=10, check=False
        )
    assert result.returncode == 23


def test_killed_process_cannot_leave_an_unprotected_refresh(tmp_path):
    path = tmp_path / "state.sqlite3"
    script = """
import os
import sys
from pathlib import Path
from paiper.state import DisplayState
with DisplayState(Path(sys.argv[1])) as state:
    state.reserve(1000, "first")
    state.begin_attempt()
    os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], timeout=10, check=False
    )
    assert result.returncode == 23
    with DisplayState(path) as state, pytest.raises(IncompleteAttemptError):
        state.reserve(999999, "second")
