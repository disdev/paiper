"""Separate durable content cache; never the physical display safety database."""

import json
import math
import sqlite3
import types
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

from paiper import content
from paiper.content import Payload, SourceRecord

APPLICATION_ID = 0x50414943  # PAIC, deliberately distinct from hardware state.
SCHEMA_VERSION = 1
_TYPES = {
    cls.__name__: cls
    for cls in vars(content).values()
    if isinstance(cls, type) and is_dataclass(cls)
}
_COLUMNS = {
    "sources": ("key", "record"),
    "history": ("id", "key", "record"),
    "state": ("key", "value"),
}


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Expected an aware datetime")
    return value.astimezone(UTC)


def _encode(value):
    if is_dataclass(value):
        return {
            "type": type(value).__name__,
            "fields": {
                field.name: _encode(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, datetime):
        return {"datetime": _instant(value).isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    return value


def _decode(node, kind):
    origin = get_origin(kind)
    if origin is types.UnionType:
        for option in get_args(kind):
            try:
                return _decode(node, option)
            except (ValueError, TypeError):
                pass
        raise ValueError("Unexpected cached value type")
    if origin is tuple:
        if not isinstance(node, list):
            raise ValueError("Expected cached array")
        return tuple(_decode(item, get_args(kind)[0]) for item in node)
    if is_dataclass(kind):
        if not isinstance(node, dict) or set(node) != {"type", "fields"}:
            raise ValueError("Invalid cached record")
        if _TYPES.get(node["type"]) is not kind:
            raise ValueError("Unknown cached record type")
        hints = get_type_hints(kind)
        if (
            not isinstance(node["fields"], dict)
            or node["fields"].keys() != hints.keys()
        ):
            raise ValueError("Invalid cached fields")
        return kind(
            **{key: _decode(value, hints[key]) for key, value in node["fields"].items()}
        )
    if kind in (datetime, date):
        key = "datetime" if kind is datetime else "date"
        if not isinstance(node, dict) or set(node) != {key}:
            raise ValueError("Invalid cached date")
        result = kind.fromisoformat(node[key])
        return _instant(result) if kind is datetime else result
    if kind is float:
        if type(node) not in (int, float) or not math.isfinite(node):
            raise ValueError("Invalid cached number")
        return float(node)
    if type(node) is not kind:
        raise ValueError("Invalid cached scalar")
    return node


def _dump(record):
    node = _encode(record)
    _decode(node, SourceRecord)
    result = json.dumps(node, allow_nan=False, separators=(",", ":"))
    if len(result.encode()) > 2 * 1024 * 1024:
        raise ValueError("Cached record exceeds 2 MiB")
    return result


def _load(raw):
    try:
        record = _decode(json.loads(raw), SourceRecord)
        if record.failures < 0 or (
            record.payload is not None and record.fetched_at is None
        ):
            raise ValueError("Invalid cache health")
        return record
    except (TypeError, KeyError, RecursionError) as error:
        raise ValueError("Invalid cached record") from error


def _invalid_constant(value):
    raise ValueError("Nonfinite selection state")


class ContentCache:
    """Caller-thread-only SQLite cache with atomic, separately committed writes."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self._connection = None

    def __enter__(self):
        if self._connection is not None:
            raise RuntimeError("ContentCache is already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.exists():
                # Inspect read-only before even allowing SQLite journal recovery.
                reader = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
                try:
                    self._validate(reader)
                finally:
                    reader.close()
            else:
                # Do not adopt an unrelated database created by a competing writer.
                with self.path.open("xb"):
                    pass
                creator = sqlite3.connect(self.path, timeout=1)
                try:
                    creator.executescript(f"""
                        BEGIN IMMEDIATE;
                        PRAGMA application_id = {APPLICATION_ID};
                        PRAGMA user_version = {SCHEMA_VERSION};
                        CREATE TABLE sources (key TEXT PRIMARY KEY, record TEXT NOT NULL);
                        CREATE TABLE history (
                            id INTEGER PRIMARY KEY, key TEXT NOT NULL, record TEXT NOT NULL
                        );
                        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                        COMMIT;
                    """)
                finally:
                    creator.close()
            self._connection = sqlite3.connect(
                self.path.as_uri() + "?mode=rw",
                uri=True,
                timeout=1,
            )
            self._validate(self._connection)
            return self
        except (sqlite3.DatabaseError, ValueError) as error:
            self.__exit__(None, None, None)
            raise ValueError(
                "Content cache is invalid or belongs to another application"
            ) from error

    def __exit__(self, exc_type, exc, traceback):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        return False

    @staticmethod
    def _validate(connection):
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Wrong database identity")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Unsupported content cache version")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Damaged content cache")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if tables != _COLUMNS.keys():
            raise ValueError("Invalid content cache schema")
        for table, columns in _COLUMNS.items():
            if (
                tuple(
                    row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                )
                != columns
            ):
                raise ValueError("Invalid content cache columns")
        for key, raw in connection.execute("SELECT key, record FROM sources"):
            if _load(raw).source_key != key:
                raise ValueError("Mismatched source identity")
        for key, raw in connection.execute("SELECT key, record FROM history"):
            if _load(raw).source_key != key:
                raise ValueError("Mismatched history identity")
        for (raw,) in connection.execute("SELECT value FROM state"):
            json.loads(raw, parse_constant=_invalid_constant)

    def _db(self):
        if self._connection is None:
            raise RuntimeError("ContentCache must be used as a context manager")
        return self._connection

    @staticmethod
    def _key(key):
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError("Cache keys must be 1–100 characters")
        return key

    def snapshot(self) -> dict[str, SourceRecord]:
        return {
            key: _load(raw)
            for key, raw in self._db().execute("SELECT key, record FROM sources")
        }

    def history(self, source_key: str) -> tuple[SourceRecord, ...]:
        return tuple(
            _load(raw)
            for (raw,) in self._db().execute(
                "SELECT record FROM history WHERE key=? ORDER BY id DESC LIMIT 32",
                (self._key(source_key),),
            )
        )

    def _write(self, record, *, keep_history):
        raw = _dump(record)
        connection = self._db()
        with connection:
            if (
                connection.execute("SELECT count(*) FROM sources").fetchone()[0] >= 64
                and not connection.execute(
                    "SELECT 1 FROM sources WHERE key=?", (record.source_key,)
                ).fetchone()
            ):
                raise ValueError("Content cache is limited to 64 sources")
            connection.execute(
                "INSERT INTO sources VALUES (?,?) ON CONFLICT(key) DO UPDATE SET record=excluded.record",
                (record.source_key, raw),
            )
            if keep_history:
                connection.execute(
                    "INSERT INTO history(key,record) VALUES (?,?)",
                    (record.source_key, raw),
                )
                connection.execute(
                    """DELETE FROM history WHERE key=? AND id NOT IN
                       (SELECT id FROM history WHERE key=? ORDER BY id DESC LIMIT 32)""",
                    (record.source_key, record.source_key),
                )

    def update(
        self,
        source_key: str,
        payload: Payload,
        now: datetime,
        *,
        observed_at: datetime | date | None,
        valid_until: datetime,
        cadence: float,
        context: str | None = None,
    ) -> None:
        now = _instant(now)
        delay = self._cadence(cadence)
        record = SourceRecord(
            self._key(source_key),
            payload,
            now,
            _instant(observed_at) if isinstance(observed_at, datetime) else observed_at,
            _instant(valid_until),
            now,
            now + timedelta(seconds=delay),
            context=context,
            attempt_context=context,
        )
        self._write(record, keep_history=True)

    @staticmethod
    def _cadence(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Cadence must be finite and positive")
        return value

    def fail(
        self,
        source_key: str,
        now: datetime,
        error: str,
        *,
        cadence: float,
        retry_after: float | None = None,
        context: str | None = None,
    ) -> None:
        now = _instant(now)
        key = self._key(source_key)
        old = self.snapshot().get(key, SourceRecord(key))
        delay = self._cadence(cadence) * 2 ** min(old.failures, 10)
        if retry_after is not None:
            delay = max(delay, self._cadence(retry_after))
        record = replace(
            old,
            last_attempt_at=now,
            next_fetch_at=now + timedelta(seconds=min(delay, 21600)),
            failures=old.failures + 1,
            error=" ".join(str(error).split())[:300] or "Source failed",
            attempt_context=context,
        )
        self._write(record, keep_history=False)

    def get_state(self, key: str, default=None):
        row = (
            self._db()
            .execute("SELECT value FROM state WHERE key=?", (self._key(key),))
            .fetchone()
        )
        return default if row is None else json.loads(row[0])

    def set_state(self, key: str, value) -> None:
        key = self._key(key)
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if len(raw.encode()) > 65536:
            raise ValueError("Selection state exceeds 64 KiB")
        connection = self._db()
        with connection:
            if (
                connection.execute("SELECT count(*) FROM state").fetchone()[0] >= 128
                and not connection.execute(
                    "SELECT 1 FROM state WHERE key=?", (key,)
                ).fetchone()
            ):
                raise ValueError("Selection state is limited to 128 keys")
            connection.execute(
                "INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, raw),
            )
