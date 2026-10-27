"""Deterministic snapshots exercise the real network-blocked Chromium pipeline."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image
from test_dashboard import NOW, weather
from test_playlist import game_record

from paiper import render
from paiper.cache import ContentCache
from paiper.content import (
    Aircraft,
    Alert,
    Alerts,
    Article,
    ExchangeRate,
    Feed,
    Flights,
    Quote,
)
from paiper.dashboard import PHOTO_BOX, build_pages
from paiper.panel import RGB_TO_CODE, quantize_photo
from paiper.preferences import PAGES, Preferences
from paiper.sources import collect

ARTIFACTS = Path(".artifacts/task-2")


def snapshot(cache):
    feed = Feed(
        "Publisher",
        (
            Article(
                "one",
                "A deliberately long but source-shaped headline: "
                + "important public research and practical community updates " * 5,
                "https://example.org/one",
                NOW - timedelta(days=2),
            ),
            Article("two", "Second distinct headline", "https://example.org/two", None),
        ),
    )
    records = {
        "weather": weather(),
        "alerts": Alerts(
            NOW,
            (
                Alert(
                    "alert",
                    NOW,
                    NOW + timedelta(hours=1),
                    NOW,
                    "Severe",
                    "Severe Thunderstorm Warning",
                    "Severe thunderstorms may produce damaging winds. Follow official local warnings.",
                ),
            ),
        ),
        "cardinals": game_record()["cardinals"].payload,
        "blkb": Quote(
            "BLKB",
            "USD",
            57.32,
            NOW - timedelta(days=4),
            56.98,
            (NOW - timedelta(days=5)).date(),
        ),
        "vti": Quote("VTI", "USD", 987654321.25, NOW, None, None),
        # Deliberately omit VOO: a failed ticker must not erase two healthy quotes.
        "usd_pen": ExchangeRate(NOW.date(), "USD", "PEN", 3.7521),
        "world_news": feed,
        "ai_news": feed,
        "cardinals_news": feed,
        "flights": Flights(
            NOW,
            (
                Aircraft(
                    "abc123",
                    "TEST123",
                    None,
                    "B738",
                    35000,
                    None,
                    13.4,
                    55,
                    35.1,
                    -92.3,
                    NOW - timedelta(seconds=35),
                ),
                Aircraft(
                    "old123",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    NOW - timedelta(days=1),
                ),
            ),
        ),
    }
    for key, payload in records.items():
        context = (
            json.dumps([35.0887, -92.4421, "America/Chicago"], separators=(",", ":"))
            if key in ("weather", "alerts", "flights")
            else None
        )
        cache.update(
            key,
            payload,
            NOW,
            observed_at=NOW,
            valid_until=NOW + timedelta(minutes=5),
            cadence=300,
            context=context,
        )
    return collect(cache, NOW, offline=True)


@pytest.fixture(scope="module")
def page_contexts(tmp_path_factory):
    path = tmp_path_factory.mktemp("snapshots") / "content.sqlite3"
    with ContentCache(path) as cache:
        return build_pages(snapshot(cache), NOW, Preferences())


@pytest.mark.parametrize("key", PAGES)
def test_every_page_real_chromium(key, page_contexts, monkeypatch):
    original = render._check_layout

    def check(page):
        original(page)
        assert page.locator("header, main, footer").evaluate_all(
            "es => es.map(e => e.getBoundingClientRect().height)"
        ) == [48, 304, 48]
        sizes = page.locator(".label,.detail,footer div,header span").evaluate_all(
            "es => es.map(e => parseFloat(getComputedStyle(e).fontSize))"
        )
        assert all(size >= 18 for size in sizes)
        if key == "weather":
            assert (
                page.locator("section:first-child .text").evaluate(
                    "e => parseFloat(getComputedStyle(e).fontSize)"
                )
                >= 40
            )
        assert page.locator("body, [data-fit]:not(img)").evaluate_all(
            "es => es.every(e => !['hidden','clip'].includes(getComputedStyle(e).overflow))"
        )

    monkeypatch.setattr(render, "_check_layout", check)
    context = next(p for p in page_contexts if p.key == key)
    frame = render.render_dashboard(context, NOW)
    assert frame.size == (600, 400) and frame.mode == "RGB"
    assert {c for _, c in frame.getcolors(240000)} <= set(RGB_TO_CODE)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    frame.save(ARTIFACTS / f"{key}.png")
    if key == "gallery":
        from io import BytesIO

        with Image.open(BytesIO(context.photo)) as photo:
            assert frame.crop(PHOTO_BOX).tobytes() == quantize_photo(photo).tobytes()


def test_all_bundled_verses_fit():
    from importlib.resources import files

    from paiper.dashboard import Block

    verses = json.loads(files("paiper").joinpath("assets", "verses.json").read_text())
    page = next(p for p in build_pages({}, NOW, Preferences()) if p.key == "verse")
    for index, verse in enumerate(verses):
        context = replace(page, blocks=(Block(verse["reference"], verse["text"]),))
        frame = render.render_dashboard(context, NOW)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        frame.save(ARTIFACTS / f"verse-{index}.png")


def test_degraded_forecast_and_old_flight_render(page_contexts):
    for key in ("weather", "flights"):
        context = next(
            p
            for p in page_contexts
            if p.key == key and (key != "flights" or p.id == "aircraft:old123")
        )
        context = replace(
            context, freshness="degraded, stale cache · Fetched Oct 23 2026 11:00 CDT"
        )
        frame = render.render_dashboard(context, NOW)
        frame.save(ARTIFACTS / f"{key}-degraded.png")


@pytest.mark.parametrize("key", ["world_news", "ai_news", "cardinals_news"])
def test_wide_unbroken_headlines_remain_legible(key):
    from test_dashboard import record

    feed = Feed("Publisher", (Article("wide", "W" * 240, "https://example.org", NOW),))
    for context in build_pages({key: record(key, feed)}, NOW, Preferences()):
        if context.layout in ("headline", "morning"):
            assert "…" in render.render_dashboard_html(context, NOW, "America/Chicago")
            frame = render.render_dashboard(context, NOW)
            frame.save(ARTIFACTS / f"{key}-{context.key}-wide.png")


def test_long_teams_and_large_missing_values():
    from test_dashboard import record

    from paiper.content import GameTeam

    schedule = game_record()["cardinals"].payload
    game = replace(
        schedule.games[0],
        home=GameTeam(
            138,
            "The Extremely Long Multi Word Baseball Team Name",
            123,
            "A Very Long Pitcher Name With Many Words",
        ),
        away=GameTeam(
            1, "Another Extremely Long Multi Word Baseball Team Name", None, None
        ),
        detailed_status="Delayed: Weather conditions",
        inning=12,
    )
    pages = build_pages(
        {"cardinals": record("cardinals", replace(schedule, games=(game,)))},
        NOW,
        Preferences(),
    )
    for context in pages:
        if context.key in ("cardinals", "morning"):
            render.render_dashboard(context, NOW).save(
                ARTIFACTS / f"{context.key}-long.png"
            )


@pytest.mark.parametrize("value", [1e100, 1e308])
def test_large_financial_values_use_explicit_notation(value):
    from test_dashboard import record

    records = {
        "blkb": record("blkb", Quote("BLKB", "USD", value, NOW, 0.001, NOW.date())),
        "usd_pen": record("usd_pen", ExchangeRate(NOW.date(), "USD", "PEN", value)),
    }
    for context in build_pages(records, NOW, Preferences()):
        if context.key in ("markets", "usd-pen"):
            assert all("inf" not in block.text.lower() for block in context.blocks)
            render.render_dashboard(context, NOW).save(
                ARTIFACTS / f"{context.key}-large.png"
            )
