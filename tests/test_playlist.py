from datetime import UTC, datetime, timedelta

import pytest

from paiper.cache import ContentCache
from paiper.content import Game, GameTeam, Schedule, SourceRecord

NOW = datetime(2026, 10, 27, 16, tzinfo=UTC)


def game_record(status="Live", age=0):
    team = GameTeam(138, "St. Louis Cardinals", 2, None)
    game = Game(
        42,
        NOW.date(),
        NOW,
        False,
        status,
        status,
        None,
        None,
        team,
        GameTeam(1, "Los Angeles Dodgers", None, None),
        4,
        "Top",
    )
    return {
        "cardinals": SourceRecord(
            "cardinals",
            Schedule(NOW.date(), NOW.date(), (game,)),
            NOW - timedelta(minutes=age),
            valid_until=NOW + timedelta(minutes=5),
        )
    }


def pages():
    from paiper.dashboard import Block, Page

    return tuple(
        Page(
            key,
            "game:42" if key == "cardinals" else key,
            key,
            (Block("test", "Readable content"),),
            "Test source",
            "Dated fixture",
        )
        for key in ("markets", "news", "gallery", "cardinals", "flights")
    )


@pytest.mark.parametrize(
    ("instant", "part"),
    [
        ("2026-11-01T06:30:00+00:00", "night"),
        ("2026-11-01T07:30:00+00:00", "night"),
        ("2026-03-08T08:30:00+00:00", "night"),
        ("2026-10-27T11:00:00+00:00", "morning"),
        ("2026-10-27T14:00:00+00:00", "day"),
        ("2026-10-27T22:00:00+00:00", "evening"),
    ],
)
def test_dayparts_and_dst(instant, part):
    # Import inside tests establishes a missing-feature RED, not collection errors.
    from paiper.playlist import daypart
    from paiper.preferences import Preferences

    assert daypart(datetime.fromisoformat(instant), Preferences()) == part


def test_selection_is_pure_and_success_survives_restart(tmp_path):
    from paiper.playlist import record_success, select_page
    from paiper.preferences import Preferences

    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        first = select_page(pages(), {}, NOW, Preferences(), {})
        assert cache.get_state("playlist") is None
        record_success(cache, first, NOW)
    with ContentCache(path) as cache:
        state = cache.get_state("playlist")
        assert state["last_success"]["page"] == first.page.key
        assert state["last_success"]["at"] == NOW.isoformat()
        second = select_page(pages(), {}, NOW, Preferences(), state)
        assert second.page.key != first.page.key


def test_recent_live_game_gets_every_other_slot():
    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    state = {}
    selected = []
    for _ in range(6):
        choice = select_page(pages(), game_record(), NOW, Preferences(), state)
        selected.append(choice.page.key)
        state = choice.state
    assert selected[::2] == ["cardinals"] * 3
    assert all(key != "cardinals" for key in selected[1::2])
    assert len(set(selected[1::2])) > 1


def test_cached_live_status_does_not_monopolize():
    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    assert (
        select_page(pages(), game_record(age=60), NOW, Preferences(), {}).page.key
        == "markets"
    )


def test_final_extra_slot_exactly_once():
    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    first = select_page(pages(), game_record("Final"), NOW, Preferences(), {})
    assert first.page.key == "cardinals"
    second = select_page(pages(), game_record("Final"), NOW, Preferences(), first.state)
    assert second.page.key != "cardinals"


def test_flight_limits_are_persisted():
    from paiper.dashboard import Block, Page
    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    prefs = Preferences(playlists={"day": ("flights", "gallery")})
    aircraft = Page(
        "flights",
        "aircraft:a",
        "Above Conway",
        (Block("A", "Observation"),),
        "adsb.lol",
        "Dated",
        content_ids=("aircraft:a",),
    )
    available = (aircraft, pages()[2])
    first = select_page(available, {}, NOW, prefs, {})
    assert first.page.key == "flights"
    state = first.state
    for minutes in (5, 30, 59):
        result = select_page(
            available, {}, NOW + timedelta(minutes=minutes), prefs, state
        )
        assert result.page.key == "gallery"
        state = result.state
    assert (
        select_page(available, {}, NOW + timedelta(hours=1), prefs, state).page.key
        == "flights"
    )


def test_different_aircraft_also_waits_thirty_minutes():
    from dataclasses import replace

    from paiper.dashboard import Block, Page
    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    prefs = Preferences(playlists={"day": ("flights", "gallery")})
    a = Page(
        "flights", "aircraft:a", "Above Conway", (Block("", "A"),), "adsb.lol", "Dated"
    )
    b = replace(a, id="aircraft:b")
    first = select_page((a, b, pages()[2]), {}, NOW, prefs, {})
    second = select_page(
        (a, b, pages()[2]), {}, NOW + timedelta(minutes=29), prefs, first.state
    )
    assert second.page.key == "gallery"
    third = select_page(
        (a, b, pages()[2]), {}, NOW + timedelta(minutes=30), prefs, second.state
    )
    assert third.page.id == "aircraft:b"


def test_stories_and_photos_rotate_after_restart(tmp_path):
    from dataclasses import replace

    from paiper.dashboard import Block, Page
    from paiper.playlist import record_success, select_page
    from paiper.preferences import Preferences

    prefs = Preferences(playlists={"day": ("news", "gallery")})
    a = Page(
        "news",
        "story:a",
        "News",
        (Block("", "A"),),
        "BBC",
        "Dated",
        content_ids=("story:a",),
    )
    b = replace(a, id="story:b", content_ids=("story:b",))
    art1 = replace(a, key="gallery", id="photo:a", content_ids=("photo:a",))
    art2 = replace(art1, id="photo:b", content_ids=("photo:b",))
    path = tmp_path / "cache.sqlite3"
    chosen = []
    for _ in range(4):
        with ContentCache(path) as cache:
            selection = select_page(
                (a, b, art1, art2), {}, NOW, prefs, cache.get_state("playlist", {})
            )
            chosen.append(selection.page.id)
            record_success(cache, selection, NOW)
    assert chosen == ["story:a", "photo:a", "story:b", "photo:b"]


def test_maximum_length_story_ids_do_not_overflow_cache_state(tmp_path):
    from dataclasses import replace

    from paiper.dashboard import Block, Page
    from paiper.playlist import record_success, select_page
    from paiper.preferences import Preferences

    prefs = Preferences(playlists={"day": ("news",)})
    base = Page("news", "", "News", (Block("", "A"),), "BBC", "Dated")
    state = {}
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        for i in range(128):
            ident = "story:" + str(i) + "x" * 1900
            choice = select_page(
                (replace(base, id=ident, content_ids=(ident,)),), {}, NOW, prefs, state
            )
            record_success(cache, choice, NOW)
            state = cache.get_state("playlist")
        assert len(state["recent_ids"]) == 128


def test_flight_bound_starts_at_display_completion(tmp_path):
    from paiper.dashboard import Block, Page
    from paiper.playlist import record_success, select_page
    from paiper.preferences import Preferences

    prefs = Preferences(playlists={"day": ("flights",)})
    page = Page(
        "flights", "aircraft:a", "Above Conway", (Block("", "A"),), "adsb.lol", "Dated"
    )
    choice = select_page((page,), {}, NOW, prefs, {})
    completed = NOW + timedelta(seconds=40)
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        record_success(cache, choice, completed)
        state = cache.get_state("playlist")
    assert state["last_flight"] == completed.isoformat()
    assert state["aircraft"]["aircraft:a"] == completed.isoformat()


def test_missing_priority_game_does_not_show_wrong_game_or_consume_final():
    from dataclasses import replace

    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    available = tuple(
        replace(p, id="game:999") if p.key == "cardinals" else p for p in pages()
    )
    choice = select_page(available, game_record("Final"), NOW, Preferences(), {})
    assert choice.page.key == "markets"
    assert choice.state["finals"] == []
