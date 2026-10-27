"""Durable refresh protection; hold one context across the entire GPIO lifecycle.

All callers for a physical panel must use the same persistent state path. Never
delete the database or its sibling lock to bypass a cooldown. An interrupted
hardware attempt is deliberately fail-closed: its physical completion time is
unknown, so an operator must inspect the hardware before recovering its state.
"""

import fcntl
import logging
import math
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)
MIN_REFRESH_INTERVAL = 300


class StateLockedError(RuntimeError):
    """Another process owns the display lifecycle."""


class StateCorruptError(RuntimeError):
    """Persistent safety metadata cannot be trusted."""


class ClockError(ValueError):
    """The clock is invalid or moved backwards."""


class IncompleteAttemptError(RuntimeError):
    """A previous hardware attempt has no recorded completion."""


def _timestamp(value: float) -> None:
    if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
        raise ClockError("Display time must be finite and nonnegative")


class DisplayState:
    """Exclusive, nonblocking state transaction with separately durable writes.

    ``reserve`` commits before any hardware access. ``begin_attempt`` marks the
    start of hardware access durably. Always call ``finish_attempt`` on failure,
    or ``succeed`` on success, with a conservative end-of-lifecycle timestamp.
    A reservation alone (no hardware started) still enforces 300 seconds.
    """

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self._lock = None
        self._connection = None
        self._reservation = None

    def __enter__(self):
        if self._lock is not None:
            raise RuntimeError("DisplayState context is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self.path.with_name(self.path.name + ".lock").open("a+b")
        try:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise StateLockedError("Display lifecycle is already locked") from error
            exists = self.path.exists()
            if exists and self.path.stat().st_size == 0:
                raise StateCorruptError("Existing display state is empty")
            self._connection = sqlite3.connect(self.path, timeout=0)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA synchronous = FULL")
            if not exists:
                with self._connection:
                    self._connection.execute(
                        """CREATE TABLE display_state (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            last_attempt_at REAL,
                            last_attempt_digest TEXT,
                            last_success_at REAL,
                            last_success_digest TEXT,
                            cooldown_at REAL,
                            clock_at REAL,
                            in_progress INTEGER NOT NULL
                        )"""
                    )
                    self._connection.execute(
                        "INSERT INTO display_state (id, in_progress) VALUES (1, 0)"
                    )
                    self._connection.execute("PRAGMA user_version = 1")
            if self._connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise StateCorruptError("Unsupported display state version")
            if self._connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StateCorruptError("Display state failed SQLite integrity check")
            self.status()
            return self
        except BaseException as error:
            self.__exit__(type(error), error, error.__traceback__)
            if isinstance(error, sqlite3.DatabaseError):
                raise StateCorruptError("Cannot read display state") from error
            raise

    def __exit__(self, exc_type, exc, traceback):
        cleanup_error = None
        for resource in (self._connection, self._lock):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    logger.exception("Display state cleanup failed")
                    if cleanup_error is None:
                        cleanup_error = error
        self._connection = self._lock = self._reservation = None
        if exc is None and cleanup_error is not None:
            raise cleanup_error
        return False

    def status(self) -> dict:
        """Read validated metadata without changing cooldown or hardware state."""
        if self._connection is None:
            raise RuntimeError("DisplayState must be used as a context manager")
        try:
            rows = self._connection.execute("SELECT * FROM display_state").fetchall()
            if len(rows) != 1:
                raise StateCorruptError(
                    "Display state singleton is missing or duplicated"
                )
            row = dict(rows[0])
            expected = {
                "id",
                "last_attempt_at",
                "last_attempt_digest",
                "last_success_at",
                "last_success_digest",
                "cooldown_at",
                "clock_at",
                "in_progress",
            }
            if row.keys() != expected or row["id"] != 1:
                raise StateCorruptError("Invalid display state schema")
            for name in (
                "last_attempt_at",
                "last_success_at",
                "cooldown_at",
                "clock_at",
            ):
                if row[name] is not None:
                    _timestamp(row[name])
            for prefix in ("last_attempt", "last_success"):
                at, digest = row[f"{prefix}_at"], row[f"{prefix}_digest"]
                if (at is None) != (digest is None):
                    raise StateCorruptError("Incomplete display state metadata")
                if digest is not None and (not isinstance(digest, str) or not digest):
                    raise StateCorruptError("Invalid display digest")
            if row["in_progress"] not in (0, 1):
                raise StateCorruptError("Invalid hardware progress marker")
            attempt, cooldown, clock = (
                row["last_attempt_at"],
                row["cooldown_at"],
                row["clock_at"],
            )
            if attempt is None:
                if (
                    any(
                        row[key] is not None
                        for key in ("cooldown_at", "last_success_at", "clock_at")
                    )
                    or row["in_progress"]
                ):
                    raise StateCorruptError("Missing display attempt metadata")
            elif cooldown is None or clock is None or not attempt <= cooldown <= clock:
                raise StateCorruptError("Invalid display cooldown ordering")
            success = row["last_success_at"]
            if success is not None and success > cooldown:
                raise StateCorruptError("Invalid display success timestamp")
            return row
        except (sqlite3.DatabaseError, ClockError) as error:
            raise StateCorruptError("Cannot trust display state") from error

    def _write(self, **values) -> None:
        # Only internal, fixed column names reach this helper.
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connection:
            self._connection.execute(
                f"UPDATE display_state SET {assignments} WHERE id = 1",
                tuple(values.values()),
            )

    def _check_time(self, now: float, row: dict) -> None:
        _timestamp(now)
        if row["clock_at"] is not None and now < row["clock_at"]:
            raise ClockError("Display clock moved backwards; refusing refresh")

    def reserve(self, now: float, digest: str) -> bool:
        """Commit an attempt, or log an unchanged/cooldown skip; never wait."""
        if not isinstance(digest, str) or not digest:
            raise ValueError("Display digest must be a nonempty string")
        row = self.status()
        self._check_time(now, row)
        if row["in_progress"]:
            raise IncompleteAttemptError(
                "Previous display attempt was interrupted; hardware inspection required"
            )
        if self._reservation is not None:
            raise RuntimeError("An attempt is already reserved in this context")
        # A later failed transfer can leave the screen partially changed, even
        # when the requested frame matches historical success metadata.
        if (
            digest == row["last_success_digest"]
            and row["last_attempt_at"] <= row["last_success_at"]
        ):
            self._write(clock_at=now)
            logger.info("Skipping unchanged display frame")
            return False
        if (
            row["cooldown_at"] is not None
            and now - row["cooldown_at"] < MIN_REFRESH_INTERVAL
        ):
            self._write(clock_at=now)
            logger.info("Skipping display frame: 300-second cooldown")
            return False
        self._write(
            last_attempt_at=now,
            last_attempt_digest=digest,
            cooldown_at=now,
            clock_at=now,
        )
        self._reservation = digest
        return True

    def begin_attempt(self) -> None:
        """Commit an interrupted-operation guard before constructing a driver."""
        if self._reservation is None:
            raise RuntimeError("Hardware access requires a reservation")
        if self.status()["in_progress"]:
            raise RuntimeError("Hardware attempt already started")
        self._write(in_progress=1)

    def _complete(self, now: float, digest: str | None) -> None:
        row = self.status()
        self._check_time(now, row)
        if self._reservation is None or (
            digest is not None and digest != self._reservation
        ):
            raise RuntimeError("Completion requires a matching reservation")
        values = {"cooldown_at": now, "clock_at": now, "in_progress": 0}
        if digest is not None:
            values.update(last_success_at=now, last_success_digest=digest)
        self._write(**values)
        self._reservation = None

    def finish_attempt(self, now: float) -> None:
        """Persist failure completion without claiming a successful refresh."""
        self._complete(now, None)

    def succeed(self, now: float, digest: str) -> None:
        """Atomically record completion and success only after hardware cleanup."""
        self._complete(now, digest)
