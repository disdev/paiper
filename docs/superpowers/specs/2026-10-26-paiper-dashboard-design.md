# Paiper: A Personal Daily Paper

Revision: 2026-10-26. Product design, approved by the user.
Implementation starts with the display foundation; later increments remain separate.

## Product direction

Make a small, beautifully typeset daily paper for one person in Conway, Arkansas:
useful at a glance, occasionally surprising, and never a wall of tiny widgets.
Borrow TRMNL's excellent playlist and preview experience. Differentiate through
personal editorial choices, color-aware scenes, local operation, and honest data.
Do not try to outbuild its plugin marketplace.

### Confirmed requirements

- Raspberry Pi 5, 16 GB RAM; Waveshare EPD_4in0e, physically rotated 90 degrees CCW.
- User reports a 400x600 seven-color panel; verify the actual model before deployment.
- Hybrid layout: compact information strip plus a large rotating hero.
- Python on the Pi, HTML/CSS rendering with Playwright and Pillow postprocessing.
- Five-minute display cadence. No paid services. Public scraping is acceptable.
- Conway, AR 72034; approximate city center 35.0887, -92.4421.
- St. Louis Cardinals, general and AI news, BLKB/VTI/VOO, Bible verses, photos, flights.
- Currency is **USD to Peruvian soles (USD/PEN)**, confirmed with the user.
  The original Solana integration was an incorrect assumption and is removed.

## TRMNL comparison and build-versus-reuse

Official pages reviewed on 2026-10-26:

| Area | TRMNL today | Original Paiper | Revised Paiper proposal |
| --- | --- | --- | --- |
| Content | Large native/community plugin catalog: calendars, finance, RSS, photos, smart home, and more | Eight hard-coded categories | Focused personal scenes; reusable source/card interfaces, not a marketplace |
| Scheduling | Playlists, time-of-day groups, multiple mashup layouts | Round-robin with unlimited game/flight overrides | Daypart editions, freshness checks, repeat suppression, bounded interruptions |
| Design | E-paper framework, typography, overflow handling, charts, dithering | Generic strip and hero | Native 600x400 compositions with a shared type scale and restrained color |
| Configuration | Visual management and custom-plugin previews | YAML and CLI only | YAML first; small local preview/control page in a later increment |
| Hosting | Hosted service plus open-source BYOS options | Entirely local | Entirely local; cache and render keep working without a vendor account |
| Hardware | Own devices plus BYOD, including Raspberry Pi paths and some color devices | Assumed panel palette and orientation | Explicit panel profile and physical calibration |
| Cost | Hosted BYOD licensing; TRMNL+ advertises five-minute refresh; BYOS avoids hosted-service fees | Free sources, but availability overstated | No required paid license/API; source limits and maintenance acknowledged |

TRMNL is not cloud-only, monochrome-only, or inherently subscription-only.
Its custom plugins could reproduce many of these ideas. Paiper's advantage is
fit for this particular person and panel, not capabilities TRMNL cannot have.
TRMNL retains the advantage in ready-made integrations, setup polish, and support.

Three options:

1. **Recommended: focused Python application inspired by TRMNL.**
   Keeps the approved stack and controls color, scheduling, and hardware directly.
   Trade-off: we maintain the integrations.
2. **TRMNL BYOS plus a panel client.**
   Reuse a maintained server such as Terminus where suitable. Verify its specific
   plugin support and this panel's client compatibility first; hosted plugin
   availability does not guarantee equivalent self-hosted behavior.
   Trade-off: another stack and layout adaptation to the small color panel.
3. **TRMNL hosted BYOD.**
   Least custom platform work, but hosted licensing/upgrades conflict with the
   strict free-only goal. Do not select this by default.

Any reused framework or recipe must have a compatible license and attribution.
Liquid recipes are not drop-in Jinja templates; do not promise automatic import.

## Experience: editions, not just widgets

The following scenes are a menu for phased implementation, not a demand to ship
every integration at once. Headlines and examples describe formats, not live facts.

| Scene | What makes it personal and useful |
| --- | --- |
| Morning Paper | One important headline, Conway's weather and rain window, today's Cardinals game, and one short verse. No scrolling news wall. |
| Cardinals Clubhouse | A vintage scorecard: score, inning, opponent, next game, and a compact diamond. Pregame shows probable pitchers when supplied; off-season shows sourced news and the next scheduled game. |
| Market Postcard | BLKB, VTI, VOO with change versus previous close, compact history when available, and explicit session/as-of labels. No invented reasons for market movement. |
| Soles in Your Pocket | "US$1 = S/..." plus conversions for $20 and $100, dated daily reference rate, and a short history. Reference rates are not bank/cash exchange quotes. |
| AI Field Notes | One primary-source release, one useful tool or paper, and one broader AI headline. Deduplicate repeated coverage and link back to originals. |
| Above Conway | A sparse sky map and a few recently observed aircraft: callsign, type if available, altitude, direction, and observation age. Show a recent flyover digest rather than pretend five-minute e-paper is live radar. |
| The Good Window | Pick a favorable one-hour daylight window in the next 12 hours using hourly rain probability, temperature, and wind. Show the inputs and label it a forecast-based suggestion. |
| Stillness | A daily verse with reference and translation, large type, and a deliberate quiet layout. Optional user-written prayer/reflection; never invent scripture. |
| Gallery | Family photos from a local folder, licensed astronomy images, public-domain paintings, or Arkansas landscapes with appropriate rights and credit. Choose crops for the actual screen. |
| Tonight Outside | Sunset, moon phase, forecast cloud cover; later, visible ISS-pass predictions using orbital data and observer geometry. |
| Cabinet of Curiosities | A sourced "on this day", an Arkansas nature fact, a historical map, or a procedural print. Conway's Game of Life becomes one still generative artwork per day, not an animation. |

Additional opt-in directions after the core works: private ICS calendar,
personal countdowns, Home Assistant sensors, GitHub activity, and a local
"send this photo/note to the frame" action. No account linking is presumed.

### Suggested dayparts

Use `America/Chicago`, including daylight-saving changes.

- 06:00-09:00: Morning Paper, Stillness, weather, and today's game.
- 09:00-17:00: markets when open, AI/general news, gallery, and occasional flights.
- 17:00-22:00: Cardinals, evening weather/sky, gallery, and curiosity.
- 22:00-06:00: quieter verse/art selection with the same five-minute scheduler.

These are editable defaults. Market status uses an exchange calendar, not a
weekday-only check. A future overnight refresh pause requires user opt-in;
the requested five-minute cadence remains the default.

## Display and visual language

**Hardware correction:** Waveshare's current 4-inch E product is Spectra 6,
600x400, with six native colors and an approximately 19-second full refresh.
It is not a seven-native-color panel. The official E6 demo's `epd4in0e.py`
uses a native 400x600 buffer despite the product's landscape dimensions.
Its six wire color codes are 0, 1, 2, 3, 5, and 6; code 4 is reserved.
The supplied model description may refer to a different revision. Check the
panel label and matching vendor demo before enabling hardware output.

- Design at 600x400 landscape. Set native buffer dimensions from the verified
  driver; do not blindly rotate every image into 400x600.
- Use a labeled four-corner calibration image to resolve physical CCW mounting
  versus the driver's own orientation handling. Apply the transform once.
- E6 palette: black, white, red, yellow, blue, green. Orange is not a seventh
  native pigment; simulate it only in imagery if useful.
- Expect full-screen refreshes. The strip cannot update independently unless
  the exact hardware explicitly supports it. No partial-refresh assumptions.
- Standard layout: 48px masthead, 304px hero, 48px context rail.
  The masthead shows date, short weather, and "Updated HH:MM", not a live clock.
  The rail holds at most three short values; the market page carries all
  tickers and FX rather than squeezing everything into every screen.
- Start with 24-30px body text, 40-64px focal numbers, and 18-20px metadata.
  Check legibility at normal desk distance on the physical four-inch panel.
  Reduce content before reducing font size.
- Black text on white for essentials. Red for Cardinals and warnings; blue for
  sky/weather; green for positive changes. Use signs/labels as well as color.
  Use locally bundled icons, not unpredictable emoji fonts.
- Dither photo/art regions only, then composite crisp palette-mapped text and
  shapes. Preserve attribution. Do not dither the whole UI indiscriminately.
- Optional gallery/verse layout expands the hero while retaining a minimal
  information strip. This is a user-selectable variation, not a forced switch.

## Selection and timing

Separate data collection from display cadence. Faster collection never implies
faster physical refresh, and must stay within each provider's terms.

Each five-minute slot:

1. Assemble a consistent snapshot from current and permitted cached data.
2. Apply currently relevant local weather notices. An unexpired severe NWS
   alert covering the configured location can take the next slot, with source,
   issue time, and expiry. This is supplementary information, not a safety
   alarm: it may be delayed or unavailable and must never replace phone/radio alerts.
3. Respect an explicit pin from the future local controller, except for the
   above weather notice. Pins have a visible expiration.
4. During a live Cardinals game, give the scorecard every other slot. Between
   those, return to the normal edition. A final result gets one extra slot.
   Score/count data is always timestamped, never described as pitch-by-pitch.
5. Otherwise choose the next eligible scene from the daypart playlist, skipping
   unavailable content and suppressing repeated stories/art.

Flights never take over continuously. A notable-flight scene can occupy at most
one slot per 30 minutes, with no repeat of the same aircraft within an hour.
Positions older than two minutes are not drawn as current positions; a dated
recent-observation digest remains valid. Do not infer a route from position.

When normal scenes have no usable data, render a bundled verse/art fallback
with an explicit offline/unavailable label. If rendering or hardware fails,
retain the last successful frame and record the error.

Persist playlist position, recent content IDs, and last successful display
metadata across restarts. Render/display at most one frame at a time; never
replay a backlog of missed refreshes.

## Sources: free, with honest limits

Prefer documented APIs, then RSS, then permitted public HTML. Honor terms,
robots policies, caching headers, rate limits, and attribution. Never bypass
paywalls, login restrictions, or anti-bot controls. A parser breaking should
degrade one scene, not the whole dashboard.

Only the USD/PEN endpoint below was live-probed during this design revision.
Other choices are candidates requiring endpoint, terms, schema, and coverage
checks before their implementation is accepted.

| Need | Candidate and constraints |
| --- | --- |
| Cardinals | MLB Stats API, team 138. Handle postponed, suspended, canceled, doubleheader, final, and no-game days. Validate public usage terms. |
| BLKB/VTI/VOO | Evaluate Yahoo chart data for free personal use; unofficial access can break. Stooq is a candidate daily-price fallback, not an equivalent intraday feed. Label delayed/daily data explicitly. |
| USD/PEN | Frankfurter v2 `/rate/USD/PEN` returned a dated rate without a key. Cache daily; label it a reference rate. Keep provider and date. |
| Weather | NWS point/grid forecast and location-relevant alerts; supply an identifying User-Agent. Open-Meteo may back up forecast data, but not silently replace official alerts. |
| General/Cardinals/AI news | Validate working publisher RSS feeds; favor original MLB, research lab, and project sources plus reputable reporting. Show attributed headlines/short permitted excerpts, not scraped full articles. |
| Bible | Bundle a lawfully distributable translation such as World English Bible with its provenance. Works offline and avoids a network request per verse. No unlicensed ESV scraping. |
| Photos/art | Local photos first; Wikimedia/open-access museums with per-image rights checks. NASA APOD is optional: API keys/rate limits, non-image days, and third-party image copyrights require handling. |
| Flights | Evaluate adsb.lol's documented API, usage terms, and Conway coverage. Route/type enrichment is optional; unavailable fields stay absent. OpenSky is not assumed to be an unlimited anonymous fallback. |
| Sky | Compute sun/moon locally. ISS needs current orbital elements and a real pass predictor such as Skyfield, including observer darkness and satellite illumination. Do not depend on the old Open Notify pass endpoint. |
| Curiosities | Curated attributed facts and licensed/public-domain content first; Wikimedia sources where appropriate. No invented historical facts. |

Suggested fetch/cache policy: news every 30 minutes; FX daily; verse/art daily;
forecast every 30 minutes; official alerts each display slot; game and available
intraday quotes every five minutes while relevant. Flight collection may run
every 60 seconds only after provider permission/rate checks, otherwise once per
slot with honest snapshot labeling.

Data records contain source, observation/publication time, fetched time,
freshness status, attribution, and typed payload. Fetching an old observation
does not make it fresh. Source fallback must preserve meaning and units.
Stale values show a readable age/date, not only a mysterious dot.
Expire alert cards using their actual expiry; never show stale aircraft as live.
Old weather must say unavailable once outside its forecast validity.
Financial closing/daily prices may remain visible with their original date.

## Architecture

Keep one Python application, not microservices:

```text
Source adapters -> validated records -> SQLite cache/history
                                            |
                            edition and scene selection
                                            |
                           Jinja + local HTML/CSS/assets
                                            |
                            Playwright -> Pillow palette
                                            |
                         preview image / verified EPD driver
```

- Sources own fetching and normalization, not layouts or display hardware.
- SQLite stores atomic last-good records, bounded histories, deduplication
  state, and last successful display status. Small local photo files remain on disk.
- Scenes consume records and expose availability, freshness requirements, and
  template context. Start with explicit registration, not a plugin installer.
- The scheduler owns dayparts, fairness, expiry, and the display slot.
- Rendering has no external network dependency: fonts/assets are local and
  remote text is escaped. Provider HTML/scripts are never executed as templates.
- The display adapter owns initialization, encoding, BUSY timeout, and sleep/
  power handling. A failed write must not be recorded as successful.
- YAML holds user preferences; systemd supervises the process and journald logs
  source/display failures. Bounded retries with backoff do not stall other sources.
- Hash the final frame to avoid identical writes. Visible update timestamps
  will usually change on a new slot, so do not promise large refresh savings.
- Remove the arbitrary nightly clear. Follow the verified panel's maintenance
  guidance; ordinary full refreshes already clear/rewrite the frame.

### A small local control page, later

This is the most worthwhile usability addition borrowed from TRMNL:
view the exact quantized preview, next scheduled scene, source health, and
last successful hardware update; reorder/disable scenes; pin a scene until a
chosen time; select a local photo.

Do not build a drag-and-drop layout editor, cloud account system, or marketplace.
Default access is loopback/SSH tunnel. Phone-on-LAN access is opt-in and must
have authenticated, CSRF-protected mutations; do not expose controls publicly.
Uploaded images must be size-limited and decoded as images, not executable files.
Manual actions queue the next legal display slot, not extra rapid refreshes.

## Scope and delivery gates

1. **Display foundation:** verify hardware model and orientation, render a
   calibration card and one hybrid scene, exercise sleep/restart, and produce
   the same palette-correct preview on Mac without GPIO.
2. **Useful daily paper:** weather, Cardinals, markets where a free provider is
   viable, USD/PEN, RSS, offline verse, and local gallery; dayparts, persistence,
   freshness labels, deterministic fallback, and systemd operation.
3. **Delight and control:** recent-flight digest, forecast-based outdoor window,
   sky/curiosity scenes, and the small local control page. Each is a separate
   increment after the daily paper is reliable.
4. **Optional extensions:** personal calendar, home sensors, and other accounts
   only on request. A small quantized local LLM could later shorten sourced
   articles, but benchmark first; no LLM dependency for facts or core operation.

This is a product design spanning several increments. The next implementation
plan should cover the display foundation first, not all integrations at once.

## Acceptance criteria

- Exact 600x400 logical render, correct verified buffer dimensions and mounting
  transform; every encoded pixel belongs to the actual hardware palette.
- No clipped text on representative long headlines, missing fields, and large
  values. Verify actual-scale readability and a photo's physical appearance.
- Display writes are serialized and at least five minutes apart, including
  restarts/manual commands; long refreshes and missed ticks do not create bursts.
- Unit tests cover daypart/DST transitions, market holidays, priority fairness,
  stale/expired records, news deduplication, and restart-persistent rotation.
- Parser fixtures cover success, missing values, unexpected schema, HTTP errors,
  rate limiting, and source switches without falsely upgrading data freshness.
- Offline startup can render bundled content. Disconnecting the network does
  not remove the last good image or disguise old scores/quotes as current.
- Render fixtures for every implemented scene, plus an end-to-end preview using
  the real Chromium/Pillow path. Hardware smoke testing is a separate gate.
- Pi smoke test verifies the chosen driver on Pi 5, BUSY timeout, sleep/re-init,
  correct orientation, and a multi-hour five-minute refresh run.

## Remaining deployment decisions

- Actual panel label/revision and orientation calibration require the physical Pi.
- Default to World English Bible until a preferred licensed translation is chosen.
- Account-dependent sources remain disabled unless explicitly configured.
- The revised experience and phased scope were approved with "Do it."

## Research references

- TRMNL overview, playlists and layouts: https://trmnl.com/
- Plugin examples: https://trmnl.com/integrations
- Framework, private plugins, recipes and BYOS: https://trmnl.com/developers
- Ecosystem, self-hosting choices and hosted upgrade terms: https://trmnl.com/ecosystem
- BYOD devices and setup paths: https://trmnl.com/byod
- Waveshare E6 product and refresh specifications: https://www.waveshare.com/4inch-e-paper-hat-plus-e.htm
- Panel usage precautions: https://www.waveshare.com/wiki/4inch_e-Paper_HAT%2B_(E)_Manual
- FX documentation: https://frankfurter.dev/
- USD/PEN endpoint: https://api.frankfurter.dev/v2/rate/USD/PEN
- Flight API documentation entry point: https://api.adsb.lol/docs
