# Paiper daily paper

One-shot rendering for the **Waveshare 4-inch six-color E6 panel only**:
600×400 logical landscape, rotated once into the driver's 400×600 native buffer.
Black, white, yellow, red, blue, and green are the only output colors.
This is not compatible with similarly named monochrome, four-color, or
seven-color panels.

The `stillness` and `calibration` scenes explicitly say **OFFLINE**. Their
America/Chicago timestamp is the image-generation time, not a live clock.
The new `dashboard` scene adds live, independently cached sources and rotating
weather, Cardinals, markets, news, USD/PEN, verified WEB verses and photographs.
See [dashboard preferences and page behavior](docs/dashboard.md).
There is no local control UI or orbital-pass prediction.
Optional user timers provide CI-approved pull updates and explicitly opted-in
offline display attempts; the application itself remains one-shot.
This change does not switch existing deployment timers to dashboard; deployment
manager integration is a separate task. Preview it locally first.
The physical Raspberry Pi, panel orientation, and
multi-hour operation are **unverified**; local preview success is not deployment.

## GitHub-managed Raspberry Pi deployment

Source: [disdev/paiper](https://github.com/disdev/paiper).
On a **Raspberry Pi 5 running 64-bit Raspberry Pi OS Bookworm or Trixie
(Debian 12/13)**, run this one command as your ordinary display user:

```sh
(umask 077; setup_dir="$HOME/.paiper-setup-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex)')" &&
 mkdir -- "$setup_dir" &&
 curl --fail --location --proto '=https' --tlsv1.2 \
   https://raw.githubusercontent.com/disdev/paiper/deploy/deploy/bootstrap.sh \
   --output "$setup_dir/bootstrap.sh" &&
 sh "$setup_dir/bootstrap.sh")
```

The command becomes available once this guided-setup change is promoted to
`deploy`; it does not publish itself. Download failures never execute a partial
script. No git clone, venv activation, YAML editing, or `sudo pip` is needed.
The guided installer asks before OS/SPI/group changes, pinned vendor
installation, physical calibration, timer scheduling, and boot-without-login.
It may ask you to reconnect after a reboot/new login and rerun; it never reboots
or resets display history. Existing manual users must identify their **original
state database**, not create a replacement. Pressing Enter never approves GPIO.
Missing saved history stops setup rather than creating a new database. Explicit
first-use approval allows one initialization only; an interrupted initialization
requires operator review, not deleting state or setup records to retry.

Follow the [deployment guide](docs/deployment.md) for the prompts, advanced
manual setup, stable paths, and rollback. GitHub's `ci` job tests changes
before a successful main push can fast-forward `deploy`; the Pi polls only that
branch. Publishing the repository does **not** install software on your device.
The updater supports public HTTPS GitHub repositories, not private authentication.

## Local installation (Python 3.11)

From this directory, with Python 3.11 available:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --constraint requirements.lock -e '.[dev]' 'setuptools>=75' wheel
python -m playwright install chromium
cp config.example.yaml config.yaml
```

On supported Debian/Ubuntu Linux, Chromium also needs OS libraries:
`python -m playwright install-deps chromium` (the installer may request sudo).
Keep browser installation and execution under the same user. Do not run
`sudo pip`; Python dependencies belong in the venv. The Pi-specific venv setup
below is needed when using OS-provided GPIO packages.

```sh
paiper preview --scene stillness --output .artifacts/stillness.png
paiper preview --scene dashboard --offline --cache .artifacts/content.sqlite3 \
  --output .artifacts/dashboard.png
paiper preview --scene calibration --output .artifacts/calibration.png
paiper preview --scene calibration --config config.yaml --native \
  --output .artifacts/calibration-native.png
python -m paiper --help
python -m pytest -q
ruff check .
```

Preview does not acquire display state/locks, import the vendor driver, or
access GPIO. It works with
`confirmed: false`; `--native` uses the profile's 90° or 270° counterclockwise
rotation. PNG saves are atomic and validate dimensions, RGB mode, and every
pixel against the six-color palette. Fonts, CSS, and templates are packaged;
scene rendering blocks external network requests. Local font attribution and
licenses are in `src/paiper/assets/`.
Dashboard collection happens before rendering; omit `--offline` to fetch live
sources. Offline mode permits cached and packaged content, never providers.

## Raspberry Pi installation — physical verification still required

Use the official [E6 manual](https://www.waveshare.com/wiki/4inch_e-Paper_HAT%2B_(E)_Manual)
and check the **physical label**, HAT, power requirements, cable orientation,
and pin wiring with power disconnected. Do not infer compatibility from size.

### SPI and Python/GPIO dependencies

1. Run `sudo raspi-config`, enable **Interface Options → SPI**, and reboot.
   Check `dtparam=spi=on` in `/boot/firmware/config.txt` (older systems use
   `/boot/config.txt`). Verify `/dev/spidev0.0` exists; existence alone does not
   prove the bus is unused. Ensure no other program or overlay owns the panel's
   SPI bus or GPIO pins.
2. On Raspberry Pi OS **Bookworm with the distro Python 3.11**, install:

   ```sh
   sudo apt update
   sudo apt install python3.11-venv python3-pip python3-spidev \
     python3-gpiozero python3-lgpio unzip curl
   /usr/bin/python3.11 -m venv --system-site-packages .venv-pi
   . .venv-pi/bin/activate
   python -m pip install --constraint requirements.lock -e .
   python -m playwright install-deps chromium
   python -m playwright install chromium
   python -c 'import sys, spidev, gpiozero, lgpio; print(sys.version); print(spidev.__file__, gpiozero.__file__, lgpio.__file__, sep="\n")'
   ```

   The official Pi backend imports `spidev` and `gpiozero`; GPIO Zero also needs
   a working pin factory, such as `lgpio` (especially for Pi 5). Match backend
   support to the actual board/OS. With lgpio installed and supported, set
   `export GPIOZERO_PIN_FACTORY=lgpio`.
   The system-site-packages venv exposes the distro's
   `/usr/lib/python3/dist-packages`; compiled GPIO modules must match the venv's
   Python ABI. A separately installed Python 3.11 on an OS whose distro Python
   is a different version cannot simply borrow those compiled modules. Use a
   matching supported OS/interpreter or build/install the hardware dependencies
   for that interpreter inside its venv; do not append arbitrary library paths
   or use blanket `sudo pip`.
3. Confirm the intended non-root user can access SPI and GPIO devices. On
   Raspberry Pi OS, add it to the `spi` and `gpio` groups if necessary
   (`sudo usermod -aG spi,gpio "$USER"`), then log out and back in.
   Do not switch to root to work around permissions: it changes the default
   safety-state location and browser cache.

### Official vendor driver (not bundled)

Run from the project directory. This exact official ZIP contains the required
`waveshare_epd.epd4in0e` module; a generic or older checkout may not.

```sh
mkdir -p .artifacts/vendor
curl --fail --location \
  'https://files.waveshare.com/wiki/4inch-e-Paper-HAT%2B-(E)/4inch_e-Paper_E.zip' \
  --output .artifacts/vendor/4inch_e-Paper_E.zip
unzip .artifacts/vendor/4inch_e-Paper_E.zip -d .artifacts/vendor
export PYTHONPATH="$PWD/.artifacts/vendor/4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib"
test -f "$PYTHONPATH/waveshare_epd/epd4in0e.py"
test -f "$PYTHONPATH/waveshare_epd/epdconfig.py"
```

The archive members are:

- `4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd4in0e.py`
- `4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epdconfig.py`

`PYTHONPATH` must point at **python/lib**, not the package directory itself.
Repeat the export in each new shell using the same absolute path. Importing
`epd4in0e` can initialize GPIO, so do not use import as a harmless installation
probe. Do not run vendor display demos alongside Paiper: they bypass its shared
refresh protection. No vendor files are modified by Paiper.

### Confirmation and corner calibration

The example defaults to:

```yaml
panel:
  rotation: 270
  confirmed: false
```

After checking the physical six-color E6 label and wiring, explicitly change
`confirmed` to `true`. **Even the first calibration display requires this
confirmation.** An unconfirmed `display` is rejected before browser launch,
state creation, or GPIO access.

```sh
paiper display --scene calibration --config config.yaml
paiper status
```

Inspect the TL/TR/BL/BR markers, all six swatches, and text. If the corners are
wrong, change `rotation: 270` to `rotation: 90`, wait **at least five minutes
after the previous attempt finishes**, then retry calibration. Once physically
verified, wait for the same cooldown before displaying `stillness`.

## Persistent refresh protection

Both `display` and `status` default to
`$XDG_STATE_HOME/paiper/display.sqlite3`, or
`~/.local/state/paiper/display.sqlite3` when XDG_STATE_HOME is unset/empty/not an
absolute path. The default is independent of the working directory.
`status` prints JSON with the resolved path, attempt/success timestamps and
digests, cooldown anchor, clock high-water mark, and `in_progress` guard.
Timestamps are Unix seconds; a fresh state has null timestamps.
Reading status initializes a new database if none exists, but does not refresh.

For an explicit path:

```sh
paiper display --scene stillness --config config.yaml \
  --state "$HOME/.local/state/paiper/display.sqlite3"
paiper status --state "$HOME/.local/state/paiper/display.sqlite3"
```

**All callers for one physical panel must share exactly one persistent path**,
including manual commands, other working directories, and any future services.
Changing users or XDG_STATE_HOME changes the default; supply the same absolute
`--state` when necessary. Store state on reliable local persistent storage.
Never delete, move, replace, restore older copies of, or rotate the database or
its sibling `.lock` file to bypass protection. Do not use a temporary state path
for hardware tests.

After confirmation, one nonblocking lock covers scene rendering, frame encoding,
reservation, lazy driver import, initialization, transfer, sleep/cleanup, and
durable completion. A competing display command fails before entering the
renderer; independent previews do not use this lock. Rendering/encoding failures
release the lock without recording or reserving a hardware attempt.
Duplicate successful frames and cooldown
requests are explicitly reported as **skipped**, not displayed. Skips return
zero; command/config/render/hardware/state failures are nonzero (argument
errors exit 2). Diagnostics go to stderr; outcomes/status go to stdout.
Unexpected programming errors remain visible rather than being swallowed.

Cooldown is at least **300 seconds from the end of each completed attempt**,
including caught failures and cleanup. A reservation without hardware access
also reserves 300 seconds. BUSY polling has a timeout, but this is not a timeout
for every possible OS/driver call. Interrupted attempts or failed completion
writes leave `in_progress` set and block further refreshes. Do not clear it to
force a retry: stop other callers, inspect the hardware and safety history,
and obtain an operator-reviewed recovery procedure; no automatic reset or
recovery command is provided. Keep a reliable system clock: backwards changes
fail closed, and arbitrary forward clock manipulation is not safe.

The foundation sends one frame, sleeps, and exits. Optional deployment timers
schedule attempts, not clears or periodic maintenance. Follow the panel's
storage/maintenance guidance during physical validation; safe multi-hour
operation is not established by automated software tests.
