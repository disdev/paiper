# The rotating daily paper

`stillness` and `calibration` retain their static APIs and offline labels.
`dashboard` is a separately collected, dated daily paper. No sources are called
from templates, Chromium, or the renderer. HTML/network requests are blocked.

```sh
# Deterministic manager smoke path: empty cache needs no network or configuration.
paiper preview --scene dashboard --offline --cache .artifacts/smoke/content.sqlite3 \
  --output .artifacts/dashboard.png

# Online collection, then render one page.
paiper preview --scene dashboard --page weather \
  --cache .artifacts/content.sqlite3 --output .artifacts/weather.png

# Only on an already verified/confirmed E6 installation, with its EXISTING state:
paiper display --scene dashboard --config /path/to/panel.yaml \
  --state /path/to/existing-display.sqlite3 --cache /path/to/content.sqlite3
```

Dashboard options on preview/display: `--page PAGE`, `--offline`, `--cache PATH`,
`--dashboard-config PATH`. Preview still accepts `--native` and optional
`--config`; display still requires the confirmed panel profile and shared state.
No hardware runs during preview. An unavailable explicit page produces a useful
error rather than made-up data; automatic selection uses bundled fallbacks.
An active severe alert outranks an explicit page request.

The cache defaults to `$XDG_STATE_HOME/paiper/content.sqlite3`, or
`~/.local/state/paiper/content.sqlite3` if XDG is missing/relative. It must never
be the hardware state database. Resolved-path and same-inode aliases are rejected;
the cache also validates its application identity before writing.

## Separate optional preferences

An explicit `--dashboard-config` must exist. Otherwise read `dashboard.yaml`
beside `--config`, or `$XDG_CONFIG_HOME/paiper/dashboard.yaml`
(`~/.config/paiper/dashboard.yaml` when XDG is absent/relative). Missing optional
files use defaults without writing any config. Panel YAML is never extended or
rewritten, so older releases can still read it.

```yaml
location: Conway
latitude: 35.0887
longitude: -92.4421
timezone: America/Chicago
photo_directory: photos
playlists:
  morning: [morning, verse, weather, cardinals]
  day: [markets, ai, news, usd-pen, gallery, flights]
  evening: [cardinals, weather, gallery, news, usd-pen, verse]
  night: [verse, gallery]
```

All fields are optional. Relative photo paths are relative to dashboard YAML,
not the process's working directory. Unknown keys/dayparts/pages, duplicate or
empty playlists, wrong types, invalid/NaN coordinates and unknown time zones
are rejected. Location is 1–24 characters. Each playlist has 1–16 unique pages,
excluding the special alert page; omitted dayparts retain their defaults.

Local time, including DST, chooses morning 06:00–09:00, day 09:00–17:00,
evening 17:00–22:00, night 22:00–06:00. These defaults express the approved
daypart design without inventing a market-session calendar or sky predictions.
Markets remain **dated quotes**, including on holidays/weekends; no open/closed,
live price or exchange-session status is claimed. No refresh cadence is changed.

## Pages

| Page | Content and limits |
| --- | --- |
| `morning` | One BBC headline, available Conway weather/rain/wind, today's returned MLB game, one complete short WEB verse. Dates distinguish publication/model/fetch. |
| `weather` | Current modeled temperature while valid; otherwise a valid future hourly forecast, high/low, rain/wind and an outdoor-window suggestion. |
| `cardinals` | Individual scores (missing stays `—`), game/start dates, MLB status, optional pitchers; upcoming schedules, previous results and club headlines rotate. An empty schedule states the returned window. |
| `markets` | Independent BLKB, VTI and VOO USD quotes, observation dates and available previous-close change. Missing tickers do not erase the others. |
| `usd-pen` | US $1/$20/$100 converted at the dated Frankfurter daily reference rate, not a cash/bank quote. |
| `news` | Source-attributed BBC World headlines with original publication dates, not fetch dates masquerading as publication. |
| `ai` | OpenAI publisher headlines, explicitly a publisher feed rather than comprehensive AI coverage. |
| `verse` | Seven verified complete public-domain WEB verses rotate by local calendar date. |
| `gallery` | Bounded local PNG/JPEG/WebP discovery plus NASA/Bill Anders Earthrise. |
| `flights` | Above the configured location: position snapshots younger than two minutes or an explicitly dated observation digest. Unknown values stay unknown; no routes or destinations inferred. |
| `alert` | Effective, unexpired NWS Severe/Extreme alerts, issue/expiry times and a supplementary-warning disclaimer. No “all clear.” |

Weather forecast eligibility uses the outer hourly forecast validity, **not**
the shorter current-model interval. Outside the forecast range it is unavailable.
The outdoor suggestion is the first two consecutive hourly samples within the
next 24 hours, entirely between supplied sunrise/sunset, each 50–85°F, rain
probability ≤20%, wind ≤15 mph. No qualifying or complete data means “No clear
outdoor window,” not a claim of unsafe conditions.

Source degradation/stale-cache timestamps remain visible; quote dates remain
old after refetch, and article dates remain their original publication dates.
All usable live content missing means honest sources-unavailable verse/art,
not a fabricated weather/score/price. Healthy online pages never say OFFLINE.
The header clock describes rendering, not a live clock or observation time.

## Rotation and persistence

Selection is a pure proposal. The cache's `playlist` state is committed only
after the existing `render_and_display(factory, profile, state_path)` returns
`True`. The factory collects, rechecks the current time, builds, selects and
renders under the existing hardware guard. Preview, false/skipped displays and
exceptions never commit rotation. Source collection may still update its own
independent last-good/health/history records.

Persisted state contains per-daypart cursors, at most 128 recent content IDs,
64 final-game IDs, 64 aircraft timestamps, the last-flight time, priority parity,
and the last successful page/id/source/freshness/completion time. Oversized
publisher IDs are hashed for bounded state. Normal slots rotate fairly through
eligible pages; unseen stories/photos precede the least recently shown when the
finite pool is exhausted. There is no backlog replay after downtime.

Recently fetched (≤10 minutes, no fetch error) MLB Live status earns every other
slot; intervening normal slots exclude Cardinals. A recently fetched final dated
today/yesterday gets one extra slot per game ID. Older cached Live never wins
priority. Final history is bounded but the eligible date window prevents old
seasons returning as “new” finals.

Flights have no interrupt priority. Every flight slot, including an explicit
request, respects one per 30 minutes and no repeat aircraft within an hour.
Persistent flight bounds start at successful display completion. Previews
consult but do not advance these bounds. Alerts expire again during selection.

Dashboard invocations use a bounded per-user set of 64 inode-keyed flock files
in the default state directory's `cache-locks/`, across collection, render/display
and successful commit, including symlink/hardlink aliases. All invocations must
use the same XDG state root. Unrelated caches mapping to the same slot safely
serialize too. The SQLite file itself is never flocked (macOS incompatibility).
No SQLite write transaction spans network/Chromium/hardware I/O. Existing
hardware profile, state locking, reservations, hash deduplication and cooldown
are untouched. If power fails after the hardware success but before the separate
playlist commit, safety state remains authoritative; rotation can repeat rather
than falsely recording an undisplayed frame.

## Rendering, media and limitations

Every frame is RGB 600×400 with 48/304/48 bands and exactly the native six-pigment
palette. Metadata is at least 18 px; body text is 24–30 px, focal values 40–48 px.
Long headlines receive explicit editorial ellipses based on actual loaded font
metrics; no clipping or smaller-font workaround. Layout overflows fail.
Only the photo rectangle is Floyd–Steinberg dithered; UI stays crisp.

Gallery scans at most 128 immediate directory entries and tries at most 32 local
files, no recursion or symlinks. Files are bounded to 12 MiB and 24 megapixels,
EXIF-transposed and proportionally contained without cropping. Bad files are
logged/skipped. Supply a curated directory rather than thousands of images;
which first 128 entries a huge directory yields is filesystem-dependent.

Packaged exact verse text and NASA media provenance/permission links are in
[`src/paiper/assets/PROVENANCE.md`](../src/paiper/assets/PROVENANCE.md).
Local image rights remain the owner's responsibility. There is no NASA
endorsement. Physical photo appearance still needs inspection on the real panel.

`deploy/app-capabilities.json` declares dashboard support for the next deployment
task. No manager/timer migration, local web controls, orbital prediction,
hardware access, or deployment is performed by this implementation.
