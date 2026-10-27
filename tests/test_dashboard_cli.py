import os

import pytest
from PIL import Image

from paiper import cli
from paiper.cache import ContentCache
from paiper.state import DisplayState


def arguments(tmp_path):
    profile = tmp_path / "panel.yaml"
    profile.write_text("panel:\n  confirmed: true\n")
    return [
        "display",
        "--scene",
        "dashboard",
        "--offline",
        "--config",
        str(profile),
        "--state",
        str(tmp_path / "display.sqlite3"),
        "--cache",
        str(tmp_path / "content.sqlite3"),
    ]


@pytest.mark.parametrize("result", [True, False, "raise", "no_factory"])
def test_dashboard_success_only_and_protected_collection(tmp_path, monkeypatch, result):
    from paiper import sources
    from paiper.render import RenderError

    inside = False
    collected = []
    real_collect = sources.collect

    def collect(*args, **kwargs):
        assert inside
        collected.append(True)
        return real_collect(*args, **kwargs)

    def display(factory, *_):
        nonlocal inside
        if result == "no_factory":
            return False
        inside = True
        image = factory()
        assert image.size == (600, 400)
        inside = False
        if result == "raise":
            raise RenderError("Failed display")
        return result

    monkeypatch.setattr(sources, "collect", collect)
    monkeypatch.setattr(cli, "render_and_display", display)
    monkeypatch.setattr(
        cli, "render_dashboard", lambda *_: Image.new("RGB", (600, 400), "white")
    )
    assert cli.main(arguments(tmp_path)) == (1 if result == "raise" else 0)
    with ContentCache(tmp_path / "content.sqlite3") as cache:
        state = cache.get_state("playlist")
    assert (state is not None) == (result is True)
    assert bool(collected) == (result != "no_factory")


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_cache_alias_cannot_mutate_hardware_safety(tmp_path, monkeypatch, alias):
    args = arguments(tmp_path)
    safety = tmp_path / "display.sqlite3"
    with DisplayState(safety):
        pass
    before = safety.read_bytes()
    cache = tmp_path / "content.sqlite3"
    if alias == "same":
        args[-1] = str(safety)
    elif alias == "symlink":
        cache.symlink_to(safety)
    else:
        os.link(safety, cache)
    monkeypatch.setattr(
        cli, "render_and_display", lambda *_: pytest.fail("Hardware path entered")
    )
    assert cli.main(args) == 1
    assert safety.read_bytes() == before


def test_offline_preview_does_not_advance_or_call_providers(tmp_path, monkeypatch):
    from paiper.sources import http

    monkeypatch.setattr(http, "fetch", lambda *_: pytest.fail("Provider called"))
    monkeypatch.setattr(
        cli, "render_and_display", lambda *_: pytest.fail("Hardware called")
    )
    cache_path = tmp_path / "content.sqlite3"
    with ContentCache(cache_path) as cache:
        cache.set_state("playlist", {"cursors": {"day": 2}, "recent_ids": ["old"]})
    assert (
        cli.main(
            [
                "preview",
                "--scene",
                "dashboard",
                "--offline",
                "--page",
                "verse",
                "--cache",
                str(cache_path),
                "--output",
                str(tmp_path / "verse.png"),
            ]
        )
        == 0
    )
    with ContentCache(cache_path) as cache:
        assert cache.get_state("playlist") == {
            "cursors": {"day": 2},
            "recent_ids": ["old"],
        }
    assert not (tmp_path / "dashboard.yaml").exists()


def test_real_display_guard_spans_dashboard_render_failure(tmp_path, monkeypatch):
    from paiper.render import RenderError
    from paiper.state import StateLockedError

    args = arguments(tmp_path)
    safety = tmp_path / "display.sqlite3"
    with DisplayState(safety) as state:
        before = state.status()

    def fail(*_):
        with pytest.raises(StateLockedError), DisplayState(safety):
            pass
        raise RenderError("Intentional pre-hardware failure")

    monkeypatch.setattr(cli, "render_dashboard", fail)
    assert cli.main(args) == 1
    with DisplayState(safety) as state:
        assert state.status() == before
    with ContentCache(tmp_path / "content.sqlite3") as cache:
        assert cache.get_state("playlist") is None


def test_offline_wrong_location_cannot_show_old_weather(tmp_path, monkeypatch):
    from test_dashboard_render import snapshot

    cache_path = tmp_path / "content.sqlite3"
    with ContentCache(cache_path) as cache:
        snapshot(cache)
    prefs = tmp_path / "dashboard.yaml"
    prefs.write_text("latitude: 10\nlongitude: 10\nlocation: Elsewhere\n")
    monkeypatch.setattr(
        cli,
        "render_dashboard",
        lambda *_: pytest.fail("Wrong-location weather rendered"),
    )
    assert (
        cli.main(
            [
                "preview",
                "--scene",
                "dashboard",
                "--offline",
                "--page",
                "weather",
                "--cache",
                str(cache_path),
                "--dashboard-config",
                str(prefs),
                "--output",
                str(tmp_path / "weather.png"),
            ]
        )
        == 1
    )


def test_capabilities_are_exact_declarative_manifest():
    import json
    from pathlib import Path

    assert json.loads(Path("deploy/app-capabilities.json").read_text()) == {
        "version": 1,
        "scenes": ["stillness", "calibration", "dashboard"],
    }


def test_same_cache_inode_serializes_collection_and_success_commit(
    tmp_path, monkeypatch
):
    import fcntl

    args = arguments(tmp_path)
    path = tmp_path / "content.sqlite3"
    with ContentCache(path):
        pass
    alias = tmp_path / "alias.sqlite3"
    os.link(path, alias)
    args[-1] = str(alias)
    monkeypatch.setattr(
        cli,
        "render_and_display",
        lambda *_: pytest.fail("Busy cache entered hardware path"),
    )
    lock = cli._cache_lock_path(path)
    assert lock == cli._cache_lock_path(alias)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cli.main(args) == 1
