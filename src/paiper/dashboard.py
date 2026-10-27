"""Pure source snapshots → dated, bounded page contexts. No provider requests."""

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from importlib.resources import files
from io import BytesIO
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

from paiper.content import (
    Alerts,
    ExchangeRate,
    Feed,
    Flights,
    Quote,
    Schedule,
    SourceRecord,
    Weather,
)
from paiper.preferences import Preferences

logger = logging.getLogger(__name__)
PHOTO_BOX = (20, 60, 580, 306)


@dataclass(frozen=True)
class Block:
    label: str
    text: str
    detail: str = ""
    tone: str = ""


@dataclass(frozen=True)
class MastheadWeather:
    text: str
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class Page:
    key: str
    id: str
    title: str
    blocks: tuple[Block, ...]
    source: str
    freshness: str
    layout: str = "stack"
    photo: bytes | None = None
    content_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    weather: MastheadWeather | None = None


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # A single oversized token must still be bounded.
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(".,;:") + "…"


def content_token(ident: str) -> str:
    """Keep recent-ID state bounded even for publishers' 2000-character GUIDs."""
    return hashlib.sha256(ident.encode()).hexdigest() if len(ident) > 160 else ident


def stamp(value: datetime | date | None, timezone: str) -> str:
    if value is None:
        return "date unknown"
    if isinstance(value, datetime):
        return value.astimezone(ZoneInfo(timezone)).strftime("%b %d %Y %H:%M %Z")
    return value.strftime("%b %d %Y")


def number(value: float, *, money=False) -> str:
    prefix = "$" if money else ""
    if not math.isfinite(value):
        return prefix + "Unavailable"
    if abs(value) >= 1e15:
        return f"{prefix}{value:.2e}"
    for scale, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= scale:
            return f"{prefix}{value / scale:.2f}{suffix}"
    return f"{prefix}{value:,.2f}" if money else f"{value:,.0f}"


def outdoor_window(weather: Weather, now: datetime) -> tuple[datetime, datetime] | None:
    """First two consecutive daylight hours: 50–85°F, rain ≤20%, wind ≤15mph."""
    hours = {
        hour.at: hour
        for hour in weather.hourly
        if now <= hour.at < now + timedelta(hours=24)
    }
    for at, first in sorted(hours.items()):
        second = hours.get(at + timedelta(hours=1))
        end = at + timedelta(hours=2)
        daylight = any(
            d.sunrise and d.sunset and d.sunrise <= at and end <= d.sunset
            for d in weather.daily
        )
        if (
            second
            and daylight
            and end <= weather.valid_until
            and all(
                50 <= h.temperature_f <= 85
                and h.rain_probability <= 20
                and h.wind_mph <= 15
                for h in (first, second)
            )
        ):
            return at, end
    return None


def _photo(data: bytes) -> bytes:
    with Image.open(BytesIO(data)) as image:
        if (
            image.format not in ("PNG", "JPEG", "WEBP")
            or image.width * image.height > 24_000_000
        ):
            raise ValueError("Photo must be PNG/JPEG/WebP with at most 24 megapixels")
        oriented = ImageOps.exif_transpose(image)
        # Contain, not crop: Earthrise's horizon and personal photos remain intact.
        fitted = ImageOps.contain(oriented.convert("RGB"), (560, 246))
        frame = Image.new("RGB", (560, 246), "black")
        frame.paste(fitted, ((560 - fitted.width) // 2, (246 - fitted.height) // 2))
        output = BytesIO()
        frame.save(output, format="PNG")
        return output.getvalue()


def gallery(directory: Path | None) -> tuple[Page, ...]:
    """Inspect ≤128 directory entries, decode ≤32 images, ≤12 MiB each; no recursion."""
    pages = []
    if directory:
        try:
            with os.scandir(directory) as entries:
                candidates = sorted(islice(entries, 128), key=lambda e: e.name)
            attempts = 0
            for entry in candidates:
                if attempts >= 32:
                    break
                if entry.is_symlink() or Path(entry.name).suffix.lower() not in (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".webp",
                ):
                    continue
                try:
                    attempts += 1
                    if not entry.is_file() or entry.stat().st_size > 12 * 1024 * 1024:
                        raise ValueError("Not a regular image or over 12 MiB")
                    with open(entry.path, "rb") as source:
                        data = source.read(12 * 1024 * 1024 + 1)
                    if len(data) > 12 * 1024 * 1024:
                        raise ValueError("Photo exceeds 12 MiB")
                    photo = _photo(data)
                    ident = "photo:" + hashlib.sha256(data).hexdigest()
                    pages.append(
                        Page(
                            "gallery",
                            ident,
                            "Personal gallery",
                            (Block("", shorten(Path(entry.name).stem, 44)),),
                            "Local photo · personal collection",
                            "Bundled/local image",
                            "gallery",
                            photo,
                            (ident,),
                        )
                    )
                except (
                    OSError,
                    ValueError,
                    UnidentifiedImageError,
                    Image.DecompressionBombError,
                ) as error:
                    logger.warning("Skipping photo %s: %s", entry.name, error)
        except OSError as error:
            logger.warning("Photo directory unavailable: %s", error)
    data = files("paiper").joinpath("assets", "earthrise.jpg").read_bytes()
    pages.append(
        Page(
            "gallery",
            "photo:nasa-earthrise",
            "Earthrise",
            (Block("", "Apollo 8 · December 1968"),),
            "NASA/Bill Anders · educational collection",
            "Apollo 8 archive · December 1968",
            "gallery",
            _photo(data),
            ("photo:nasa-earthrise",),
        )
    )
    return tuple(pages)


def build_pages(
    records: dict[str, SourceRecord],
    now: datetime,
    preferences: Preferences,
    *,
    recent_ids: tuple[str, ...] = (),
) -> tuple[Page, ...]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Page generation time must be aware")
    prefs = preferences
    local = now.astimezone(ZoneInfo(prefs.timezone))
    pages = []

    def payload(key, kind):
        record = records.get(key)
        return record.payload if record and isinstance(record.payload, kind) else None

    def health(key):
        record = records.get(key)
        if not record or record.payload is None:
            return "Source unavailable"
        flags = []
        if record.error:
            flags.append("degraded")
        stale = record.is_stale(now)
        if isinstance(record.payload, Weather):
            # Forecasts outlive the short current-condition model interval.
            forecast = record.payload
            stale = (
                not forecast.valid_from <= now < forecast.valid_until
                or record.is_due(now)
            )
        if stale:
            flags.append("stale cache")
        return (
            (", ".join(flags) + " · " if flags else "")
            + "Fetched "
            + stamp(record.fetched_at, prefs.timezone)
        )

    verses = json.loads(files("paiper").joinpath("assets", "verses.json").read_text())
    verse = verses[local.date().toordinal() % len(verses)]
    verse_page = Page(
        "verse",
        "verse:" + verse["reference"],
        "Stillness",
        (Block(verse["reference"], verse["text"]),),
        "World English Bible · public domain",
        "Daily verse · complete verse",
        "verse",
    )
    weather = payload("weather", Weather)
    masthead_weather = None
    if weather and weather.valid_from <= now < weather.valid_until:
        current = weather.current
        future = next((h for h in weather.hourly if now <= h.at), None)
        if current.observed_at <= now < current.valid_until:
            temperature, label = current.temperature_f, "Modeled conditions"
            detail = "Model " + stamp(current.observed_at, prefs.timezone)
            masthead_weather = MastheadWeather(
                f"{temperature:.0f}°F model",
                current.observed_at,
                min(current.valid_until, weather.valid_until),
            )
        elif future:
            temperature, label = future.temperature_f, "Hourly forecast"
            detail = "For " + stamp(future.at, prefs.timezone)
            masthead_weather = MastheadWeather(
                f"{temperature:.0f}°F fcst",
                now,
                min(future.at + timedelta(hours=1), weather.valid_until),
            )
        else:
            temperature, label, detail = None, "Forecast", "Current model unavailable"
        day = next((d for d in weather.daily if d.date == local.date()), None)
        window = outdoor_window(weather, now)
        outdoor = (
            "Outdoor window "
            + window[0].astimezone(ZoneInfo(prefs.timezone)).strftime("%H:%M")
            + "–"
            + window[1].astimezone(ZoneInfo(prefs.timezone)).strftime("%H:%M %Z")
            if window
            else "No clear outdoor window"
        )
        blocks = (
            Block(
                label,
                f"{temperature:.0f}°F" if temperature is not None else "Unavailable",
                detail,
            ),
            Block(
                "Today",
                f"High {day.high_f:.0f}° · Low {day.low_f:.0f}°"
                if day
                else "Daily range unavailable",
                f"Rain {future.rain_probability:.0f}% · Wind {future.wind_mph:.0f} mph"
                if future
                else "Hourly data unavailable",
            ),
            Block(
                "Forecast suggestion", outdoor, "Two mild, dry, low-wind daylight hours"
            ),
        )
        pages.append(
            Page(
                "weather",
                "weather",
                shorten(prefs.location + " weather", 28),
                blocks,
                "Open-Meteo · modeled forecast",
                health("weather"),
                "weather",
            )
        )

    schedule = payload("cardinals", Schedule)
    club_reading = not schedule or not any(
        g.date == local.date()
        or (g.abstract_status == "Live" and 0 <= (local.date() - g.date).days <= 1)
        for g in schedule.games
    )
    next_game = (
        min(
            (g for g in schedule.games if g.date > local.date()),
            key=lambda g: (g.date, g.game_number or 0),
            default=None,
        )
        if schedule
        else None
    )
    if schedule and schedule.games:

        def order(game):
            return (
                0
                if game.abstract_status == "Live"
                else 1
                if game.date == local.date()
                else 2
                if game.abstract_status == "Final"
                and (local.date() - game.date).days == 1
                else 3
                if game.date > local.date()
                else 4,
                abs((game.date - local.date()).days),
                game.game_number or 0,
            )

        upcoming = sorted(schedule.games, key=order)[:6]
        previous = sorted(
            (g for g in schedule.games if g.date < local.date()),
            key=lambda g: (g.date, g.game_number or 0),
            reverse=True,
        )[:2]
        games = {game.id: game for game in (*upcoming, *previous)}
        for game in games.values():
            status = shorten(game.detailed_status, 40)
            if game.inning is not None:
                status += f" · {game.inning_half or ''} {game.inning}"
            teams = tuple(
                Block(
                    shorten(team.name, 32),
                    str(team.score) if team.score is not None else "—",
                    "Pitcher: " + shorten(team.probable_pitcher, 35)
                    if team.probable_pitcher
                    else "",
                )
                for team in (game.away, game.home)
            )
            timing = (
                "Start TBD"
                if game.start_time_tbd
                else stamp(game.start_at, prefs.timezone)
            )
            pages.append(
                Page(
                    "cardinals",
                    f"game:{game.id}",
                    "Cardinals scorecard",
                    (
                        *teams,
                        Block(
                            status,
                            timing,
                            f"Game date {game.date.isoformat()}"
                            + (
                                f" · Game {game.game_number}"
                                if game.game_number
                                else ""
                            ),
                        ),
                    ),
                    "MLB · snapshot, not pitch-by-pitch",
                    health("cardinals"),
                    "score",
                )
            )
    elif schedule:
        pages.append(
            Page(
                "cardinals",
                "schedule:empty",
                "Cardinals schedule",
                (
                    Block("MLB schedule", "No games returned"),
                    Block(
                        "Requested date window",
                        f"{schedule.start_date.isoformat()} – {schedule.end_date.isoformat()}",
                    ),
                    Block("Offseason reading", "Club headlines rotate when available"),
                ),
                "MLB · no schedule inferred",
                health("cardinals"),
            )
        )
    for key, page_key, title, source in (
        ("world_news", "news", "The headlines", "BBC World · published headlines"),
        ("ai_news", "ai", "AI notebook", "OpenAI · publisher headlines"),
        (
            "cardinals_news",
            "cardinals",
            "Cardinals notebook",
            "MLB Cardinals · publisher headlines",
        ),
    ):
        feed = payload(key, Feed)
        if feed:
            for item in feed.items[:10]:
                ident = "story:" + key + ":" + item.id
                blocks = (
                    Block(
                        "Published " + stamp(item.published_at, prefs.timezone),
                        shorten(item.title, 170),
                    ),
                )
                layout = "headline"
                if key == "cardinals_news" and club_reading:
                    layout = "club-news"
                    if next_game:
                        opponent = (
                            next_game.away
                            if next_game.home.id == 138
                            else next_game.home
                        )
                        timing = (
                            stamp(next_game.date, prefs.timezone) + " · TBD"
                            if next_game.start_time_tbd or next_game.start_at is None
                            else stamp(next_game.start_at, prefs.timezone)
                        )
                        blocks += (
                            Block(
                                "Next game · " + timing,
                                ("vs " if next_game.home.id == 138 else "@ ")
                                + shorten(opponent.name, 42),
                                health("cardinals"),
                            ),
                        )
                    else:
                        blocks += (
                            Block(
                                "Next game",
                                "Schedule unavailable"
                                if not schedule
                                else "No game returned",
                                health("cardinals"),
                            ),
                        )
                pages.append(
                    Page(
                        page_key,
                        ident,
                        title,
                        blocks,
                        source,
                        health(key),
                        layout,
                        content_ids=(ident,),
                    )
                )

    if any(payload(key, Quote) for key in ("blkb", "vti", "voo")):
        blocks = []
        for key in ("blkb", "vti", "voo"):
            quote = payload(key, Quote)
            if quote:
                percent = quote.change_percent
                tone = ""
                if percent is not None and math.isfinite(percent):
                    delta = (
                        f"{percent:+.2f}" if abs(percent) < 10000 else f"{percent:+.2e}"
                    )
                    change = delta + "%"
                    tone = (
                        "positive" if percent > 0 else "negative" if percent < 0 else ""
                    )
                else:
                    change = "Change unavailable"
                blocks.append(
                    Block(
                        key.upper() + " · " + number(quote.price, money=True),
                        change,
                        ("Degraded · " if records[key].error else "")
                        + (
                            "Stale · "
                            if records[key].is_stale(now)
                            or now >= quote.observed_at + timedelta(days=1)
                            else ""
                        )
                        + stamp(quote.observed_at, prefs.timezone),
                        tone,
                    )
                )
            else:
                blocks.append(Block(key.upper(), "Unavailable"))
        pages.append(
            Page(
                "markets",
                "markets",
                "Market Postcard",
                tuple(blocks),
                "Yahoo Finance · USD · vs previous close",
                "Delayed quotes · as-of times above",
                "markets",
            )
        )
    rate = payload("usd_pen", ExchangeRate)
    if rate:
        pages.append(
            Page(
                "usd-pen",
                "usd-pen",
                "Dollars → soles",
                tuple(
                    Block(f"US ${usd}", f"S/ {number(usd * rate.rate, money=True)[1:]}")
                    for usd in (1, 20, 100)
                ),
                "Frankfurter · daily reference, not cash",
                "Reference " + stamp(rate.date, prefs.timezone),
                "currency",
            )
        )

    flights = payload("flights", Flights)
    if flights:
        current_ids = {aircraft.id for aircraft in flights.current_at(now)}
        for aircraft in flights.aircraft[:16]:
            if aircraft.observed_at is None:
                continue
            ident = "aircraft:" + aircraft.id
            current = aircraft.id in current_ids
            altitude = (
                "On ground"
                if aircraft.on_ground
                else f"{aircraft.altitude_ft:,.0f} ft"
                if aircraft.altitude_ft is not None
                else "Altitude unknown"
            )
            distance = (
                f"{aircraft.distance_nm:.1f} nm from point"
                if aircraft.distance_nm is not None
                else "Distance unknown"
            )
            pages.append(
                Page(
                    "flights",
                    ident,
                    shorten("Above " + prefs.location, 28),
                    (
                        Block(
                            "Current position snapshot"
                            if current
                            else "Dated observation",
                            shorten(
                                aircraft.callsign
                                or aircraft.registration
                                or aircraft.id,
                                30,
                            ),
                            stamp(aircraft.observed_at, prefs.timezone),
                        ),
                        Block(
                            aircraft.aircraft_type or "Type unknown", altitude, distance
                        ),
                    ),
                    "adsb.lol · observations, not radar",
                    health("flights"),
                    "flights",
                    content_ids=(ident,),
                )
            )

    alerts = payload("alerts", Alerts)
    if alerts:
        for alert in alerts.active_at(now):
            if alert.severity not in ("Severe", "Extreme"):
                continue
            pages.append(
                Page(
                    "alert",
                    "alert:" + alert.id,
                    "Weather notice",
                    (
                        Block(shorten(alert.event, 52), shorten(alert.headline, 135)),
                        Block(
                            "Issued " + stamp(alert.issued_at, prefs.timezone),
                            "Expires " + stamp(alert.expires_at, prefs.timezone),
                        ),
                    ),
                    "NWS · supplementary notice only",
                    "Follow official warnings & local instructions",
                    "alert",
                    expires_at=alert.expires_at,
                )
            )

    # A small editorial front page, never a dense dashboard of invented defaults.
    front = []
    front_ids = []

    def story_rank(page):
        ident = content_token(page.id)
        return recent_ids.index(ident) + 1 if ident in recent_ids else 0

    headline = min((p for p in pages if p.key == "news"), key=story_rank, default=None)
    if headline:
        front.append(
            Block(
                "BBC · " + headline.blocks[0].label,
                shorten(headline.blocks[0].text, 62),
            )
        )
        front_ids.extend(headline.content_ids)
    weather_page = next((p for p in pages if p.key == "weather"), None)
    if weather_page:
        front.append(
            Block(
                prefs.location + " · " + weather_page.blocks[0].detail,
                weather_page.blocks[0].text
                + " · "
                + (weather_page.blocks[1].detail or "Forecast"),
            )
        )
    today = (
        next((g for g in schedule.games if g.date == local.date()), None)
        if schedule
        else None
    )
    game_record = records.get("cardinals")
    cached_game = not (
        game_record
        and game_record.fetched_at
        and not game_record.error
        and timedelta(0) <= now - game_record.fetched_at <= timedelta(minutes=10)
    )
    game_label = (
        "MLB · "
        + ("cached · " if cached_game and today else "fetched · ")
        + stamp(game_record.fetched_at if game_record else None, prefs.timezone)
    )
    front.append(
        Block(
            game_label,
            shorten(
                f"{today.away.name} / {today.home.name} · {today.detailed_status}", 60
            )
            if today
            else "No game returned today"
            if schedule
            else "Schedule unavailable",
        )
    )
    # Select a complete short verse for the compact front page, not a rewritten excerpt.
    short_verse = next(v for v in verses if len(v["text"]) <= 65)
    front.append(Block(short_verse["reference"] + " · WEB", short_verse["text"]))
    if headline or weather_page or schedule:
        pages.append(
            Page(
                "morning",
                "morning",
                "Morning Paper",
                tuple(front),
                "BBC / Open-Meteo / MLB / WEB",
                "Forecast / fetched / published dates above",
                "morning",
                content_ids=tuple(front_ids),
            )
        )
    unavailable = not pages
    if unavailable:
        verse_page = replace(
            verse_page, freshness="Live sources unavailable · bundled verse"
        )
    pages.append(verse_page)
    for photo in gallery(prefs.photo_directory):
        if unavailable:
            photo = replace(
                photo, freshness="Live sources unavailable · bundled/local art"
            )
        pages.append(photo)
    # Keep scorecards available to priority selection; offseason normal slots
    # lead with sourced reporting and carry the next game as context.
    pages.sort(key=lambda p: p.layout != "club-news")
    return tuple(replace(page, weather=masthead_weather) for page in pages)
