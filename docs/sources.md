# Personal-paper data sources

The content layer is independent of display rendering, GPIO, deployment and the
display safety database. It uses Python's standard library only. It does not
enable new dashboard scenes by itself.

## Collecting and retaining data

```python
from datetime import UTC, datetime
from pathlib import Path

from paiper.cache import ContentCache
from paiper.content import Alerts, Flights
from paiper.sources import collect

with ContentCache(Path("content.sqlite3")) as cache:
    records = collect(
        cache,
        datetime.now(UTC),
        latitude=35.0887,
        longitude=-92.4421,
        timezone="America/Chicago",
    )
    # Re-evaluate eligibility at presentation time, AFTER collection.
    display_now = datetime.now(UTC)
    alerts = records["alerts"]
    active_alerts = (
        alerts.payload.active_at(display_now)
        if isinstance(alerts.payload, Alerts) else ()
    )
    flights = records["flights"]
    current_aircraft = (
        flights.payload.current_at(display_now)
        if isinstance(flights.payload, Flights) else ()
    )
    cache.set_state("playlist", {"index": 3, "recent_ids": ["publisher:item-id"]})
```

Choose a persistent **different path** from the hardware safety database.
`ContentCache` must stay in its creating thread and be used as a context manager.
Do not hold a SQLite write transaction during collection; the API itself does
not do so. Callers should serialize collection for one cache: due checks are not
a cross-process scheduler lock.

`collect(cache, now, *, latitude=35.0887, longitude=-92.4421,
timezone="America/Chicago", offline=False)` returns a `dict[str, SourceRecord]`
containing all eleven keys below, even if none has succeeded. `now` must be an
aware `datetime`. Offline mode never makes network calls or updates failure
counts. Its results still have dates and must be checked at presentation time.
Changing location/time zone forces the location-dependent sources to refresh.
Their old payloads remain on disk, but are hidden from results for a different
requested location, including offline results. Failed requests at that new
location still respect backoff.

Each `SourceRecord` holds an immutable, typed `payload` (or `None`), `fetched_at`,
`observed_at`, `valid_until`, `last_attempt_at`, `next_fetch_at`, `failures`, and
`error`, plus `source_key`, `context` and `attempt_context`. The two context
strings are collector-owned location identities; consumers do not need to parse
them. All actual timestamp fields are aware UTC datetimes. **FX `observed_at` is
a `date`, not an invented midnight observation.** Schedule observation time is
unknown (`None`); missing RSS publication dates also remain `None`.
`fetched_at`/`last_attempt_at` use the caller's collection time, not a fabricated
provider observation.

`record.is_due(now)` controls the next attempt. `record.is_stale(now)` checks the
source-specific validity rule below. `record.status(now)` is:

- `unavailable`: no eligible cached payload for this request;
- `degraded`: there is a last-good payload but a subsequent attempt failed;
- `stale`: a payload exists, no subsequent failure, but validity has expired;
- `ok`: payload exists, no failure, and it passes its validity rule.

`ok` is **not** a claim of real-time markets, complete aircraft coverage, or
weather safety. Check `is_stale` separately for degraded records. On a source
failure, data and its original timestamps are retained; the error is visible
and limited to 300 characters. Missing or malformed data is never replaced with
sample data, zeros, inferred routes or invented market sessions.

## Providers, cadence and presentation rules

| Key | Provider / endpoint | Payload | Normal fetch interval |
| --- | --- | --- | --- |
| `weather` | [Open-Meteo](https://api.open-meteo.com/v1/forecast) | `Weather` | 30 minutes |
| `alerts` | [NWS active point alerts](https://api.weather.gov/alerts/active?point=35.0887,-92.4421) | `Alerts` | 5 minutes |
| `cardinals` | [MLB Stats API schedule](https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId=138) | `Schedule` | 5 minutes |
| `blkb` | [Yahoo BLKB daily chart](https://query1.finance.yahoo.com/v8/finance/chart/BLKB?interval=1d&range=5d) | `Quote` | 5 minutes |
| `vti` | [Yahoo VTI daily chart](https://query1.finance.yahoo.com/v8/finance/chart/VTI?interval=1d&range=5d) | `Quote` | 5 minutes |
| `voo` | [Yahoo VOO daily chart](https://query1.finance.yahoo.com/v8/finance/chart/VOO?interval=1d&range=5d) | `Quote` | 5 minutes |
| `usd_pen` | [Frankfurter USD/PEN](https://api.frankfurter.dev/v2/rate/USD/PEN) | `ExchangeRate` | 24 hours |
| `world_news` | [BBC World RSS](https://feeds.bbci.co.uk/news/world/rss.xml) | `Feed` | 30 minutes |
| `cardinals_news` | [Cardinals RSS](https://www.mlb.com/cardinals/feeds/news/rss.xml) | `Feed` | 30 minutes |
| `ai_news` | [OpenAI News RSS](https://openai.com/news/rss.xml) | `Feed` | 30 minutes |
| `flights` | [adsb.lol 25-nautical-mile point query](https://api.adsb.lol/v2/point/35.0887/-92.4421/25) | `Flights` | 5 minutes |

### Weather

The request uses Fahrenheit, mph, hourly precipitation **probability** (percent),
and two days of forecasts. Times are requested as Unix timestamps to avoid
ambiguous repeated local hours around daylight-saving transitions. The response
time zone converts daily boundaries to local dates. Grid coordinates returned by
the provider may differ slightly from the requested point.

Current conditions are modeled values, not a local weather-station measurement.
`WeatherCurrent.valid_until` is the provider's timestamp plus supplied interval.
The source record uses that current-conditions validity; `Weather.valid_from`
and `Weather.valid_until` separately bound the supplied hourly forecast coverage
through the end of its last hour. A current observation can therefore be stale
while tomorrow's forecast is still eligible. Sunrise and sunset may be missing
at polar locations. Do not substitute zero for missing values.

Attribute **Open-Meteo, CC BY 4.0** visibly. The free service permits personal,
noncommercial use, subject to its limits; see
[Open-Meteo terms](https://open-meteo.com/en/terms). No availability guarantee.

### Official alerts

`Alerts.active_at(now)` returns only alerts effective by `now` and not expired.
Use this method, **not raw `items`**, when displaying active alerts from cache.
Empty NWS `features` is a healthy response, but is never an “all clear.” This is
supplementary information, not an emergency-warning system; stale/unavailable
alerts require an explicit label. Source polling validity is five minutes,
independent of each alert's issue/effective/expiry times.

Attribute **National Weather Service** and link to the official source.
[NWS API documentation](https://www.weather.gov/documentation/services-web-api)
describes the public service and identification/rate-limit requirements.

### Cardinals schedule

Team 138, sport 1, with linescore and probable-pitcher hydration. Games retain
their IDs, official dates, detailed status, home/away teams, supplied scores,
inning, doubleheader marker and game number. A TBD start is `None`, never the
provider's placeholder midnight. No score is inferred from status. Probable
pitchers and inning data may be absent.

October–February queries cover September 1 of the preceding baseball season
through June 1 of the next season. Other months cover the previous 30 days and
next 45 days. Empty results mean only that the API returned no games in that
window, not that no season or game exists. Future results can include spring
training; do not label every upcoming game “regular season.” Polling validity is
five minutes; scheduled start time is not an observation timestamp.

Attribute **MLB / St. Louis Cardinals**. The publicly accessible Stats API is not
a promise of a stable, licensed redistribution API. Respect
[MLB terms](https://www.mlb.com/official-information/terms-of-use); personal
display use does not imply rights to republish MLB content or logos.

### Market quotes

Fetch and validate BLKB, VTI and VOO separately, including returned symbol,
USD currency, finite positive price, and `regularMarketTime`. Display the actual
quote time/date and source, never “real-time.” Quotes older than 24 hours are
marked stale; this is a conservative display-age rule, **not a trading-session
calendar**. Weekends, holidays, outages, closed markets and delays cannot be
distinguished reliably by weekday arithmetic.

`Quote.previous_close` comes from the latest supplied prior-session daily bar,
or an explicit `previousClose`/`regularMarketPreviousClose` field. If that bar
has a missing close, an earlier session is not substituted. Explicit fields
may have no known `previous_close_date`. Multi-day `chartPreviousClose` is not
used. `change` and `change_percent` are `None` when the comparison is unknown.
Old observations are refetched at the normal five-minute cadence, not silently
treated as new because an HTTP request succeeded.

Attribute **Yahoo Finance**. This public chart endpoint is unofficial and may
change, deny requests, rate-limit or return delayed data. Respect
[Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) and
upstream exchange restrictions; no guaranteed service or redistribution rights
are asserted. This display is not trading advice or an execution-price feed.

### USD/PEN

`ExchangeRate.date` is the provider's actual publication/reference date; this
is a daily **reference rate**, not a bank quote, cash exchange rate or promised
transaction price. The age boundary is the next UTC date, without inferring
business days or fabricating an observation time. Daily polling may continue
to return an older reference date; keep that date visible.

Attribute **Frankfurter**. Its [documentation](https://frankfurter.dev/)
describes free access without API keys and recommends caching; underlying
institutional publication schedules apply.

### Publisher headlines

Only headlines, IDs, links and supplied publication times are retained—never
full articles or descriptions. Headlines are stripped of markup and limited
to 240 characters; up to 40 distinct items in publisher order are retained.
IDs/links are preserved rather than truncated (oversized identities are an
explicit failure). Missing publisher names/publication dates remain unknown. RSS declarations
and custom entities are rejected. The three configured feeds use RSS 2.0; this
is deliberately not a general-purpose RSS/Atom aggregation framework.

Attribute **BBC News**, **MLB / Cardinals**, and **OpenAI**, respectively, and
retain their original article links. The 30-minute source validity describes
polling recency, not the freshness of every article. These are publisher
feeds, not freely licensed full articles. Respect the publishers' terms:
[BBC](https://www.bbc.com/usingthebbc/terms/),
[MLB](https://www.mlb.com/official-information/terms-of-use),
[OpenAI](https://openai.com/policies/terms-of-use/).
No second AI feed is configured; OpenAI is the verified primary AI publisher.

### Nearby aircraft

Attribute **adsb.lol, ODbL 1.0**. Public access and license are documented in
[adsb.lol API documentation](https://www.adsb.lol/docs/open-data/api/).
Observe attribution and database-license obligations if redistributing data.

The response time is epoch **milliseconds**. An aircraft's position observation
time is that timestamp minus its `seen_pos` seconds, never the fetch time or
`seen` message age. `Flights.current_at(now)` includes only known positions
between zero and 120 seconds old. Use this method at presentation time, **not
raw `aircraft`**; retained positions older than two minutes cannot be current.
Collection takes time, so recalculate `now` after it completes.

Callsign, registration, aircraft type, altitude (feet), distance (nautical miles)
and bearing from the requested point (degrees) appear only if supplied. Ground
status is `True` only for the explicit `"ground"` token, otherwise unknown; it
does not become a zero-foot altitude. No destination, route or scheduled
flight is inferred. Coverage is incomplete; empty results are not proof of
empty airspace. This is not suitable for navigation or aircraft separation.
At five-minute polling, cached flights will necessarily become stale between
refreshes; no attempt is made to disguise that mismatch.

## Request and persistence bounds

- Four fixed collector workers, at most four HTTP subprocesses concurrently.
  Each request has an identifying Paiper User-Agent and an 8-second socket
  timeout/read deadline. A 12-second parent-enforced subprocess timeout also
  bounds DNS/TLS/header hangs. Eleven due sources take at most roughly three
  such batches, plus parsing/SQLite/OS scheduling overhead.
- Bodies are limited to 2 MiB; no compression expansion, cookies, authentication
  tricks, anti-bot bypasses or in-slot retry loops. HTTPS requests use normal
  certificate validation. HTTP and schema failures are visible per source.
- First failure backs off by the source cadence, doubling on successive
  failures. A valid `Retry-After` (seconds or HTTP date) can extend that delay.
  All retry delays cap at six hours. A successful request resets the failure
  count. Last-good payloads survive errors and process restarts.
- Cadence is measured from the caller's previous attempt time. Serial calls
  never refetch a source within its interval, even if returned observations
  are already stale. Failed FX attempts use the same six-hour retry cap rather
  than the successful daily cadence.
- Cache identity is SQLite `application_id = 0x50414943` (`PAIC`), schema version
  1. Existing databases are inspected read-only before writable access.
  Wrong identities, versions, schemas or damaged payloads raise `ValueError`;
  there is **no automatic reset, replacement or migration**.
- JSON records carry explicit whitelisted dataclass type names, exact field
  sets, tagged UTC timestamps and tagged dates. Typed tuples round-trip;
  nonfinite numbers and wrong types are rejected. No pickle.
- Last-good and its history insertion/pruning commit atomically. History keeps
  32 successful snapshots per source (newest first). Failures update current
  health only. At most 64 source keys, 128 selection keys, 64 KiB per selection
  JSON value and 2 MiB per record are permitted. Keys are 1–100 characters.
- `get_state(key, default=None)` / `set_state(key, value)` persist JSON-compatible
  playlist/recent-item state; `history(source_key)` returns immutable typed
  records. Store bounded recent-ID lists rather than continually inventing keys.
  The layer does not choose photos, articles, scenes, or playlist order.

Focused deterministic tests:

```sh
.venv/bin/python -m pytest -q tests/test_sources.py tests/test_cache.py
.venv/bin/ruff check src/paiper/content.py src/paiper/cache.py src/paiper/sources \
  tests/test_sources.py tests/test_cache.py
```
