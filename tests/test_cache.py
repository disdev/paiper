import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from paiper.cache import ContentCache
from paiper.content import ExchangeRate

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)
RATE = ExchangeRate(date(2026, 10, 26), "USD", "PEN", 3.44)


def save(cache, now=NOW):
    cache.update(
        "usd_pen",
        RATE,
        now,
        observed_at=NOW - timedelta(days=1),
        valid_until=NOW,
        cadence=86400,
    )


def test_content_cache_does_not_modify_an_unrelated_database(tmp_path):
    path = tmp_path / "display.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    before = path.read_bytes()
    with pytest.raises(ValueError), ContentCache(path):
        pass
    assert path.read_bytes() == before


def test_display_safety_database_is_rejected_without_changes(tmp_path):
    from paiper.state import DisplayState

    path = tmp_path / "display.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "test")
    before = path.read_bytes()
    with pytest.raises(ValueError), ContentCache(path):
        pass
    assert path.read_bytes() == before


@pytest.mark.parametrize("data", [b"", b"not sqlite"])
def test_damaged_database_is_not_replaced(tmp_path, data):
    path = tmp_path / "content.sqlite3"
    path.write_bytes(data)
    with pytest.raises(ValueError), ContentCache(path):
        pass
    assert path.read_bytes() == data


def test_reopen_retains_typed_payload_dates_and_selection(tmp_path):
    path = tmp_path / "content.sqlite3"
    with ContentCache(path) as cache:
        save(cache)
        cache.set_state("playlist", {"index": 2, "recent": ["bbc:one"]})
    with ContentCache(path) as cache:
        record = cache.snapshot()["usd_pen"]
        assert record.payload == RATE
        assert isinstance(record.payload.date, date)
        assert record.observed_at == NOW - timedelta(days=1)
        assert record.is_stale(NOW)
        assert record.status(NOW) == "stale"
        assert not record.is_due(NOW)
        assert record.is_due(NOW + timedelta(days=1))
        assert cache.get_state("playlist") == {"index": 2, "recent": ["bbc:one"]}
        assert cache.get_state("absent", []) == []


def test_failure_keeps_last_good_and_backs_off_then_success_resets(tmp_path):
    with ContentCache(tmp_path / "content.sqlite3") as cache:
        save(cache)
        cache.fail("usd_pen", NOW, "HTTP 429", cadence=300, retry_after=1800)
        record = cache.snapshot()["usd_pen"]
        assert record.payload == RATE
        assert record.fetched_at == NOW
        assert record.failures == 1
        assert record.error == "HTTP 429"
        assert record.status(NOW) == "degraded"
        assert record.next_fetch_at == NOW + timedelta(seconds=1800)
        cache.fail("usd_pen", NOW, "timeout", cadence=300)
        assert cache.snapshot()["usd_pen"].next_fetch_at == NOW + timedelta(seconds=600)
        save(cache)
        assert cache.snapshot()["usd_pen"].failures == 0
        assert cache.snapshot()["usd_pen"].error is None


def test_missing_source_failure_is_visible_bounded_and_persistent(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        for _ in range(20):
            cache.fail("weather", NOW, "x" * 1000, cadence=300, retry_after=1e9)
    with ContentCache(path) as cache:
        record = cache.snapshot()["weather"]
        assert record.payload is None
        assert record.status(NOW) == "unavailable"
        assert len(record.error) <= 300
        assert record.next_fetch_at <= NOW + timedelta(hours=6)


def test_history_and_state_are_bounded_and_invalid_write_is_atomic(tmp_path):
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        for i in range(40):
            save(cache, NOW + timedelta(seconds=i))
        assert len(cache.history("usd_pen")) == 32
        assert cache.history("usd_pen")[0].fetched_at == NOW + timedelta(seconds=39)
        with pytest.raises(ValueError):
            cache.set_state("large", "a" * 65537)
        with pytest.raises(ValueError):
            cache.update(
                "usd_pen",
                ExchangeRate(date(2026, 10, 26), "USD", "PEN", float("nan")),
                NOW,
                observed_at=NOW,
                valid_until=NOW,
                cadence=300,
            )
        assert cache.snapshot()["usd_pen"].payload == RATE
        assert cache.get_state("large") is None


def test_corrupt_payload_or_schema_is_not_silently_reset(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        save(cache)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE sources SET record = '{\"bad\":true}'")
    before = path.read_bytes()
    with pytest.raises(ValueError), ContentCache(path):
        pass
    assert path.read_bytes() == before


def test_date_only_observation_is_never_fabricated_into_midnight(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        cache.update(
            "usd_pen",
            RATE,
            NOW,
            observed_at=RATE.date,
            valid_until=NOW,
            cadence=86400,
        )
    with ContentCache(path) as cache:
        assert type(cache.snapshot()["usd_pen"].observed_at) is date
        assert cache.snapshot()["usd_pen"].observed_at == date(2026, 10, 26)


@pytest.mark.parametrize(
    "damage",
    [
        "PRAGMA user_version=999",
        "DROP TABLE history",
        "UPDATE sources SET record=replace(record, 'ExchangeRate', 'UnknownPayload')",
    ],
)
def test_wrong_version_schema_or_type_is_rejected_without_mutation(tmp_path, damage):
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        save(cache)
    with sqlite3.connect(path) as connection:
        connection.execute(damage)
    before = path.read_bytes()
    with pytest.raises(ValueError), ContentCache(path):
        pass
    assert path.read_bytes() == before
