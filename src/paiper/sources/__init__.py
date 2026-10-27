"""Eleven fixed, independently cached sources; no hardware or rendering imports."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from paiper.cache import ContentCache
from paiper.content import (
    Alerts,
    ExchangeRate,
    Feed,
    Flights,
    Quote,
    SourceRecord,
    Weather,
)
from paiper.sources import http, providers


def _json(parser, body):
    def invalid_constant(value):
        raise ValueError("Nonfinite JSON number")

    return parser(json.loads(body, parse_constant=invalid_constant))


def _requests(now, latitude, longitude, timezone):
    today = now.astimezone(ZoneInfo(timezone)).date()
    if today.month >= 10 or today.month <= 2:
        season = today.year if today.month >= 10 else today.year - 1
        start, end = date(season, 9, 1), date(season + 1, 6, 1)
    else:
        start, end = today - timedelta(days=30), today + timedelta(days=45)
    weather_query = urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
            "hourly": "temperature_2m,precipitation_probability,wind_speed_10m",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timeformat": "unixtime",
            "forecast_days": 2,
        }
    )
    schedule_query = urlencode(
        {
            "sportId": 1,
            "teamId": 138,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "hydrate": "linescore,probablePitcher",
            "fields": (
                "dates,date,games,gamePk,officialDate,gameDate,status,abstractGameState,"
                "detailedState,startTimeTBD,teams,home,away,team,id,name,score,probablePitcher,"
                "fullName,doubleHeader,gameNumber,linescore,currentInning,inningHalf"
            ),
        }
    )
    requests = [
        (
            "weather",
            "https://api.open-meteo.com/v1/forecast?" + weather_query,
            1800,
            partial(_json, providers.parse_weather),
        ),
        (
            "alerts",
            "https://api.weather.gov/alerts/active?"
            + urlencode(
                {
                    "point": f"{latitude},{longitude}",
                }
            ),
            300,
            partial(_json, providers.parse_alerts),
        ),
        (
            "cardinals",
            "https://statsapi.mlb.com/api/v1/schedule?" + schedule_query,
            300,
            partial(
                _json, partial(providers.parse_schedule, start_date=start, end_date=end)
            ),
        ),
    ]
    for symbol in ("BLKB", "VTI", "VOO"):
        requests.append(
            (
                symbol.lower(),
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
                300,
                partial(_json, partial(providers.parse_quote, symbol=symbol)),
            )
        )
    requests.extend(
        [
            (
                "usd_pen",
                "https://api.frankfurter.dev/v2/rate/USD/PEN",
                86400,
                partial(_json, providers.parse_fx),
            ),
            (
                "world_news",
                "https://feeds.bbci.co.uk/news/world/rss.xml",
                1800,
                providers.parse_feed,
            ),
            (
                "cardinals_news",
                "https://www.mlb.com/cardinals/feeds/news/rss.xml",
                1800,
                providers.parse_feed,
            ),
            ("ai_news", "https://openai.com/news/rss.xml", 1800, providers.parse_feed),
            (
                "flights",
                f"https://api.adsb.lol/v2/point/{latitude}/{longitude}/25",
                300,
                partial(_json, providers.parse_flights),
            ),
        ]
    )
    return requests


def _check_observation(observed, now, skew=timedelta(minutes=5)):
    if observed is not None and observed > now + skew:
        raise ValueError("Provider observation is materially in the future")


def _times(payload, now, cadence):
    if isinstance(payload, Weather):
        # Current modeled conditions can be aligned to a 15-minute interval.
        _check_observation(payload.current.observed_at, now, timedelta(minutes=15))
        return payload.current.observed_at, payload.current.valid_until
    if isinstance(payload, Flights):
        _check_observation(payload.observed_at, now, timedelta(seconds=30))
        return payload.observed_at, payload.observed_at + timedelta(seconds=120)
    if isinstance(payload, Quote):
        _check_observation(payload.observed_at, now)
        return payload.observed_at, payload.observed_at + timedelta(days=1)
    if isinstance(payload, ExchangeRate):
        # A date-only reference can differ by one day across provider time zones.
        if payload.date > now.date() + timedelta(days=1):
            raise ValueError("Provider reference date is materially in the future")
        boundary = datetime.combine(payload.date, datetime.min.time(), UTC)
        return payload.date, boundary + timedelta(days=1)
    if isinstance(payload, Alerts):
        _check_observation(payload.updated_at, now)
        for alert in payload.items:
            _check_observation(alert.issued_at, now)
        return payload.updated_at, now + timedelta(seconds=cadence)
    if isinstance(payload, Feed):
        published = max(
            (item.published_at for item in payload.items if item.published_at),
            default=None,
        )
        _check_observation(published, now)
        return published, now + timedelta(seconds=cadence)
    return None, now + timedelta(seconds=cadence)


def _fetch(url, parser):
    return parser(http.fetch(url))


def _visible(snapshot, contexts):
    records = {}
    for key, context in contexts.items():
        record = snapshot.get(key, SourceRecord(key))
        if record.payload is not None and record.context != context:
            record = replace(
                record,
                payload=None,
                fetched_at=None,
                observed_at=None,
                valid_until=None,
                error=(
                    "No cached data for requested location. " + (record.error or "")
                )[:300],
            )
        records[key] = record
    return records


def collect(
    cache: ContentCache,
    now: datetime,
    *,
    latitude: float = 35.0887,
    longitude: float = -92.4421,
    timezone: str = "America/Chicago",
    offline: bool = False,
) -> dict[str, SourceRecord]:
    """Return all source records, including explicit unavailable placeholders.

    Only due sources use the network. Stale observations remain dated, and are
    polled at the normal cadence rather than being mistaken for newly observed
    data. Offline reads never mutate cache health or attempt any network access.
    """
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    now = now.astimezone(UTC)
    latitude = providers.number(latitude, minimum=-90, maximum=90)
    longitude = providers.number(longitude, minimum=-180, maximum=180)
    requests = _requests(now, latitude, longitude, timezone)
    contexts = {
        key: json.dumps([latitude, longitude, timezone], separators=(",", ":"))
        if key in ("weather", "alerts", "flights")
        else None
        for key, *_ in requests
    }
    snapshot = cache.snapshot()
    records = _visible(snapshot, contexts)
    if offline:
        return records
    due = [
        spec
        for spec in requests
        if records[spec[0]].is_due(now)
        or records[spec[0]].attempt_context != contexts[spec[0]]
    ]
    if due:
        with ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="paiper-source"
        ) as executor:
            pending = {
                executor.submit(_fetch, url, parser): (key, cadence)
                for key, url, cadence, parser in due
            }
            for future in as_completed(pending):
                key, cadence = pending[future]
                try:
                    payload = future.result()
                    observed, valid_until = _times(payload, now, cadence)
                except (ValueError, OSError, RecursionError) as error:
                    cache.fail(
                        key,
                        now,
                        str(error) or type(error).__name__,
                        cadence=cadence,
                        retry_after=error.retry_after
                        if isinstance(error, http.SourceError)
                        else None,
                        context=contexts[key],
                    )
                else:
                    cache.update(
                        key,
                        payload,
                        now,
                        observed_at=observed,
                        valid_until=valid_until,
                        cadence=cadence,
                        context=contexts[key],
                    )
        records = _visible(cache.snapshot(), contexts)
    return records
