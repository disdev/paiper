"""Normalization of public provider responses."""

import math
import re
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import wraps
from html.parser import HTMLParser
from itertools import pairwise
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from paiper.content import (
    Aircraft,
    Alert,
    Alerts,
    Article,
    ExchangeRate,
    Feed,
    Flights,
    Game,
    GameTeam,
    Quote,
    Schedule,
    Weather,
    WeatherCurrent,
    WeatherDay,
    WeatherHour,
)


def _parser(function):
    @wraps(function)
    def normalize(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (
            KeyError,
            TypeError,
            IndexError,
            AttributeError,
            OverflowError,
            ZoneInfoNotFoundError,
            ElementTree.ParseError,
        ) as error:
            raise ValueError(
                f"Invalid {function.__name__.removeprefix('parse_')} response: {error}"
            ) from error

    return normalize


def number(value, *, minimum=None, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Expected a finite number")
    if (
        minimum is not None
        and value < minimum
        or maximum is not None
        and value > maximum
    ):
        raise ValueError("Number outside expected range")
    return float(value)


def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("Expected an integer")
    return value


def _optional_number(value, **kwargs):
    return None if value is None else number(value, **kwargs)


def instant(value, zone=None):
    if type(value) in (int, float):
        return datetime.fromtimestamp(number(value, minimum=0), UTC)
    if not isinstance(value, str):
        raise TypeError("Expected timestamp")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        if zone is None:
            raise ValueError("Timestamp has no timezone")
        result = result.replace(tzinfo=zone)
    return result.astimezone(UTC)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag in ("p", "div"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, limit=240):
    if not isinstance(value, str):
        raise TypeError("Expected text")
    parser = _Text()
    parser.feed(value)
    result = " ".join("".join(parser.parts).split())[:limit]
    if not result:
        raise ValueError("Missing text")
    return result


def _optional_text(value, limit=240):
    return None if value is None or value == "" else _text(value, limit)


def _identity(value, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Missing or oversized identity")
    return value.strip()


def _array(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("Invalid or oversized array")
    return value


def _rows(data, names, limit):
    arrays = [_array(data[name], limit) for name in names]
    if not arrays[0] or len({len(array) for array in arrays}) != 1:
        raise ValueError("Missing or misaligned arrays")
    return zip(*arrays)


def _units(data, wanted):
    if any(data.get(key) != value for key, value in wanted.items()):
        raise ValueError("Unexpected weather units")


@_parser
def parse_weather(data: dict) -> Weather:
    zone = ZoneInfo(data["timezone"])
    _units(data["current_units"], {"temperature_2m": "°F", "wind_speed_10m": "mp/h"})
    _units(
        data["daily_units"], {"temperature_2m_max": "°F", "temperature_2m_min": "°F"}
    )
    _units(
        data["hourly_units"],
        {
            "temperature_2m": "°F",
            "wind_speed_10m": "mp/h",
            "precipitation_probability": "%",
        },
    )
    raw = data["current"]
    at = instant(raw["time"], zone)
    current = WeatherCurrent(
        at,
        at + timedelta(seconds=number(raw["interval"], minimum=1, maximum=3600)),
        number(raw["temperature_2m"]),
        _integer(raw["weather_code"]),
        number(raw["wind_speed_10m"], minimum=0),
    )
    hourly = tuple(
        WeatherHour(
            instant(at, zone),
            number(temp),
            number(rain, minimum=0, maximum=100),
            number(wind, minimum=0),
        )
        for at, temp, rain, wind in _rows(
            data["hourly"],
            (
                "time",
                "temperature_2m",
                "precipitation_probability",
                "wind_speed_10m",
            ),
            384,
        )
    )
    if any(b.at <= a.at for a, b in pairwise(hourly)):
        raise ValueError("Weather hours are not chronological")
    daily = []
    for day, high, low, sunrise, sunset in _rows(
        data["daily"],
        (
            "time",
            "temperature_2m_max",
            "temperature_2m_min",
            "sunrise",
            "sunset",
        ),
        16,
    ):
        day = (
            instant(day).astimezone(zone).date()
            if type(day) in (float, int)
            else date.fromisoformat(day)
        )
        high, low = number(high), number(low)
        if low > high:
            raise ValueError("Weather high is below low")
        daily.append(
            WeatherDay(
                day,
                high,
                low,
                None if sunrise is None else instant(sunrise, zone),
                None if sunset is None else instant(sunset, zone),
            )
        )
    return Weather(
        number(data["latitude"], minimum=-90, maximum=90),
        number(data["longitude"], minimum=-180, maximum=180),
        data["timezone"],
        current,
        hourly,
        tuple(daily),
        hourly[0].at,
        hourly[-1].at + timedelta(hours=1),
    )


@_parser
def parse_alerts(data: dict) -> Alerts:
    alerts = []
    for feature in _array(data["features"], 200):
        raw = feature["properties"]
        issued = instant(raw["sent"])
        expires = instant(raw["expires"])
        effective = instant(raw["effective"]) if raw.get("effective") else issued
        if expires <= issued:
            raise ValueError("Alert expiry precedes issue time")
        alerts.append(
            Alert(
                _identity(feature.get("id") or raw["id"]),
                issued,
                expires,
                effective,
                _text(raw["severity"]),
                _text(raw["event"]),
                _text(raw["headline"]),
            )
        )
    return Alerts(
        instant(data["updated"]) if data.get("updated") else None, tuple(alerts)
    )


@_parser
def parse_quote(data: dict, symbol: str) -> Quote:
    chart = data["chart"]
    if chart.get("error") is not None:
        raise ValueError("Yahoo chart reported an error")
    results = _array(chart["result"], 1)
    raw = results[0]
    meta = raw["meta"]
    if meta["symbol"] != symbol or meta["currency"] != "USD":
        raise ValueError("Unexpected quote symbol or currency")
    price = number(meta["regularMarketPrice"], minimum=0.0000001)
    observed = instant(meta["regularMarketTime"])
    zone = ZoneInfo(meta["exchangeTimezoneName"])
    session_date = observed.astimezone(zone).date()
    previous, previous_date = None, None
    timestamps = _array(raw.get("timestamp", []), 100)
    quotes = raw.get("indicators", {}).get("quote", [])
    closes = _array(quotes[0].get("close", []), 100) if quotes else []
    if len(timestamps) != len(closes):
        raise ValueError("Misaligned quote bars")
    prior = []
    for at, close in zip(timestamps, closes):
        at = instant(at)
        close = _optional_number(close, minimum=0.0000001)
        if at.astimezone(zone).date() < session_date:
            prior.append((at, close))
    if prior:
        at, previous = max(prior, key=lambda item: item[0])
        previous_date = at.astimezone(zone).date() if previous is not None else None
    if previous is None and meta.get("previousClose") is not None:
        previous = number(meta["previousClose"], minimum=0.0000001)
    elif previous is None and meta.get("regularMarketPreviousClose") is not None:
        previous = number(meta["regularMarketPreviousClose"], minimum=0.0000001)
    return Quote(symbol, "USD", price, observed, previous, previous_date)


@_parser
def parse_fx(data: dict) -> ExchangeRate:
    if data["base"] != "USD" or data["quote"] != "PEN":
        raise ValueError("Unexpected reference-rate currencies")
    return ExchangeRate(
        date.fromisoformat(data["date"]),
        "USD",
        "PEN",
        number(data["rate"], minimum=0.0000001),
    )


def _team(raw):
    return GameTeam(
        _integer(raw["team"]["id"], minimum=1),
        _text(raw["team"]["name"]),
        _integer(raw["score"]) if raw.get("score") is not None else None,
        _optional_text(raw.get("probablePitcher", {}).get("fullName")),
    )


@_parser
def parse_schedule(data: dict, start_date: date, end_date: date) -> Schedule:
    games = {}
    for day in _array(data["dates"], 500):
        for raw in _array(day["games"], 20):
            status = raw["status"]
            home, away = _team(raw["teams"]["home"]), _team(raw["teams"]["away"])
            if 138 not in (home.id, away.id):
                raise ValueError("Schedule contains another team's game")
            tbd = status.get("startTimeTBD", False)
            if not isinstance(tbd, bool):
                raise TypeError("Invalid TBD flag")
            start = None if tbd or not raw.get("gameDate") else instant(raw["gameDate"])
            linescore = raw.get("linescore", {})
            game = Game(
                _integer(raw["gamePk"], minimum=1),
                date.fromisoformat(raw["officialDate"]),
                start,
                tbd,
                _text(status["detailedState"]),
                _text(status["abstractGameState"]),
                _optional_text(raw.get("doubleHeader")),
                _integer(raw["gameNumber"], minimum=1)
                if raw.get("gameNumber") is not None
                else None,
                home,
                away,
                _integer(linescore["currentInning"], minimum=1)
                if linescore.get("currentInning")
                else None,
                _optional_text(linescore.get("inningHalf")),
            )
            if not start_date <= game.date <= end_date:
                raise ValueError("Game outside requested date range")
            games[game.id] = game
    if len(games) > 500:
        raise ValueError("Too many games")
    return Schedule(
        start_date,
        end_date,
        tuple(
            sorted(
                games.values(),
                key=lambda game: (game.date, game.game_number or 0, game.id),
            )
        ),
    )


@_parser
def parse_feed(data: bytes) -> Feed:
    if not isinstance(data, bytes) or len(data) > 2 * 1024 * 1024:
        raise ValueError("Invalid feed body")
    text = data.decode("utf-8-sig")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("Feed declarations/entities are not supported")
    root = ElementTree.fromstring(text)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Expected publisher RSS")
    articles = []
    seen = set()
    for raw in channel.findall("item"):
        link = _identity(raw.findtext("link"))
        parsed = urlparse(link)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
        ):
            raise ValueError("Invalid publisher link")
        identifier = _identity(raw.findtext("guid") or link)
        if identifier in seen:
            continue
        published = raw.findtext("pubDate")
        if published:
            published = parsedate_to_datetime(published)
            if published.utcoffset() is None:
                raise ValueError("Publication date has no timezone")
            published = published.astimezone(UTC)
        else:
            published = None
        articles.append(
            Article(identifier, _text(raw.findtext("title")), link, published)
        )
        seen.add(identifier)
        if len(articles) >= 40:
            break
    return Feed(_optional_text(channel.findtext("title")), tuple(articles))


@_parser
def parse_flights(data: dict) -> Flights:
    if data.get("msg") not in (None, "No error"):
        raise ValueError("Aircraft provider reported an error")
    at = instant(number(data["now"], minimum=0) / 1000)
    aircraft = []
    for raw in _array(data["ac"], 500):
        altitude = raw.get("alt_baro")
        if altitude is None:
            altitude = raw.get("alt_geom")
        seen_pos = raw.get("seen_pos")
        observed = (
            None
            if seen_pos is None
            else at - timedelta(seconds=number(seen_pos, minimum=0))
        )
        aircraft.append(
            Aircraft(
                _identity(raw["hex"], 40),
                _optional_text(raw.get("flight"), 40),
                _optional_text(raw.get("r"), 40),
                _optional_text(raw.get("t"), 40),
                None if altitude == "ground" else _optional_number(altitude),
                True if altitude == "ground" else None,
                _optional_number(raw.get("dst"), minimum=0),
                _optional_number(raw.get("dir"), minimum=0, maximum=360),
                _optional_number(raw.get("lat"), minimum=-90, maximum=90),
                _optional_number(raw.get("lon"), minimum=-180, maximum=180),
                observed,
            )
        )
    return Flights(at, tuple(aircraft))
