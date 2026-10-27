import io
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from paiper.cache import ContentCache
from paiper.sources import collect, http, providers

NOW = datetime(2026, 10, 27, 15, 0, tzinfo=UTC)


def weather():
    return {
        "latitude": 35.09,
        "longitude": -92.44,
        "timezone": "America/Chicago",
        "current_units": {"temperature_2m": "°F", "wind_speed_10m": "mp/h"},
        "daily_units": {"temperature_2m_max": "°F", "temperature_2m_min": "°F"},
        "hourly_units": {
            "temperature_2m": "°F",
            "wind_speed_10m": "mp/h",
            "precipitation_probability": "%",
        },
        "current": {
            "time": "2026-10-27T10:00",
            "interval": 900,
            "temperature_2m": 65.0,
            "weather_code": 3,
            "wind_speed_10m": 4,
        },
        "daily": {
            "time": ["2026-10-27"],
            "temperature_2m_max": [77.2],
            "temperature_2m_min": [48.6],
            "sunrise": ["2026-10-27T07:26"],
            "sunset": ["2026-10-27T18:20"],
        },
        "hourly": {
            "time": ["2026-10-27T10:00", "2026-10-27T11:00"],
            "temperature_2m": [65, 69],
            "wind_speed_10m": [4, 5],
            "precipitation_probability": [0, 20],
        },
    }


def quote(symbol="BLKB"):
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": "USD",
                        "regularMarketPrice": 53.85,
                        "regularMarketTime": NOW.timestamp(),
                        "exchangeTimezoneName": "America/New_York",
                        "chartPreviousClose": 42,
                    },
                    "timestamp": [
                        datetime(2026, 10, 23, 13, 30, tzinfo=UTC).timestamp(),
                        datetime(2026, 10, 26, 13, 30, tzinfo=UTC).timestamp(),
                        datetime(2026, 10, 27, 13, 30, tzinfo=UTC).timestamp(),
                    ],
                    "indicators": {"quote": [{"close": [54, 55.07, 53.85]}]},
                }
            ],
        }
    }


def test_weather_uses_local_timezone_units_and_validity():
    payload = providers.parse_weather(weather())
    assert payload.current.observed_at == NOW
    assert payload.current.valid_until == NOW + timedelta(minutes=15)
    assert payload.current.temperature_f == 65
    assert payload.current.wind_mph == 4
    assert payload.daily[0].sunrise == datetime(2026, 10, 27, 12, 26, tzinfo=UTC)
    assert payload.daily[0].date == date(2026, 10, 27)
    assert payload.hourly[1].rain_probability == 20
    assert payload.valid_until == NOW + timedelta(hours=2)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["current"].pop("temperature_2m"),
        lambda d: d["current"].update(temperature_2m=float("nan")),
        lambda d: d["current"].update(wind_speed_10m=float("inf")),
        lambda d: d["current_units"].update(temperature_2m="°C"),
        lambda d: d["hourly"].update(temperature_2m=[1]),
        lambda d: d["hourly"].update(precipitation_probability=[0, 101]),
        lambda d: d["current"].update(time="not a date"),
        lambda d: d.update(timezone="Not/AZone"),
    ],
)
def test_malformed_weather_is_not_normalized_as_success(change):
    data = weather()
    change(data)
    with pytest.raises(ValueError):
        providers.parse_weather(data)


def test_weather_epoch_times_are_unambiguous_across_dst():
    data = weather()
    data["current"]["time"] = 1793514600  # 2026-11-01 06:30 UTC, first 01:30 local.
    data["hourly"]["time"] = [1793514600, 1793518200]
    payload = providers.parse_weather(data)
    assert payload.hourly[1].at - payload.hourly[0].at == timedelta(hours=1)
    assert payload.current.observed_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_quote_previous_close_is_previous_actual_session_not_range_close():
    result = providers.parse_quote(quote(), "BLKB")
    assert result.symbol == "BLKB"
    assert result.observed_at == NOW
    assert result.previous_close == 55.07
    assert result.previous_close_date == date(2026, 10, 26)
    assert result.change == pytest.approx(-1.22)
    assert result.change_percent == pytest.approx(-2.215362, abs=1e-5)


def test_quote_retains_old_date_and_never_invents_previous_close():
    data = quote()
    result = data["chart"]["result"][0]
    result["timestamp"] = [NOW.timestamp()]
    result["indicators"]["quote"][0]["close"] = [53.85]
    parsed = providers.parse_quote(data, "BLKB")
    assert parsed.previous_close is None
    assert parsed.change is None
    result["meta"]["previousClose"] = 55
    assert providers.parse_quote(data, "BLKB").previous_close == 55
    result["meta"]["regularMarketTime"] = NOW.timestamp() - 4 * 86400
    assert providers.parse_quote(data, "BLKB").observed_at == NOW - timedelta(days=4)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "VOO"),
        ("currency", "EUR"),
        ("regularMarketPrice", None),
        ("regularMarketPrice", float("nan")),
        ("regularMarketPrice", True),
        ("regularMarketTime", "today"),
        ("regularMarketTime", -1),
        ("regularMarketPrice", -3),
    ],
)
def test_invalid_quote_identity_and_numbers_fail(field, value):
    data = quote()
    data["chart"]["result"][0]["meta"][field] = value
    with pytest.raises(ValueError):
        providers.parse_quote(data, "BLKB")


def test_fx_reference_date_currency_and_finite_positive_rate():
    data = {"date": "2026-10-26", "base": "USD", "quote": "PEN", "rate": 3.4443}
    result = providers.parse_fx(data)
    assert result.date == date(2026, 10, 26)
    assert result.rate == 3.4443
    for field, value in [
        ("date", "today"),
        ("base", "EUR"),
        ("quote", "USD"),
        ("rate", 0),
        ("rate", float("inf")),
        ("rate", "3.4"),
    ]:
        with pytest.raises(ValueError):
            providers.parse_fx({**data, field: value})


def test_empty_alerts_are_healthy_but_expired_or_future_alerts_are_ineligible():
    assert providers.parse_alerts({"features": []}).active_at(NOW) == ()
    data = {
        "updated": NOW.isoformat(),
        "features": [
            {
                "id": "nws:1",
                "properties": {
                    "sent": "2026-10-27T09:00:00-05:00",
                    "expires": "2026-10-27T11:00:00-05:00",
                    "effective": "2026-10-27T09:00:00-05:00",
                    "severity": "Severe",
                    "event": "Wind Advisory",
                    "headline": "Wind <b>advisory</b>",
                },
            }
        ],
    }
    payload = providers.parse_alerts(data)
    assert payload.items[0].headline == "Wind advisory"
    assert payload.items[0].issued_at == NOW - timedelta(hours=1)
    assert len(payload.active_at(NOW)) == 1
    assert payload.active_at(NOW + timedelta(hours=1)) == ()
    assert payload.active_at(NOW - timedelta(hours=2)) == ()
    data["features"][0]["properties"].pop("expires")
    with pytest.raises(ValueError):
        providers.parse_alerts(data)


def game(pk=1):
    return {
        "gamePk": pk,
        "officialDate": "2027-03-25",
        "gameDate": "2027-03-25T00:00:00Z",
        "status": {
            "abstractGameState": "Preview",
            "detailedState": "Scheduled",
            "startTimeTBD": True,
        },
        "teams": {
            "home": {
                "team": {"id": 138, "name": "St. Louis Cardinals"},
                "probablePitcher": {"fullName": "Pitcher"},
            },
            "away": {"team": {"id": 112, "name": "Chicago Cubs"}},
        },
        "doubleHeader": "Y",
        "gameNumber": pk,
    }


def test_schedule_preserves_tbd_doubleheader_detailed_status_and_missing_scores():
    first, second = game(), game(2)
    second["status"] = {"abstractGameState": "Live", "detailedState": "Delayed: Rain"}
    second["teams"]["home"]["score"] = 0
    second["teams"]["away"]["score"] = 2
    second["linescore"] = {"currentInning": 3, "inningHalf": "Top"}
    payload = providers.parse_schedule(
        {"dates": [{"games": [first, second]}]},
        date(2026, 9, 1),
        date(2027, 5, 1),
    )
    assert len(payload.games) == 2
    assert payload.games[0].start_at is None
    assert payload.games[0].home.score is None
    assert payload.games[0].home.probable_pitcher == "Pitcher"
    assert payload.games[1].detailed_status == "Delayed: Rain"
    assert payload.games[1].home.score == 0
    assert payload.games[1].inning == 3
    assert payload.games[1].game_number == 2
    assert payload.games[0].doubleheader == "Y"
    first["teams"]["home"]["team"]["id"] = 999
    with pytest.raises(ValueError):
        providers.parse_schedule(
            {"dates": [{"games": [first]}]}, date(2026, 9, 1), date(2027, 5, 1)
        )


def test_rss_strips_markup_bounds_titles_preserves_ids_links_and_optional_dates():
    data = b"""<rss><channel><title>Publisher</title><item><guid>a</guid>
    <title>&lt;b&gt;AI &amp;amp; news&lt;/b&gt;</title><link>https://example.com/a</link>
    <pubDate>Tue, 27 Oct 2026 09:00:00 -0500</pubDate><description>Do not retain</description>
    </item><item><title>Undated</title><link>https://example.com/b</link></item>
    </channel></rss>"""
    payload = providers.parse_feed(data)
    assert payload.title == "Publisher"
    assert payload.items[0].title == "AI & news"
    assert payload.items[0].id == "a"
    assert payload.items[0].published_at == NOW - timedelta(hours=1)
    assert payload.items[1].published_at is None
    assert payload.items[1].id == "https://example.com/b"
    long = (
        b"<rss><channel><item><title>"
        + b"x" * 1000
        + b"</title><link>https://example.com</link></item></channel></rss>"
    )
    assert len(providers.parse_feed(long).items[0].title) <= 240
    with pytest.raises(ValueError):
        providers.parse_feed(b"<html>Access denied</html>")
    with pytest.raises(ValueError):
        providers.parse_feed(b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss><channel/></rss>')


def test_flights_age_from_epoch_milliseconds_minus_seen_position_not_fetch_time():
    data = {
        "now": NOW.timestamp() * 1000,
        "ac": [
            {
                "hex": "abc",
                "flight": "N123  ",
                "t": "C172",
                "alt_baro": 3000,
                "dst": 2.4,
                "dir": 270,
                "seen_pos": 10,
                "lat": 35.1,
                "lon": -92.4,
            },
            {"hex": "old", "seen_pos": 121, "lat": 35.1, "lon": -92.4},
            {"hex": "unknown"},
        ],
    }
    payload = providers.parse_flights(data)
    assert payload.observed_at == NOW
    plane = payload.aircraft[0]
    assert plane.observed_at == NOW - timedelta(seconds=10)
    assert plane.callsign == "N123"
    assert plane.altitude_ft == 3000
    assert plane.distance_nm == 2.4
    assert plane.bearing_degrees == 270
    assert len(payload.current_at(NOW)) == 1
    assert payload.current_at(NOW + timedelta(seconds=111)) == ()
    assert payload.aircraft[2].observed_at is None
    assert payload.aircraft[2].altitude_ft is None
    data["ac"][0]["seen_pos"] = -1
    with pytest.raises(ValueError):
        providers.parse_flights(data)


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.headers = {}


def test_http_identifies_client_times_out_and_bounds_bodies(monkeypatch):
    def open_response(request, timeout):
        assert timeout == 8
        assert "Paiper" in request.get_header("User-agent")
        return Response(b"x" * (2 * 1024 * 1024 + 1))

    monkeypatch.setattr(http, "urlopen", open_response)
    with pytest.raises(http.SourceError, match="2 MiB"):
        http._request("https://example.com")


@pytest.mark.parametrize(
    "error",
    [
        URLError("offline"),
        TimeoutError("timeout"),
        HTTPError("https://example.com", 503, "Unavailable", {}, None),
        HTTPError(
            "https://example.com", 429, "rate limited", {"Retry-After": "1800"}, None
        ),
    ],
)
def test_http_failures_are_explicit_and_retry_after_preserved(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(http, "urlopen", fail)
    with pytest.raises(http.SourceError) as caught:
        http._request("https://example.com")
    if isinstance(error, HTTPError) and error.code == 429:
        assert caught.value.retry_after == 1800


def responses(url):
    if "open-meteo" in url:
        return json.dumps(weather()).encode()
    if "weather.gov" in url:
        return b'{"features":[]}'
    if "statsapi" in url:
        return b'{"dates":[]}'
    if "yahoo" in url:
        return json.dumps(quote(urlparse(url).path.rsplit("/", 1)[-1])).encode()
    if "frankfurter" in url:
        return b'{"date":"2026-10-26","base":"USD","quote":"PEN","rate":3.44}'
    if "adsb.lol" in url:
        return json.dumps({"now": NOW.timestamp() * 1000, "ac": []}).encode()
    return b"<rss><channel><title>Publisher</title></channel></rss>"


def observation_response(key, at):
    if key in ("blkb", "vti", "voo"):
        data = quote(key.upper())
        data["chart"]["result"][0]["meta"]["regularMarketTime"] = at.timestamp()
    elif key == "usd_pen":
        data = {
            "date": at.date().isoformat(),
            "base": "USD",
            "quote": "PEN",
            "rate": 3.5,
        }
    elif key == "weather":
        data = weather()
        data["current"]["time"] = at.timestamp()
    elif key == "flights":
        data = {
            "now": at.timestamp() * 1000,
            "ac": [{"hex": "abc", "seen_pos": 10, "lat": 35.1, "lon": -92.4}],
        }
    elif key == "alerts_updated":
        data = {"updated": at.isoformat(), "features": []}
    elif key == "alerts":
        data = {
            "features": [
                {
                    "id": "nws:1",
                    "properties": {
                        "sent": at.isoformat(),
                        "effective": (at + timedelta(hours=1)).isoformat(),
                        "expires": (at + timedelta(hours=2)).isoformat(),
                        "severity": "Severe",
                        "event": "Wind Advisory",
                        "headline": "Wind advisory",
                    },
                }
            ]
        }
    else:
        return (
            "<rss><channel><item><title>Headline</title>"
            "<link>https://example.com/article</link>"
            f"<pubDate>{at.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>"
            "</item></channel></rss>"
        ).encode()
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    "kind",
    [
        "blkb",
        "vti",
        "voo",
        "usd_pen",
        "weather",
        "flights",
        "alerts",
        "alerts_updated",
        "world_news",
    ],
)
def test_future_observations_preserve_last_good_and_backoff(
    tmp_path, monkeypatch, kind
):
    from paiper.sources import _requests

    key = "alerts" if kind == "alerts_updated" else kind
    specs = _requests(NOW, 35.0887, -92.4421, "America/Chicago")
    url = next(url for name, url, *_ in specs if name == key)
    monkeypatch.setattr(http, "fetch", responses)
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        good = collect(cache, NOW)[key]
        history = cache.history(key)
        attempted_at = NOW + timedelta(days=1)
        bad_body = observation_response(kind, NOW.replace(year=2027))
        monkeypatch.setattr(
            http,
            "fetch",
            lambda target: bad_body if target == url else responses(target),
        )
        failed = collect(cache, attempted_at)[key]
        assert failed.status(attempted_at) == "degraded"
        assert "future" in failed.error.lower()
        assert failed.payload == good.payload
        assert failed.observed_at == good.observed_at
        assert failed.fetched_at == good.fetched_at
        assert failed.valid_until == good.valid_until
        assert failed.failures == 1
        assert failed.last_attempt_at == attempted_at
        assert failed.next_fetch_at > attempted_at
        assert cache.history(key) == history
        monkeypatch.setattr(http, "fetch", lambda url: pytest.fail("backoff"))
        assert collect(cache, attempted_at + timedelta(seconds=1))[key] == failed
    with ContentCache(path) as cache:
        assert collect(cache, attempted_at, offline=True)[key] == failed


@pytest.mark.parametrize(
    ("key", "seconds"),
    [
        ("blkb", 300),
        ("usd_pen", 86400),
        ("weather", 900),
        ("flights", 30),
        ("alerts", 300),
        ("alerts_updated", 300),
        ("world_news", 300),
    ],
)
def test_observation_clock_skew_is_bounded(key, seconds):
    from paiper.sources import _requests, _times

    source_key = "alerts" if key == "alerts_updated" else key
    spec = next(
        spec
        for spec in _requests(NOW, 35.0887, -92.4421, "America/Chicago")
        if spec[0] == source_key
    )
    _, _, cadence, parser = spec
    for offset in (0, seconds):
        payload = parser(observation_response(key, NOW + timedelta(seconds=offset)))
        _times(payload, NOW, cadence)
    beyond = seconds + (86400 if key == "usd_pen" else 1)
    payload = parser(observation_response(key, NOW + timedelta(seconds=beyond)))
    with pytest.raises(ValueError, match="future"):
        _times(payload, NOW, cadence)


def test_current_observations_and_future_forecasts_schedules_remain_cacheable(
    tmp_path, monkeypatch
):
    forecast = weather()
    forecast["daily"]["time"] = ["2026-10-28"]
    forecast["daily"]["sunrise"] = ["2026-10-28T07:27"]
    forecast["daily"]["sunset"] = ["2026-10-28T18:19"]
    forecast["hourly"]["time"] = ["2026-10-28T10:00", "2026-10-28T11:00"]
    scheduled = game()
    scheduled["status"]["startTimeTBD"] = False

    def fetch(url):
        if "open-meteo" in url:
            return json.dumps(forecast).encode()
        if "statsapi" in url:
            return json.dumps({"dates": [{"games": [scheduled, game(2)]}]}).encode()
        if "frankfurter" in url:
            return observation_response("usd_pen", NOW)
        if "weather.gov" in url:
            return observation_response("alerts", NOW)
        return responses(url)

    monkeypatch.setattr(http, "fetch", fetch)
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        records = collect(cache, NOW)
        assert all(record.status(NOW) == "ok" for record in records.values())
        assert all(not record.is_stale(NOW) for record in records.values())
        assert records["usd_pen"].observed_at == NOW.date()
        assert records["blkb"].observed_at == NOW
        assert records["weather"].payload.hourly[0].at > NOW
        assert records["weather"].payload.daily[0].date > NOW.date()
        assert records["cardinals"].payload.games[0].start_at > NOW
        assert records["cardinals"].payload.games[1].start_time_tbd
        alert = records["alerts"].payload.items[0]
        assert alert.effective_at > NOW
        assert alert.expires_at > NOW


def test_collect_independent_failures_cadence_offline_and_reopen(tmp_path, monkeypatch):
    def fetch(url):
        if "/BLKB?" in url:
            raise http.SourceError("HTTP 429", retry_after=1800)
        return responses(url)

    monkeypatch.setattr(http, "fetch", fetch)
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        records = collect(cache, NOW)
        assert set(records) == {
            "weather",
            "alerts",
            "cardinals",
            "blkb",
            "vti",
            "voo",
            "usd_pen",
            "world_news",
            "cardinals_news",
            "ai_news",
            "flights",
        }
        assert records["blkb"].error == "HTTP 429"
        assert records["voo"].payload.symbol == "VOO"
        assert records["alerts"].payload.items == ()
        assert records["usd_pen"].observed_at == date(2026, 10, 26)
        assert records["weather"].next_fetch_at == NOW + timedelta(minutes=30)
        assert records["flights"].next_fetch_at == NOW + timedelta(minutes=5)
        assert records["blkb"].next_fetch_at == NOW + timedelta(minutes=30)
        monkeypatch.setattr(http, "fetch", lambda url: pytest.fail("not due/offline"))
        assert collect(cache, NOW + timedelta(seconds=1)) == records
    with ContentCache(path) as cache:
        assert collect(cache, NOW + timedelta(days=2), offline=True) == records


def test_stale_quotes_refetch_without_changing_observation_or_losing_last_good(
    tmp_path, monkeypatch
):
    data = quote("VOO")
    data["chart"]["result"][0]["meta"]["regularMarketTime"] = (
        NOW.timestamp() - 4 * 86400
    )

    def fetch(url):
        return json.dumps(data).encode() if "/VOO?" in url else responses(url)

    monkeypatch.setattr(http, "fetch", fetch)
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        old = collect(cache, NOW)["voo"]
        updated = collect(cache, NOW + timedelta(minutes=5))["voo"]
        assert updated.fetched_at == NOW + timedelta(minutes=5)
        assert updated.observed_at == NOW - timedelta(days=4)
        assert updated.is_stale(NOW)

        def fail(url):
            raise http.SourceError("offline")

        monkeypatch.setattr(http, "fetch", fail)
        failed = collect(cache, NOW + timedelta(minutes=10))["voo"]
        assert failed.payload == old.payload
        assert failed.error == "offline"


def test_collect_builds_location_units_and_next_season_queries(tmp_path, monkeypatch):
    urls = []

    def fetch(url):
        urls.append(url)
        return responses(url)

    monkeypatch.setattr(http, "fetch", fetch)
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        collect(cache, NOW, latitude=1.25, longitude=-2.5, timezone="UTC")
    weather_url = next(url for url in urls if "open-meteo" in url)
    query = parse_qs(urlparse(weather_url).query)
    assert query["latitude"] == ["1.25"]
    assert query["longitude"] == ["-2.5"]
    assert query["temperature_unit"] == ["fahrenheit"]
    assert query["wind_speed_unit"] == ["mph"]
    assert query["timezone"] == ["UTC"]
    assert query["timeformat"] == ["unixtime"]
    schedule = parse_qs(urlparse(next(url for url in urls if "statsapi" in url)).query)
    assert schedule["teamId"] == ["138"]
    assert schedule["sportId"] == ["1"]
    assert "2027" in schedule["endDate"][0]
    assert date.fromisoformat(schedule["startDate"][0]) < date(2026, 10, 1)
    assert "linescore" in schedule["hydrate"][0]
    assert any("point=1.25%2C-2.5" in url for url in urls)
    assert any("/point/1.25/-2.5/25" in url for url in urls)


def test_offline_empty_cache_reports_unavailable_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "fetch", lambda url: pytest.fail("offline network"))
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        records = collect(cache, NOW, offline=True)
        assert len(records) == 11
        assert all(record.status(NOW) == "unavailable" for record in records.values())


def test_http_has_hard_process_deadline_even_when_dns_or_headers_hang(monkeypatch):
    real_run = subprocess.run

    def hung_resolver(command, **kwargs):
        return real_run([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)

    monkeypatch.setattr(http, "WORKER_TIMEOUT", 0.1, raising=False)
    monkeypatch.setattr(subprocess, "run", hung_resolver)
    monkeypatch.setattr(http, "urlopen", lambda *a, **kw: Response(b"hello"))
    start = time.monotonic()
    with pytest.raises(http.SourceError, match="deadline"):
        http.fetch("https://example.com")
    assert time.monotonic() - start < 3


def test_collect_uses_at_most_four_workers_and_only_caller_touches_sqlite(
    tmp_path, monkeypatch
):
    lock = threading.Lock()
    active = maximum = 0

    def fetch(url):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return responses(url)

    monkeypatch.setattr(http, "fetch", fetch)
    with ContentCache(tmp_path / "cache.sqlite3") as cache:
        records = collect(cache, NOW)
    assert all(record.payload is not None for record in records.values())
    assert 2 <= maximum <= 4


def test_typed_roundtrip_for_every_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "fetch", responses)
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        records = collect(cache, NOW)
    with ContentCache(path) as cache:
        assert collect(cache, NOW, offline=True) == records


@pytest.mark.parametrize(
    "parser",
    [
        providers.parse_weather,
        providers.parse_alerts,
        providers.parse_fx,
        providers.parse_flights,
        lambda data: providers.parse_quote(data, "BLKB"),
    ],
)
@pytest.mark.parametrize("data", [None, [], {"features": "not an array"}])
def test_bad_envelopes_are_explicit_source_errors(parser, data):
    with pytest.raises(ValueError):
        parser(data)


def test_http_rejects_bad_length_compression_and_handles_read_errors(monkeypatch):
    from http.client import IncompleteRead

    for headers in [
        {"Content-Length": str(2 * 1024 * 1024 + 1)},
        {"Content-Length": "NaN"},
        {"Content-Encoding": "gzip"},
    ]:
        response = Response(b"content")
        response.headers = headers
        monkeypatch.setattr(
            http, "urlopen", lambda *a, response=response, **kw: response
        )
        with pytest.raises(http.SourceError):
            http._request("https://example.com")

    class Broken(Response):
        def read1(self, size):
            raise IncompleteRead(b"partial")

    monkeypatch.setattr(http, "urlopen", lambda *a, **kw: Broken(b""))
    with pytest.raises(http.SourceError):
        http._request("https://example.com")


def test_null_previous_session_does_not_substitute_two_sessions_ago():
    data = quote()
    data["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [54, None, 53.85]
    result = providers.parse_quote(data, "BLKB")
    assert result.previous_close is None
    assert result.change is None


def test_missing_tbd_start_does_not_invent_midnight():
    data = game()
    data["status"].pop("startTimeTBD")
    data.pop("gameDate")
    result = providers.parse_schedule(
        {"dates": [{"games": [data]}]},
        date(2027, 3, 1),
        date(2027, 4, 1),
    ).games[0]
    assert result.start_at is None


def test_feed_does_not_truncate_long_ids_or_urls_into_different_identity():
    url = "https://example.com/" + "x" * 2000
    data = f"<rss><channel><item><title>Headline</title><link>{url}</link></item></channel></rss>"
    with pytest.raises(ValueError):
        providers.parse_feed(data.encode())


def test_location_change_never_exposes_old_location_as_current_and_respects_backoff(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(http, "fetch", responses)
    path = tmp_path / "cache.sqlite3"
    with ContentCache(path) as cache:
        original = collect(cache, NOW)
        offline = collect(cache, NOW, latitude=40, offline=True)
        for key in ("weather", "alerts", "flights"):
            assert offline[key].payload is None
            assert offline[key].status(NOW) == "unavailable"
        assert offline["voo"].payload == original["voo"].payload

        def failed(url):
            raise http.SourceError("offline")

        monkeypatch.setattr(http, "fetch", failed)
        other = collect(cache, NOW, latitude=40)
        assert other["weather"].payload is None
        assert other["weather"].error is not None
        monkeypatch.setattr(
            http, "fetch", lambda url: pytest.fail("location failure is backed off")
        )
        assert collect(cache, NOW + timedelta(seconds=1), latitude=40) == other
        assert (
            collect(cache, NOW, offline=True)["weather"].payload
            == original["weather"].payload
        )
    with ContentCache(path) as cache:
        assert collect(cache, NOW, latitude=40, offline=True)["weather"].payload is None


def test_process_worker_failure_protocol_uses_no_network():
    with pytest.raises(http.SourceError, match="HTTPS"):
        http.fetch("http://example.com")


def test_socket_response_deadline_prevents_slow_trickle(monkeypatch):
    clock = iter([0, 0, 9])
    monkeypatch.setattr(http.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(http, "urlopen", lambda *a, **kw: Response(b"x" * 100000))
    with pytest.raises(http.SourceError, match="deadline"):
        http._request("https://example.com")


def test_retry_after_http_date_is_bounded(monkeypatch):
    future = datetime.now(UTC) + timedelta(days=30)
    error = HTTPError(
        "https://example.com",
        429,
        "limited",
        {
            "Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT"),
        },
        None,
    )

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(http, "urlopen", fail)
    with pytest.raises(http.SourceError) as caught:
        http._request("https://example.com")
    assert caught.value.retry_after == 21600


def test_missing_publisher_name_stays_unknown():
    assert providers.parse_feed(b"<rss><channel/></rss>").title is None


def test_aircraft_ground_status_is_not_inferred_from_numeric_altitude():
    data = {
        "now": NOW.timestamp() * 1000,
        "ac": [
            {"hex": "numeric", "alt_geom": 123},
            {"hex": "grounded", "alt_baro": "ground"},
        ],
    }
    planes = providers.parse_flights(data).aircraft
    assert planes[0].on_ground is None
    assert planes[1].on_ground is True
    assert planes[1].altitude_ft is None
