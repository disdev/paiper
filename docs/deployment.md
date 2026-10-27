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

## 1. Fresh Raspberry Pi bootstrap

Use a supported 64-bit Raspberry Pi OS with distro Python >=3.11 (Bookworm's
Python 3.11 is the reference), sufficient disk for multiple separate Chromium
installations, a reliable clock, and persistent local storage. These OS and
hardware steps must be performed by the operator; the installer/updater never
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
check installation: import can initialize GPIO. Paiper does not bundle,
download on update, or modify the vendor code.

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
