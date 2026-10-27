# GitHub-managed Paiper deployment

Paiper currently renders only the offline `stillness` and `calibration` scenes.
There are no live sources. The example configuration deliberately retains
`panel.confirmed: false`. Repository publication, previews and CI do not verify
a physical panel or install anything on a Raspberry Pi.

The supported updater transport is a **public, credential-free HTTPS GitHub
repository**, initially `https://github.com/disdev/paiper.git`, branch `deploy`.
Private repository authentication is not implemented. Do not put tokens in
settings, Git URLs, units or configuration; the manager rejects credential URLs,
SSH, local paths and other hosts and disables Git configuration rewrites.

## Guided setup (recommended)

Supported: **Raspberry Pi 5 Model B, 64-bit Raspberry Pi OS Bookworm/Trixie
(Debian 12/13), distro `/usr/bin/python3` >=3.11**. A network connection, `curl`,
a terminal (including SSH), sufficient disk space, a reliable clock and the
ordinary user's working systemd session are required. Other boards, generic
Debian/Ubuntu installations, 32-bit userspaces and root execution are rejected.
Hardware is identified from device-tree/OS metadata, never by importing a driver.

Run this single compound shell command as the intended display user:

```sh
(umask 077; setup_dir="$HOME/.paiper-setup-$(/usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex)')" &&
 mkdir -- "$setup_dir" &&
 curl --fail --location --proto '=https' --tlsv1.2 \
   https://raw.githubusercontent.com/disdev/paiper/deploy/deploy/bootstrap.sh \
   --output "$setup_dir/bootstrap.sh" &&
 sh "$setup_dir/bootstrap.sh")
```

This entrypoint is available **after the guided-setup change passes CI and is
promoted to `deploy`**. Until then, use a reviewed local checkout's
`/usr/bin/python3 deploy/setup.py`; do not publish an untested branch to deploy.
The command downloads into a newly created, unpredictable, private directory;
`&&` prevents executing a failed/partial curl download. It never pipes a download
into sudo. Bootstrap resolves the public GitHub `deploy` ref once and extracts
that exact SHA into an owned directory under `~/.local/share/paiper-bootstrap`.
Archive links, traversal, special files, duplicates and multiple roots are
rejected before execution. The exact setup source is retained for resuming.
Subsequent release installation still belongs to the existing manager, which
fetches the then-current CI-promoted deploy ref and performs its real smoke tests.

### What the prompts do

All approval prompts default to **No**; there is no unattended hardware `--yes`
option. Answers are read from `/dev/tty`, even if bootstrap's stdin is redirected.

1. **Preserve your paths.** Choose an existing manual config by path, or use
   the persistent default. No YAML editing is needed. Confirm first use, or
   supply the **original existing state database** from manual operation before
   any GPIO. `--config PATH --state ORIGINAL_DB --vendor-lib PATH` can supply
   existing paths. Managed settings always win; conflicting replacements fail.
   Choices are saved before provisioning so reboot/rerun needs no state/vendor
   environment variables.
2. **OS prerequisites.** Inspect distro package/module availability without
   importing GPIO. If missing, explicitly approve `sudo apt-get update` and
   `sudo apt-get install --yes` for the listed packages: `git`, `python3-venv`,
   `python3-pip`, `python3-spidev`, `python3-gpiozero`, `python3-lgpio`,
   `raspi-config`. No OS upgrade is requested.
3. **SPI and permissions.** Separately approve
   `sudo raspi-config nonint do_spi 0` when SPI is disabled (this can load SPI
   pins immediately as well as persist boot configuration). Keep other SPI/GPIO
   programs stopped. Separately approve `sudo usermod -aG spi,gpio USER` for
   missing groups. Setup returns **2 (restart/new login needed)** after changes,
   never reboots, and tells you to reconnect and rerun as the same user.
4. **Chromium OS libraries.** Optionally approve a stable user-owned
   `~/.local/share/paiper/bootstrap-venv`, created with distro Python and system
   site packages. It installs Playwright constrained by the repository pins,
   then runs `python -m playwright install-deps chromium`, which invokes sudo
   for OS packages. Pip and the venv stay non-root; there is no editable install
   pointing at disposable source. Declining is appropriate if libraries are
   already provisioned; candidate offline smoke must still pass.
5. **Prepare the app.** The existing installer/manager builds and activates a
   healthy candidate, installs user units and leaves scheduling unchanged
   (`--no-update-timer`). On first installation both timers remain off.
   Known failed candidates require an explicit retry after fixing the cause.
   Explicitly approved first use initializes the safety database once through
   the healthy candidate's actual `DisplayState`, even if you stay offline.
6. **Vendor sources.** Reuse a valid configured vendor without modifying it,
   or explicitly approve filling a currently null `vendor_lib`. The official
   [E6 archive](https://files.waveshare.com/wiki/4inch-e-Paper-HAT%2B-(E)/4inch_e-Paper_E.zip)
   must match SHA-256
   `61edbb7992cbb3321029b2775c2ca6bdf6aea7663a138a9a8fa017bfa2a9ea47`.
   Only `__init__.py`, `epd4in0e.py`, and `epdconfig.py` from
   `4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/` are installed,
   with their source notices intact, by default under
   `~/.local/share/paiper/vendor`. No `.so`, `.pyc` or unrelated files are copied.
   A checksum mismatch fails before extraction/import. Existing customized
   vendor files are never overwritten. Updates never download vendor code.
7. **Calibrate or stay offline.** Skip calibration, or explicitly reuse a
   previously confirmed profile for this same hardware. For new calibration,
   approve pausing any existing display **timer**, not its service; a busy
   lifecycle fails rather than killing a refresh. Then confirm the exact
   **six-color E6 label and wiring** before vendor import or transfer. Setup
   uses the healthy candidate's real deferred renderer and `render_and_display`,
   under the original persistent state lock and outer lifecycle lock. The
   temporary calibration profile does not prematurely set stored confirmation.
   A zero CLI exit alone is not considered a transfer: skip has a distinct result.
8. **Verify visually.** Check six colors, text and TL/TR/BL/BR corners. A panel
   mounted 90° counterclockwise normally uses rotation **270**; if upside-down,
   choose **90**. The one opposite-orientation retry waits the remaining
   **at least 300 seconds after completed transfer**, using the same database;
   it never resets cooldown or busy-spins. Only actual transfer followed by
   your visual approval saves `confirmed: true` and rotation. The candidate's
   real profile validator checks the atomic edit, and the exact prior config is
   backed up beside it as `config.yaml.setup-<id>.bak`.
9. **Scheduling and boot.** Separately opt into display and update timers.
   Before enabling display, approve the `GPIOZERO_PIN_FACTORY=lgpio` service
   drop-in; calibration also sets that environment. Conflicting drop-ins and
   `EnvironmentFile` overrides are retained for manual review, not overwritten.
   Separately approve `sudo loginctl enable-linger USER` to run enabled user
   timers at boot without login.

Rerun the same command, or the retained exact-source command printed by
bootstrap. Prerequisites are rechecked, not treated as complete after failure.
Exit **0** means the requested installation steps completed (possibly offline
only), **1** means failure/interruption, and **2** requests reboot/new login.
No software-only outcome proves physical verification. Skipped, failed, or
interrupted calibration never creates a new stored confirmation; interrupted
hardware history remains fail-closed for operator inspection.

Existing settings/config bytes are unchanged unless you approve a vendor/panel
edit. Settings edits also get a sibling `.setup-<id>.bak`. Runtime, vendor,
bootstrap venv, config and state stay outside managed releases. Provisioning and
profile changes take the shared install/update/lifecycle/state locks in that
order; calibration lets the application acquire its own inner state lock.
Existing corrupt or interrupted state blocks provisioning too, using the
manager's unchanged state validator; setup never repairs or replaces history.

**State provenance and recovery:** `~/.local/share/paiper/setup-choices.json`
also records a `state_phase`. An existing selected database is `required` from
initial selection. Older saved choices or managed settings without first-use
provenance are also treated as `required`: a missing file is not evidence of an
unused panel. A missing required database stops reruns before external commands,
and candidate cooldown/calibration/confirmation operations cannot create it.
Restore access to the original persistent storage; do not select a new path.

Only explicit first-use approval for a missing database records `pending`.
That permission survives a reboot/new-login rerun **before initialization**.
Observing an existing database consumes it. Otherwise the candidate durably
writes `initializing` before opening SQLite, initializes the application's real
schema without GPIO, then persists `required`. Crashes/failures during that
phase deliberately block automatic retry—even if a valid-looking database
exists—because completion is ambiguous. Preserve the database, sibling locks,
config, settings and setup choices for operator-reviewed recovery. Never delete
or edit the phase record to regain creation permission, and never clear an
interrupted hardware marker. No automatic reset/recovery command is provided.
Keep these records on reliable persistent storage; they cannot identify the
physical panel or detect an operator substituting an old valid database.
The healthy candidate must support non-creating state access; an older candidate
without that API fails closed rather than falling back to creating history.

Setup does not stop services. Declining scheduling leaves enabled timers enabled;
actual enabled/active states are printed. If you approve pausing a display
timer, it stays paused on failure or declined re-enablement; restart it only
after resolving the issue. System commands use the manager's sanitized failure
summaries; commands that may need sudo show their interactive output locally.

`/usr/bin/python3 deploy/setup.py --help` and `sh deploy/bootstrap.sh --help`
are safe on macOS, but OS provisioning, systemd and actual E6 transfers require
on-device validation. No Pi/GPIO operation is claimed verified by local tests.

## Advanced manual installation

The remaining sections preserve the lower-level setup and operational guide.
Use them for already-provisioned devices or deliberate manual maintenance.

## 1. Fresh Raspberry Pi bootstrap (manual)

Use a supported 64-bit Raspberry Pi OS with distro Python >=3.11 (Bookworm's
Python 3.11 is the reference), sufficient disk for multiple separate Chromium
installations, a reliable clock, and persistent local storage. These OS and
hardware steps below are performed by the operator; `install.py`/the updater never
run apt, sudo, system package upgrades or browser `install-deps`.

```sh
sudo apt update
sudo apt install git python3-venv python3-pip python3-spidev \
  python3-gpiozero python3-lgpio curl unzip
mkdir -p "$HOME/src"
git clone https://github.com/disdev/paiper.git "$HOME/src/paiper"
cd "$HOME/src/paiper"
/usr/bin/python3 --version
/usr/bin/python3 -m venv --system-site-packages .venv-pi
. .venv-pi/bin/activate
python -m pip install --constraint requirements.lock -e .
python -m playwright install-deps chromium
python -m playwright install chromium
cp -n config.example.yaml config.yaml
```

`install-deps` is a separate provisioning operation and may request sudo.
Run pip, browser downloads, calibration, installer and manager as the intended
**non-root** display user; do not use `sudo pip` or run the manager with sudo.
Distro GPIO extensions must match the venv's Python ABI. A separately installed
Python cannot simply borrow incompatible distro binary modules.

Follow [README's SPI, wiring, groups and calibration instructions](../README.md).
Enable SPI with `sudo raspi-config`, reboot if required, and ensure your user can
access the SPI/GPIO devices (usually `spi` and `gpio` groups, followed by a fresh
login). Match the official **4-inch six-color E6** physical label, wiring, HAT,
board and driver; do not infer compatibility from the display size. Keep all
other programs away from its pins and SPI bus.

## 2. Stable driver and physical calibration first

Install the official driver **outside the checkout and managed releases**:

```sh
mkdir -p "$HOME/.local/share/paiper-vendor"
curl --fail --location \
  'https://files.waveshare.com/wiki/4inch-e-Paper-HAT%2B-(E)/4inch_e-Paper_E.zip' \
  --output "$HOME/.local/share/paiper-vendor/4inch_e-Paper_E.zip"
unzip "$HOME/.local/share/paiper-vendor/4inch_e-Paper_E.zip" \
  -d "$HOME/.local/share/paiper-vendor"
export PAIPER_VENDOR_LIB="$HOME/.local/share/paiper-vendor/4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib"
test -f "$PAIPER_VENDOR_LIB/waveshare_epd/epd4in0e.py"
test -f "$PAIPER_VENDOR_LIB/waveshare_epd/epdconfig.py"
export PYTHONPATH="$PAIPER_VENDOR_LIB"
export GPIOZERO_PIN_FACTORY=lgpio
```

Review the vendor archive before using it. Do not import the driver just to
check installation: import can initialize GPIO. Paiper does not bundle, download on update, or modify the vendor code. Guided
setup is the optional checksum-pinned source downloader described above.

Render both scenes without GPIO first:

```sh
paiper preview --scene stillness --output .artifacts/stillness.png
paiper preview --scene calibration --config config.yaml --native \
  --output .artifacts/calibration-native.png
```

After physical label/wiring verification, explicitly edit `config.yaml` to set
`panel.confirmed: true`. The first calibration also requires confirmation.

```sh
paiper display --scene calibration --config "$PWD/config.yaml"
paiper status
```

Verify all six colors, TL/TR/BL/BR corners and text. Adjust rotation from 270 to
90 only as needed; wait at least 300 seconds after every completed attempt
before trying again. Preserve the same user and state path throughout
calibration and managed operation. An interrupted refresh blocks later attempts;
never reset or replace its SQLite database to bypass that guard.

## 3. Install the updater (display remains opt-in)

Wait until GitHub shows a passing `ci` and the public `deploy` branch exists.
From the checkout, with the same XDG environment used for calibration:

```sh
/usr/bin/python3 deploy/install.py --config "$PWD/config.yaml" \
  --vendor-lib "$PAIPER_VENDOR_LIB"
```

This copies the standalone manager, creates settings and copies configuration
only if absent, then prepares and activates one healthy release **before**
installing user units and enabling `paiper-update.timer`. Preparation creates a
per-commit venv with system site packages, installs `requirements.lock`
constraints and its own Chromium, and validates real offline status and both
scenes in landscape/native orientation. No staging or activation step uses GPIO.
Network/build/browser failures leave the prior active release untouched.

You may omit `--config` for an offline-only install: the unconfirmed example is
copied. You may omit `--vendor-lib` until configuring hardware, but a display
opt-in requires an explicit stable vendor location in settings. The installer
does **not** silently change existing settings, config, state or display timer
enablement on reruns. Conflicting `--config` content or `--vendor-lib` fails.
To change a persistent setting, stop scheduling as appropriate, review and edit
`settings.json` yourself, and rerun; never move the hardware state to start fresh.
Changing the settings schema is not supported.

For an existing manual config/state pair, use
`--config-path /absolute/config.yaml --state /absolute/original.sqlite3`
to retain the configuration in place and persist the original state location on
first install. Conflicting paths on a rerun are refused. Add `--no-update-timer`
to prepare the healthy release and units without changing update scheduling;
the original installer default still enables the update timer. Guided setup
uses this opt-out and asks about each timer separately.

### Opt in to the display timer

Only after calibration and user-device permissions are correct:

```sh
/usr/bin/python3 deploy/install.py --enable-display
```

This loads the **stored configuration** through the installed release's profile
validator, requires an actual boolean `panel.confirmed: true`, and checks the
configured `waveshare_epd/epd4in0e.py` and `epdconfig.py` files without importing
them. It cannot validate physical wiring, actual GPIO access or board support.
If the board needs a pin factory, set it in a user service drop-in rather than
assuming shell exports reach systemd:

```sh
systemctl --user edit paiper-display.service
# Add:
# [Service]
# Environment=GPIOZERO_PIN_FACTORY=lgpio
systemctl --user daemon-reload
```

Configure that drop-in before enabling the display timer when required.
The display timer attempts a refresh one minute after boot and five minutes
after the prior service becomes inactive; it has no random delay. The updater
starts after two minutes and polls five minutes after completion with up to
20 seconds of random delay. Both use the stable `/usr/bin/python3` manager,
run as the user, and log to journald. Display has no start timeout that could
kill a slow physical refresh. Core display cooldown/locking remains authoritative:
successful service completion can mean **skipped**, not physically displayed.

For user services to run at boot without login, separately opt in as operator:

```sh
sudo loginctl enable-linger "$USER"
```

The installer does not elevate or enable linger. A functioning user systemd
session is required; it reports an error rather than falling back to root.
These units are Linux/systemd-specific and are not installed by tests on macOS.

## Stable paths and ownership

| Item | Default / rule |
| --- | --- |
| Runtime / manager | `~/.local/share/paiper/manager.py` |
| Settings | `~/.local/share/paiper/settings.json` |
| Config | `$XDG_CONFIG_HOME/paiper/config.yaml`, or `~/.config/paiper/config.yaml` |
| Hardware state | `$XDG_STATE_HOME/paiper/display.sqlite3`, or `~/.local/state/paiper/display.sqlite3` |
| Vendor | Explicit absolute `vendor_lib` outside releases; example above |
| Units | `$XDG_CONFIG_HOME/systemd/user/paiper-{update,display}.{service,timer}`, or `~/.config/systemd/user/...` |
| Releases | `~/.local/share/paiper/releases/<40-character-sha>/{app,.venv,browsers}` |
| Active / rollback pointers | `~/.local/share/paiper/current` and `previous` |
| Failure metadata | `~/.local/share/paiper/failed.json` |

XDG defaults require a nonempty absolute environment value; otherwise the home
fallback applies. The installing shell and user systemd manager must use the
same effective `XDG_CONFIG_HOME`, so systemd can discover the installed units.
A shell-only export does not change an already-running user manager's unit
search path; configure the user session consistently before installing. The
installer does not change the systemd environment or relocate old units.

State is the **original CLI default**, not a new deployment
database. Its resolved path is persisted in settings at first installation, so
later changes to the timer's environment do not change it. Any manually supplied
`--state` must match this exact persistent path for the same physical panel.
For an existing manually customized state location, review settings to use that
original location **before enabling any managed display**.

Configuration, state and vendor paths must stay outside releases. Updater
activation never updates those files. It takes SQLite-consistent snapshots and
requires compatible version-1 safety metadata; snapshots are validation copies,
not hardware state restores. The stable manager and unit files themselves are
updated only by explicitly rerunning a reviewed checkout's installer. Releases
cannot self-replace that manager. Keep runtime directories user-owned and do
not run untrusted code as this user; this is not a hostile-code sandbox.

## GitHub checks and deploy promotion

CI uses GitHub-hosted Ubuntu, Python 3.11, constrained runtime dependencies,
dev/build tools, real Chromium, Ruff and the complete pytest suite with
warnings treated as errors. It runs on pull requests, pushes to `main`, and
manual dispatch. The single stable required check is **`ci`**.

The workflow default is `contents: read`; only the separate `promote` job has
`contents: write`. Promotion needs successful `ci`, runs only for a push to
`main`, obtains full history, checks the live main SHA still matches the tested
SHA, and pushes it to `deploy` without force. A stale successful run skips
promotion; a diverged deploy branch fails rather than discarding history.
Promotion concurrency is serialized without canceling an in-flight push.
GitHub can briefly report a completed CI job as still in progress to branch
protection. Promotion retries only that specific rejection every 15 seconds
for up to five minutes, rechecking live main before each push. Other failures
stop immediately; exhausted retries leave deploy unchanged. No protection is
bypassed or disabled.
GitHub's workflow token does not trigger a second push workflow; deploy
protection uses the existing passing `ci` on that exact tested commit.

Repository administrators must configure main/deploy protection and read-only
default Actions permissions separately. Require `ci` for both, strict checks
and PRs for main (zero mandatory reviewers is suitable for a solo maintainer),
no force pushes/deletions and administrator enforcement. Deploy must permit
direct promotion without a PR requirement; do not assume a token bypass.
`requirements.lock` remains the stable manager/CI constraints entrypoint. It
includes `requirements.txt` with `-c`, so the one canonical set of exact runtime
pins has a filename Dependabot's pip updater discovers (arbitrary `.lock`
filenames are not discovered). Weekly Dependabot updates cover these pip
manifests/constraints and pinned Actions;
no auto-merge or paid functionality is enabled. No source license grant has
been selected; bundled font attribution remains intact.

## Operations, failures and manual rollback

```sh
systemctl --user list-timers 'paiper-*'
systemctl --user status paiper-update.timer paiper-display.timer
systemctl --user status paiper-update.service paiper-display.service
journalctl --user -u paiper-update.service -n 100 --no-pager
journalctl --user -u paiper-display.service -n 100 --no-pager
/usr/bin/python3 "$HOME/.local/share/paiper/manager.py" status
```

For later manual application `status` commands, use the state path from settings.
The manager's `status` reads deployment metadata only and never touches GPIO.

Preparation and health failures report the stage and exit status, timeout, or OS
error with sanitized stderr/stdout excerpts. These distinguish causes such as
DNS resolution, full disks and missing dependencies. Candidate failure reasons
are also retained in `failed.json` and exposed by manager `status`; fetch failures
are reported without attributing them to a candidate SHA. The installer uses the
same diagnostic formatter for its command failures.

Each command excerpt is capped at 850 characters per stream and each failure
summary at 2,048 characters, retaining the beginning and end around a truncation
marker. Redaction runs before truncation and removes whole URLs,
authorization/cookie/API-key headers, common
secret assignments, recognizable token formats and values of secret-named
environment variables. Subprocess argument lists are not echoed. This is
best-effort sanitization, not a guarantee for arbitrary unlabelled or encoded
secrets; do not supply credentials to builds or publish logs without review.
The bounds apply to reported/persisted diagnostics, not subprocess capture
memory. Normal managed display stdout/stderr still pass through unchanged to
the terminal/journal, with no display timeout or automatic rollback.

A rejected candidate SHA stays in `failed.json`, so each poll does not repeat
an expensive broken install. Correct the prerequisite or publish a fixed commit.
To explicitly retry the same SHA after investigating:

```sh
/usr/bin/python3 "$HOME/.local/share/paiper/manager.py" update --retry
```

The installer has no implicit retry override. After a failed first install,
run that command successfully and rerun the installer to enable the timer.

To return to the previous healthy, compatible release:

```sh
/usr/bin/python3 "$HOME/.local/share/paiper/manager.py" rollback
```

Rollback uses the same locks and offline health checks as activation. It marks
the rolled-back-from SHA failed so the next poll will not reinstall it. It does
not restore config or SQLite state, reset cooldown, repair GPIO or make schema
migrations reversible. It is limited to compatible application/settings/state
schemas. There is no previous release on a first install.

To suspend scheduled attempts without terminating an in-flight physical refresh:

```sh
systemctl --user disable --now paiper-display.timer
systemctl --user disable --now paiper-update.timer
```

Do not stop/kill a running physical display service just to update. Shared
lifecycle/state locks prevent simultaneous activation and hardware access;
busy operations skip safely. Later display failures do not automatically roll
back code. Monitor disk usage: old ready releases and some interrupted scratch
work are retained, not automatically garbage-collected. Never delete current,
previous, safety state, lock files, or active work to recover disk space.

## Validation boundaries

Local tests exercise real Git promotion, profile validation, SQLite state,
release links and locks with injected systemctl/build seams. The integration
smoke additionally installs real constrained packages and per-release Chromium,
checks offline renders, updates between two commits, rolls back and verifies
failed-SHA suppression without invoking display/GPIO. CI validates the software,
not physical refresh safety. Pi OS provisioning, live user-systemd behavior,
device calibration and prolonged hardware operation still require on-device
verification.
