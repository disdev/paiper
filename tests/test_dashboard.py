from datetime import UTC, datetime, timedelta

import pytest

from paiper.content import (
    Alert,
    Alerts,
    Article,
    Feed,
    Quote,
    SourceRecord,
    Weather,
    WeatherCurrent,
    WeatherDay,
    WeatherHour,
)

NOW = datetime(2026, 10, 27, 16, tzinfo=UTC)


def record(key, payload, age=0):
    return SourceRecord(
        key,
        payload,
        NOW - timedelta(hours=age),
        valid_until=NOW - timedelta(hours=age) + timedelta(minutes=5),
    )


def weather():
    return Weather(
        35.0887,
        -92.4421,
        "America/Chicago",
        WeatherCurrent(NOW - timedelta(hours=1), NOW, 70, 0, 4),
        tuple(WeatherHour(NOW + timedelta(hours=i), 72, 10, 5) for i in range(6)),
        (WeatherDay(NOW.date(), 78, 55, NOW, NOW + timedelta(hours=6)),),
        NOW - timedelta(hours=1),
        NOW + timedelta(hours=6),
    )


def build(records, now=NOW):
    from paiper.dashboard import build_pages
    from paiper.preferences import Preferences

    return build_pages(records, now, Preferences())


def text(page):
    return " ".join(
        [page.title, page.source, page.freshness]
        + [f"{b.label} {b.text} {b.detail}" for b in page.blocks]
    )


def test_independent_quotes_and_original_dates():
    quote = Quote("BLKB", "USD", 51.23, NOW - timedelta(days=4), None, None)
    page = next(
        p for p in build({"blkb": record("blkb", quote, 4)}) if p.key == "markets"
    )
    assert "51.23" in text(page)
    assert "Oct 23" in text(page)
    assert "unavailable" in text(page).lower()
    assert "stale" in text(page).lower()
    assert "OFFLINE" not in text(page)


def test_shared_masthead_uses_local_date_and_only_valid_weather():
    from dataclasses import replace

    from paiper.render import render_dashboard_html

    value = weather()
    records = {"weather": record("weather", value)}
    for page in build(records):
        html = render_dashboard_html(page, NOW, "America/Chicago")
        header = html.split("<header", 1)[1].split("</header>", 1)[0]
        assert "PAIPER" in header
        assert "Tue Oct 27" in header
        assert "Updated 11:00 CDT" in header
        assert "72°F fcst" in header
        expired = render_dashboard_html(
            page, NOW + timedelta(hours=7), "America/Chicago"
        )
        assert "72°F" not in expired.split("</header>", 1)[0]
    value = replace(
        value, current=replace(value.current, valid_until=NOW + timedelta(minutes=15))
    )
    page = build({"weather": record("weather", value)})[0]
    assert "70°F model" in render_dashboard_html(page, NOW, "America/Chicago")
    fallback = build({})[0]
    html = render_dashboard_html(fallback, NOW.replace(hour=2), "America/Chicago")
    assert "Mon Oct 26" in html and "Updated 21:00 CDT" in html
    assert "°F" not in html.split("</header>", 1)[0]


def test_flight_focal_data_and_concise_notices():
    from paiper.content import Aircraft, Flights

    aircraft = Aircraft(
        "abc123", "N92CF", None, "SIRA", 1070, None, 9.1, 55, 35.1, -92.3, NOW
    )
    page = next(
        p
        for p in build({"flights": record("flights", Flights(NOW, (aircraft,)))})
        if p.key == "flights"
    )
    assert page.blocks[0].text == "N92CF"
    assert page.blocks[1].text == "1,070 ft"
    assert "Oct 27 2026 11:00 CDT" in text(page)
    assert "observations, not radar" in page.source
    assert "No route inferred" not in text(page)
    assert len(page.blocks) == 2
    quote = Quote("BLKB", "USD", 50, NOW, 51, NOW.date())
    market = next(
        p for p in build({"blkb": record("blkb", quote)}) if p.key == "markets"
    )
    assert "Delayed" in market.freshness and "as-of" in market.freshness
    assert "session status inferred" not in text(market)
    assert "no endorsement" not in text(
        next(p for p in build({}) if p.key == "gallery")
    )


@pytest.mark.parametrize(
    ("price", "previous", "tone", "change"),
    [
        (51, 50, "positive", "+2.00%"),
        (49, 50, "negative", "-2.00%"),
        (50, 50, "", "+0.00%"),
        (1e308, 0.001, "", "Change unavailable"),
    ],
)
def test_quote_accents_require_a_finite_signed_change(price, previous, tone, change):
    page = next(
        p
        for p in build(
            {
                "blkb": record(
                    "blkb", Quote("BLKB", "USD", price, NOW, previous, NOW.date())
                )
            }
        )
        if p.key == "markets"
    )
    assert page.blocks[0].text == change
    assert page.blocks[0].tone == tone


def test_forecast_outlives_current_model_but_expires():
    from paiper.dashboard import outdoor_window

    records = {"weather": record("weather", weather(), 1)}
    page = next(p for p in build(records) if p.key == "weather")
    assert "forecast" in text(page).lower()
    assert outdoor_window(weather(), NOW) == (NOW, NOW + timedelta(hours=2))
    assert not any(p.key == "weather" for p in build(records, NOW + timedelta(hours=7)))


def test_alerts_expire_at_selection_time():
    from paiper.playlist import select_page
    from paiper.preferences import Preferences

    alert = Alert(
        "a",
        NOW,
        NOW + timedelta(minutes=1),
        NOW,
        "Severe",
        "Tornado Warning",
        "Take shelter now.",
    )
    records = {"alerts": record("alerts", Alerts(NOW, (alert,)))}
    available = build(records)
    assert select_page(available, records, NOW, Preferences(), {}).page.key == "alert"
    later = NOW + timedelta(minutes=1)
    assert select_page(available, records, later, Preferences(), {}).page.key != "alert"


def test_news_dates_escaping_and_editorial_ellipsis():
    title = '<script>alert("x")</script> ' + "Very long headline " * 20
    feed = Feed("BBC", (Article("story", title, "https://example.org", NOW),))
    page = next(
        p for p in build({"world_news": record("world_news", feed)}) if p.key == "news"
    )
    from paiper.render import render_dashboard_html

    html = render_dashboard_html(page, NOW, "America/Chicago")
    assert "<script>" not in html
    assert "…" in html
    assert "Oct 27" in html
    assert "OFFLINE" not in html


def test_verified_verse_rotation_and_gallery():
    first = build({})
    tomorrow = build({}, NOW + timedelta(days=1))
    verse = next(p for p in first if p.key == "verse")
    assert verse.id != next(p.id for p in tomorrow if p.key == "verse")
    assert "World English Bible" in text(verse)
    assert "sources unavailable" in text(verse).lower()
    assert next(p for p in first if p.key == "gallery").photo is not None


@pytest.mark.parametrize(
    "yaml",
    [
        "unknown: true",
        "latitude: true",
        "latitude: 91",
        "longitude: .nan",
        "timezone: Mars/Nowhere",
        "photo_directory: []",
        "playlists: {day: [unknown]}",
        "playlists: {day: []}",
        "playlists: {dawn: [verse]}",
    ],
)
def test_strict_preferences(tmp_path, yaml):
    from paiper.preferences import load_preferences

    path = tmp_path / "dashboard.yaml"
    path.write_text(yaml)
    with pytest.raises(ValueError):
        load_preferences(path)


def test_preferences_separate_optional_and_relative_photos(tmp_path):
    from paiper.preferences import load_preferences

    panel = tmp_path / "panel.yaml"
    panel.write_text("panel:\n  rotation: 270\n")
    assert load_preferences(panel_path=panel).timezone == "America/Chicago"
    assert not (tmp_path / "dashboard.yaml").exists()
    (tmp_path / "dashboard.yaml").write_text(
        "location: Little Rock\nlatitude: 34.7\nphoto_directory: photos\n"
    )
    prefs = load_preferences(panel_path=panel)
    assert prefs.location == "Little Rock"
    assert prefs.photo_directory == tmp_path / "photos"


def test_dashboard_has_an_offline_cli_preview(tmp_path):
    from PIL import Image

    from paiper.cli import main
    from paiper.panel import RGB_TO_CODE

    output = tmp_path / "dashboard.png"
    assert (
        main(
            [
                "preview",
                "--scene",
                "dashboard",
                "--offline",
                "--cache",
                str(tmp_path / "content.sqlite3"),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    with Image.open(output) as image:
        assert image.size == (600, 400)
        assert image.mode == "RGB"
        assert {color for _, color in image.getcolors(240000)} <= set(RGB_TO_CODE)


def test_empty_schedule_is_a_useful_dated_page():
    from paiper.content import Schedule

    pages = build(
        {"cardinals": record("cardinals", Schedule(NOW.date(), NOW.date(), ()))}
    )
    page = next(p for p in pages if p.key == "cardinals")
    assert "No games returned" in text(page)
    assert "2026-10-27" in text(page)


def test_quote_health_does_not_rewrite_observation_age():
    quote = Quote("BLKB", "USD", 51.23, NOW - timedelta(days=4), None, None)
    page = next(p for p in build({"blkb": record("blkb", quote)}) if p.key == "markets")
    assert "Stale" in text(page)


def test_local_photos_exif_bad_files_and_bounds(tmp_path, caplog):
    from PIL import Image

    from paiper.dashboard import gallery

    (tmp_path / "bad.jpg").write_bytes(b"not a photograph")
    image = Image.new("RGB", (30, 60), "red")
    exif = image.getexif()
    exif[274] = 6
    image.save(tmp_path / "rotated.jpg", exif=exif)
    result = gallery(tmp_path)
    assert len(result) == 2  # Local image plus bundled Earthrise.
    from io import BytesIO

    with Image.open(BytesIO(result[0].photo)) as frame:
        assert frame.getpixel((80, 123))[0] > 200  # EXIF rotates into a wide image.
        assert frame.getpixel((280, 2))[0] > 200
    assert "bad.jpg" in caplog.text


def test_outdoor_window_requires_contiguous_daylight_and_thresholds():
    from dataclasses import replace

    from paiper.dashboard import outdoor_window

    value = weather()
    assert outdoor_window(replace(value, daily=()), NOW) is None
    assert outdoor_window(replace(value, hourly=value.hourly[::2]), NOW) is None
    for field, bad in (
        ("temperature_f", 90),
        ("rain_probability", 21),
        ("wind_mph", 16),
    ):
        assert (
            outdoor_window(
                replace(
                    value,
                    hourly=tuple(replace(h, **{field: bad}) for h in value.hourly),
                ),
                NOW,
            )
            is None
        )


def test_morning_uses_unseen_story_and_labels_cached_game():
    from test_playlist import game_record

    from paiper.dashboard import build_pages
    from paiper.preferences import Preferences

    feed = Feed(
        "BBC",
        (
            Article("first", "First story", "https://example.org/a", NOW),
            Article("second", "Second story", "https://example.org/b", NOW),
        ),
    )
    records = {**game_record(age=60), "world_news": record("world_news", feed)}
    context = next(
        p
        for p in build_pages(
            records, NOW, Preferences(), recent_ids=("story:world_news:first",)
        )
        if p.key == "morning"
    )
    assert "Second story" in text(context)
    assert "cached" in text(context).lower()
    assert "First story" not in text(context)


def test_photo_decode_attempts_are_bounded(tmp_path, monkeypatch):
    from paiper import dashboard

    for index in range(60):
        (tmp_path / f"{index}.png").write_bytes(b"broken")
    decode = dashboard._photo
    attempts = []

    def count(data):
        attempts.append(True)
        return decode(data)

    monkeypatch.setattr(dashboard, "_photo", count)
    assert len(dashboard.gallery(tmp_path)) == 1
    assert len(attempts) <= 33  # 32 local attempts plus the bundled asset.


def test_recent_final_remains_available_ahead_of_future_schedule():
    from dataclasses import replace

    from test_playlist import game_record

    record = game_record("Final")["cardinals"]
    final = replace(record.payload.games[0], date=(NOW - timedelta(days=1)).date())
    future = tuple(
        replace(
            final,
            id=100 + i,
            date=(NOW + timedelta(days=i + 1)).date(),
            abstract_status="Preview",
            detailed_status="Scheduled",
        )
        for i in range(20)
    )
    records = {
        "cardinals": replace(
            record, payload=replace(record.payload, games=(*future, final))
        )
    }
    assert any(page.id == "game:42" for page in build(records))


def test_publisher_local_ids_do_not_collide():
    feed = Feed("Publisher", (Article("1", "A headline", "https://example.org", NOW),))
    pages = build({key: record(key, feed) for key in ("world_news", "ai_news")})
    assert next(p.id for p in pages if p.key == "news") != next(
        p.id for p in pages if p.key == "ai"
    )
