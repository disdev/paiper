"""Interactive, non-root setup for Raspberry Pi 5 and Raspberry Pi OS 12/13."""

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import pwd
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from uuid import uuid4

SOURCE = Path(__file__).resolve().parent
PYTHON = "/usr/bin/python3"
VENDOR_URL = (
    "https://files.waveshare.com/wiki/4inch-e-Paper-HAT%2B-(E)/4inch_e-Paper_E.zip"
)
VENDOR_SHA256 = "61edbb7992cbb3321029b2775c2ca6bdf6aea7663a138a9a8fa017bfa2a9ea47"
VENDOR_PREFIX = "4inch_e-Paper_E/RaspberryPi_JetsonNano/python/lib/waveshare_epd/"
VENDOR_FILES = ("__init__.py", "epd4in0e.py", "epdconfig.py")
MODULES = {}


def module(name):
    if name in MODULES:
        return MODULES[name]
    spec = importlib.util.spec_from_file_location(
        "paiper_setup_" + name, SOURCE / f"{name}.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    MODULES[name] = loaded
    return loaded


def probe_platform():
    return {
        "model": Path("/proc/device-tree/model").read_text().strip("\0\n"),
        "machine": platform.machine(),
        "bits": struct.calcsize("P") * 8,
        "os": platform.freedesktop_os_release(),
        "pi_os": Path("/etc/rpi-issue").is_file(),
    }


def validate_platform(facts):
    release = facts["os"]
    if (
        not facts["model"].startswith("Raspberry Pi 5 Model B")
        or facts["machine"] != "aarch64"
        or facts["bits"] != 64
        or release.get("ID") not in ("debian", "raspbian")
        or release.get("VERSION_ID") not in ("12", "13")
        or not facts["pi_os"]
    ):
        raise ValueError(
            "Supported: Raspberry Pi 5, 64-bit Raspberry Pi OS Bookworm/Trixie (12/13)"
        )


def terminal_prompt(question):
    # stdin may be the bootstrap program; never consume piped answers as consent.
    with open("/dev/tty", "w") as terminal:
        terminal.write(question + " ")
        terminal.flush()
    with open("/dev/tty", "r") as terminal:
        answer = terminal.readline()
        if not answer:
            raise EOFError("No terminal answer")
        return answer.strip()


def approve(prompt, question):
    return prompt(question + " [y/N]").lower() == "y"


def extract_vendor(data, destination):
    if hashlib.sha256(data).hexdigest() != VENDOR_SHA256:
        raise ValueError("Vendor checksum mismatch; nothing extracted")
    if destination.exists() or destination.is_symlink():
        raise ValueError(
            "Vendor destination exists; refusing to overwrite customized files"
        )
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        selected = []
        for name in VENDOR_FILES:
            matches = [
                entry
                for entry in archive.infolist()
                if entry.filename == VENDOR_PREFIX + name
            ]
            if len(matches) != 1 or stat.S_ISLNK(matches[0].external_attr >> 16):
                raise ValueError("Missing, duplicate or unsafe vendor source")
            selected.append((name, archive.read(matches[0])))
    staging = destination.with_name(".vendor-" + uuid4().hex)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        staging.mkdir(mode=0o700)
        package = staging / "waveshare_epd"
        package.mkdir()
        for name, content in selected:
            (package / name).write_bytes(content)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def command(runner, argv, *, check=True, timeout=60, env=None, cwd=None, capture=True):
    return runner(
        list(map(str, argv)),
        check=check,
        capture_output=capture,
        timeout=timeout,
        env=env,
        cwd=str(cwd) if cwd else None,
    )


def text(result):
    output = result.stdout or b""
    return output.decode().strip() if isinstance(output, bytes) else output.strip()


def prerequisites(root, runner, prompt):
    command(
        runner, [PYTHON, "-I", "-c", "import sys; assert sys.version_info >= (3, 11)"]
    )
    packages = (
        "git",
        "python3-venv",
        "python3-pip",
        "python3-spidev",
        "python3-gpiozero",
        "python3-lgpio",
        "raspi-config",
    )
    missing = [
        package
        for package in packages
        if text(
            command(runner, ["dpkg-query", "-W", "-f=${Status}", package], check=False)
        )
        != "install ok installed"
    ]
    if missing:
        if not approve(
            prompt,
            "Missing prerequisites: "
            + ", ".join(missing)
            + ". Run sudo apt-get update and sudo apt-get install for these packages (no OS upgrade)?",
        ):
            raise ValueError("Missing prerequisites; no packages were installed")
        command(runner, ["sudo", "apt-get", "update"], timeout=1800, capture=False)
        command(
            runner,
            ["sudo", "apt-get", "install", "--yes", *missing],
            timeout=1800,
            capture=False,
        )
    command(
        runner,
        [
            PYTHON,
            "-I",
            "-c",
            (
                "import importlib.util; "
                "assert all(importlib.util.find_spec(n) for n in "
                "('venv', 'ensurepip', 'pip', 'spidev', 'gpiozero', 'lgpio'))"
            ),
        ],
    )
    spi = text(command(runner, ["raspi-config", "nonint", "get_spi"]))
    if spi not in ("0", "1"):
        raise ValueError("Cannot determine SPI configuration")
    restart = False
    if spi == "1":
        if not approve(
            prompt,
            "Enable SPI now and persist for boot using sudo raspi-config nonint do_spi 0? This may load SPI pins; confirm other GPIO/SPI applications are stopped. No automatic reboot.",
        ):
            raise ValueError("SPI is disabled; no SPI change was made")
        command(
            runner, ["sudo", "raspi-config", "nonint", "do_spi", "0"], capture=False
        )
        restart = True
    groups = set(text(command(runner, ["id", "-nG"])).split())
    missing_groups = sorted({"spi", "gpio"} - groups)
    if missing_groups:
        username = pwd.getpwuid(os.getuid()).pw_name
        if not approve(
            prompt,
            f"Add {username} to {','.join(missing_groups)} using sudo usermod -aG? Requires a new login.",
        ):
            raise ValueError("SPI/GPIO group permissions are required")
        command(
            runner,
            ["sudo", "usermod", "-aG", ",".join(missing_groups), username],
            capture=False,
        )
        restart = True
    if restart:
        print(
            "Restart needed: allow any active refresh to finish, reboot or log out/in as appropriate."
        )
        print(
            "Reconnect as this same user and rerun the setup command; choices and state are preserved."
        )
        return 2
    return 0


def browser_dependencies(root, runner, prompt):
    if not approve(
        prompt,
        "Provision Chromium OS libraries? A user-owned pinned Playwright venv will run install-deps chromium (invokes sudo apt; no OS upgrade).",
    ):
        print(
            "Browser OS provisioning skipped; the manager's real offline smoke must still pass."
        )
        return
    venv = root / "bootstrap-venv"
    if venv.is_symlink():
        raise ValueError("Unsafe bootstrap venv")
    work = root / "work"
    module("manager").Manager._directory(work)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "PIP_")) and key != "VIRTUAL_ENV"
    }
    env.update(TMPDIR=str(work), TMP=str(work), TEMP=str(work))
    command(
        runner,
        [PYTHON, "-m", "venv", "--system-site-packages", venv],
        env=env,
        timeout=180,
    )
    python = venv / "bin/python"
    command(
        runner,
        [
            python,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--constraint",
            SOURCE.parent / "requirements.lock",
            "playwright",
        ],
        env=env,
        timeout=1800,
    )
    command(
        runner,
        [python, "-m", "playwright", "install-deps", "chromium"],
        env=env,
        timeout=1800,
        capture=False,
    )


def choose_paths(root, args, prompt):
    manager = module("manager")
    settings_path = root / "settings.json"
    choices_path = root / "setup-choices.json"
    saved = None
    if choices_path.exists() or choices_path.is_symlink():
        saved = manager.read_json(choices_path)
        if (
            not isinstance(saved, dict)
            or set(saved)
            not in (
                {"config", "state", "vendor_lib"},
                {"config", "state", "vendor_lib", "state_phase"},
            )
            or saved.get("state_phase", "required")
            not in ("pending", "initializing", "required")
        ):
            raise ValueError("Invalid saved setup choices")
    if settings_path.exists() or settings_path.is_symlink():
        stored = manager.Manager(root).settings
        if saved and any(saved[key] != stored[key] for key in ("config", "state")):
            raise ValueError("Saved setup choices conflict with authoritative settings")
    elif saved is not None:
        stored = saved
    else:
        installer = module("install")
        home = Path.home()
        config = args.config
        if config is None:
            answer = prompt(
                "Existing manual config path, or Enter to use the persistent default (no YAML editing needed):"
            )
            config = Path(answer).expanduser() if answer else None
        if config is not None and not config.expanduser().is_file():
            raise ValueError("Existing manual config was not found")
        config = (
            config
            or installer._xdg_path("XDG_CONFIG_HOME", home / ".config")
            / "paiper/config.yaml"
        )
        state = args.state
        first_use = False
        if state is None:
            default = (
                installer._xdg_path("XDG_STATE_HOME", home / ".local/state")
                / "paiper/display.sqlite3"
            )
            if approve(
                prompt,
                f"Is this the FIRST Paiper use of this panel (no previous custom state)? Use {default}; never replace prior safety history.",
            ):
                state = default
                first_use = True
            else:
                answer = prompt(
                    f"Enter the ORIGINAL state database path (default was {default}); required before GPIO:"
                )
                if not answer:
                    raise ValueError(
                        "Original state path required; no new safety history chosen"
                    )
                state = Path(answer).expanduser()
                if not state.is_file():
                    raise ValueError("Original state database does not exist")
        if args.state and not args.state.expanduser().is_file():
            raise ValueError("--state must name your existing original database")
        state_phase = (
            "pending"
            if first_use and not (state.exists() or state.is_symlink())
            else "required"
        )
        stored = {
            "config": str(config.expanduser().resolve()),
            "state": str(state.expanduser().resolve()),
            "vendor_lib": str(args.vendor_lib.expanduser().resolve())
            if args.vendor_lib
            else None,
        }
        for value in stored.values():
            if value and Path(value).is_relative_to(root / "releases"):
                raise ValueError("Persistent paths must stay outside releases")
        selected_config = Path(stored["config"])
        if not selected_config.exists():
            installer._copy_atomic(
                SOURCE.parent / "config.example.yaml", selected_config
            )
        stored["state_phase"] = state_phase
        manager.write_json(choices_path, stored)
        saved = stored
    for key in ("config", "state", "vendor_lib"):
        value = stored[key]
        if key == "vendor_lib" and value is None:
            continue
        if (
            not isinstance(value, str)
            or "\0" in value
            or not Path(value).is_absolute()
            or Path(value).resolve().is_relative_to(root / "releases")
        ):
            raise ValueError(
                "Invalid saved setup choices; paths must be absolute and outside releases"
            )
    for attribute, key in (
        ("config", "config"),
        ("state", "state"),
        ("vendor_lib", "vendor_lib"),
    ):
        supplied = getattr(args, attribute)
        if (
            supplied
            and stored[key] is not None
            and str(supplied.expanduser().resolve()) != stored[key]
        ):
            raise ValueError(
                f"Existing {key} differs; use the authoritative saved path"
            )
    paths = {key: stored[key] for key in ("config", "state", "vendor_lib")}
    if saved is None or "state_phase" not in saved:
        # Old managed settings/choices cannot prove that this panel is unused.
        manager.write_json(choices_path, {**paths, "state_phase": "required"})
    return paths


def check_setup_state(root, state, *, allow_pending=False):
    """Called under the state lock; only unconsumed first-use consent permits absence."""
    shared = module("manager")
    saved = shared.read_json(root / "setup-choices.json")
    if saved.get("state") != str(state):
        raise ValueError("Safety state differs from saved setup provenance")
    phase = saved.get("state_phase")
    if phase == "initializing":
        raise ValueError(
            "Interrupted state initialization; preserve all files for operator review"
        )
    if phase not in ("pending", "required"):
        raise ValueError("Invalid safety state provenance")
    if phase == "pending" and (state.exists() or state.is_symlink()):
        # Observing a database consumes first-use permission even if it is invalid.
        shared.write_json(
            root / "setup-choices.json", {**saved, "state_phase": "required"}
        )
        phase = "required"
    if phase == "pending" and allow_pending:
        return phase
    shared.validate_state(state)
    return phase


def calibrate(operation, prompt, sleep=time.sleep):
    profile = operation("profile")
    if not approve(
        prompt,
        "Confirm the physical Waveshare 4-inch six-color E6 label, correct Pi 5 HAT/wiring and exclusive SPI/GPIO use. Send a calibration refresh now?",
    ):
        return False
    rotation = profile["rotation"]
    for attempt in range(2):
        remaining = operation("remaining")["seconds"]
        if remaining:
            if not approve(
                prompt,
                f"Wait {remaining} seconds for the persistent 300-second cooldown before this transfer?",
            ):
                return False
            sleep(remaining)
            if operation("remaining")["seconds"]:
                raise ValueError("Cooldown remains; rerun later (state was not reset)")
        if (
            operation("calibrate", rotation, profile["digest"]).get("transferred")
            is not True
        ):
            print(
                "Calibration skipped, not transferred. No new confirmation was saved; rerun later."
            )
            return False
        if approve(
            prompt,
            f"Are all six colors, readable text and TL/TR/BL/BR corners correct? Save confirmed:true and rotation:{rotation}, with a config backup?",
        ):
            operation("confirm", rotation, profile["digest"])
            return True
        if attempt or not approve(
            prompt,
            "Is it upside-down? Try the opposite rotation (270 ↔ 90), respecting the same cooldown?",
        ):
            return False
        rotation = 90 if rotation == 270 else 270
    return False


def timer_status(purpose, runner):
    name = f"paiper-{purpose}.timer"
    enabled = text(
        command(runner, ["systemctl", "--user", "is-enabled", name], check=False)
    )
    active = text(
        command(runner, ["systemctl", "--user", "is-active", name], check=False)
    )
    print(f"{name}: {enabled or 'unknown'}, {active or 'unknown'}")
    return enabled, active


def offer_timer(purpose, runner, prompt):
    timer_status(purpose, runner)
    if approve(
        prompt,
        f"Enable/start paiper-{purpose}.timer for scheduled {'updates' if purpose == 'update' else 'hardware display attempts'}?",
    ):
        command(
            runner,
            ["systemctl", "--user", "enable", "--now", f"paiper-{purpose}.timer"],
        )
        timer_status(purpose, runner)
    else:
        print(f"paiper-{purpose}.timer left unchanged.")


def pin_factory(units, runner, prompt):
    dropin = units / "paiper-display.service.d/10-paiper-gpio.conf"
    content = "[Service]\nEnvironment=GPIOZERO_PIN_FACTORY=lgpio\n"
    if dropin.is_symlink() or (dropin.exists() and dropin.read_text() != content):
        raise ValueError(
            "Existing GPIO drop-in differs; preserve and review it manually"
        )
    files = text(
        command(
            runner,
            [
                "systemctl",
                "--user",
                "show",
                "paiper-display.service",
                "--property=EnvironmentFiles",
                "--value",
            ],
        )
    )
    if files:
        raise ValueError(
            "Existing EnvironmentFile may override GPIO; preserved for manual review"
        )
    effective = text(
        command(
            runner,
            [
                "systemctl",
                "--user",
                "show",
                "paiper-display.service",
                "--property=Environment",
                "--value",
            ],
        )
    )
    for value in shlex.split(effective):
        if (
            value.startswith("GPIOZERO_PIN_FACTORY=")
            and value != "GPIOZERO_PIN_FACTORY=lgpio"
        ):
            raise ValueError(
                "Conflicting GPIO pin factory in user drop-in; it was not overwritten"
            )
    if not dropin.exists():
        if not approve(
            prompt,
            "Write a user display service drop-in setting GPIOZERO_PIN_FACTORY=lgpio before enabling hardware scheduling?",
        ):
            return False
        module("manager").Manager._directory(dropin.parent)
        with dropin.open("x", encoding="utf-8") as stream:
            stream.write(content)
        command(runner, ["systemctl", "--user", "daemon-reload"])
    effective = text(
        command(
            runner,
            [
                "systemctl",
                "--user",
                "show",
                "paiper-display.service",
                "--property=Environment",
                "--value",
            ],
        )
    )
    values = [
        value
        for value in shlex.split(effective)
        if value.startswith("GPIOZERO_PIN_FACTORY=")
    ]
    if values != ["GPIOZERO_PIN_FACTORY=lgpio"]:
        raise ValueError(
            "Cannot verify effective lgpio drop-in; display scheduling not enabled"
        )
    return True


def fetch_bytes(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        if not response.url.startswith("https://"):
            raise ValueError("Download redirected away from HTTPS")
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("Download exceeds size limit")
    return data


def valid_vendor(path, root):
    if not path.is_absolute() or path.resolve().is_relative_to(root / "releases"):
        raise ValueError(
            "Vendor must stay outside releases at an absolute stable location"
        )
    if not all((path / "waveshare_epd" / name).is_file() for name in VENDOR_FILES):
        raise ValueError(
            "Vendor needs __init__.py, epd4in0e.py and epdconfig.py; not imported"
        )


def configure_vendor(manager, supplied, prompt, fetch=fetch_bytes):
    existing = manager.settings["vendor_lib"]
    if existing:
        if supplied and supplied.expanduser().resolve() != Path(existing):
            raise ValueError(
                "Existing vendor differs; settings and customized files preserved"
            )
        valid_vendor(Path(existing), manager.root)
        print(
            f"Reusing configured vendor at {existing}; files were not changed or imported."
        )
        return True
    destination = (
        supplied.expanduser().resolve() if supplied else manager.root / "vendor"
    )
    if supplied or destination.exists():
        valid_vendor(destination, manager.root)
        question = f"Reuse vendor at {destination} and set currently null vendor_lib in settings (with backup)?"
    else:
        question = f"Download the checksum-pinned official E6 vendor sources to {destination} and set currently null vendor_lib in settings (with backup)?"
    if not approve(prompt, question):
        return False
    if not destination.exists():
        extract_vendor(fetch(VENDOR_URL), destination)
    valid_vendor(destination, manager.root)
    shared = module("manager")
    state = Path(manager.settings["state"]).resolve()
    with shared.lock(state.with_name(state.name + ".lock")):
        path = manager.root / "settings.json"
        if shared.read_json(path) != manager.settings:
            raise ValueError("Settings changed during setup")
        backup = path.with_name(f"settings.json.setup-{uuid4().hex}.bak")
        with backup.open("xb") as stream:
            stream.write(path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        backup.chmod(0o600)
        updated = {**manager.settings, "vendor_lib": str(destination)}
        shared.write_json(path, updated)
        manager.settings = updated
    return True


def candidate_operation(manager, release, runner, action, rotation=270, digest=""):
    state = Path(manager.settings["state"])
    shared = module("manager")
    with shared.lock(state.with_name(state.name + ".lock")):
        phase = check_setup_state(
            manager.root, state, allow_pending=action == "initialize"
        )
        if action == "initialize" and phase != "pending":
            raise ValueError(
                "State initialization permission has already been consumed"
            )
    env = manager._environment(release, display=action == "calibrate")
    env["GPIOZERO_PIN_FACTORY"] = "lgpio"
    argv = [
        release / ".venv/bin/python",
        "-c",
        (SOURCE / "setup_candidate.py").read_text(),
        str(manager.root / "setup-choices.json"),
        action,
        manager.settings["config"],
        manager.settings["state"],
        str(rotation),
        digest,
    ]
    result = command(
        runner,
        argv,
        check=False,
        timeout=None if action == "calibrate" else 60,
        env=env,
        cwd=release / "app",
    )
    if result.returncode not in (0, 3):
        raise subprocess.CalledProcessError(
            result.returncode, ["candidate"], result.stdout, result.stderr
        )
    records = [
        line.removeprefix("PAIPER_SETUP_RESULT=")
        for line in text(result).splitlines()
        if line.startswith("PAIPER_SETUP_RESULT=")
    ]
    if len(records) != 1:
        raise ValueError(
            "Candidate did not report a unique setup result; no calibration confirmation"
        )
    report = json.loads(records[0])
    if not isinstance(report, dict):
        raise ValueError("Invalid candidate result")  # noqa: TRY004
    if action == "calibrate":
        if type(report.get("transferred")) is not bool or result.returncode != (
            0 if report["transferred"] else 3
        ):
            raise ValueError("Invalid transfer result; no calibration confirmation")
    elif result.returncode:
        raise ValueError("Candidate action did not complete")
    if action == "initialize" and report.get("initialized") is not True:
        raise ValueError("Candidate did not complete state initialization")
    return report


def pause_display(runner, prompt):
    _, active = timer_status("display", runner)
    if active != "inactive" and active != "unknown":
        if not approve(
            prompt,
            "Pause the display TIMER for calibration/profile changes? Never stop the active display service. You must explicitly restart scheduling afterwards.",
        ):
            return False
        command(runner, ["systemctl", "--user", "stop", "paiper-display.timer"])
        print(
            "Display timer paused by consent; it will remain paused unless you approve starting it again."
        )
    return True


def device_access():
    return all(
        os.access(path, os.R_OK | os.W_OK)
        for path in ("/dev/spidev0.0", "/dev/gpiochip0")
    )


def run_setup(args, *, runner, prompt):
    shared, installer = module("manager"), module("install")
    root = Path.home() / ".local/share/paiper"
    shared.Manager._directory(root)
    with shared.lock(root / "setup.lock"):
        with (
            shared.lock(root / "install.lock"),
            shared.lock(root / "update.lock"),
            shared.lock(root / "lifecycle.lock"),
        ):
            choices = choose_paths(root, args, prompt)
            state = Path(choices["state"]).resolve()
            with shared.lock(state.with_name(state.name + ".lock")):
                check_setup_state(root, state, allow_pending=True)
                restart = prerequisites(root, runner, prompt)
                if restart:
                    return restart
                browser_dependencies(root, runner, prompt)
        if not Path(choices["config"]).is_file():
            raise ValueError(
                "Saved configuration is missing; refusing to replace it with defaults"
            )
        install_args = [
            "--no-update-timer",
            "--config-path",
            choices["config"],
            "--state",
            choices["state"],
        ]
        # Set a supplied vendor only after the separate explicit settings consent.
        if installer.main(install_args, runner=runner):
            print("Preparation failed. Fix the reported prerequisite before retrying.")
            if not (root / "settings.json").is_file() or not approve(
                prompt,
                "Retry a previously failed candidate SHA now through the manager?",
            ):
                return 1
            shared.Manager(root, runner=runner).update(retry=True)
            if installer.main(install_args, runner=runner):
                return 1
        with (
            shared.lock(root / "install.lock"),
            shared.lock(root / "update.lock"),
            shared.lock(root / "lifecycle.lock"),
        ):
            manager = shared.Manager(root, runner=runner)
            status = manager.status()
            if not status["current"] or status["activation_pending"]:
                raise ValueError("No healthy active candidate available")
            release = manager._release(status["current"])
            with shared.lock(state.with_name(state.name + ".lock")):
                phase = check_setup_state(root, state, allow_pending=True)
            if phase == "pending":
                candidate_operation(manager, release, runner, "initialize")
            with shared.lock(state.with_name(state.name + ".lock")):
                check_setup_state(root, state)
            print(f"Authoritative config: {manager.settings['config']}")
            print(
                f"Authoritative safety state: {manager.settings['state']} (never reset)"
            )
            vendor = configure_vendor(
                manager,
                args.vendor_lib
                or (Path(choices["vendor_lib"]) if choices["vendor_lib"] else None),
                prompt,
            )

            def operation(action, rotation=270, digest=""):
                return candidate_operation(
                    manager, release, runner, action, rotation, digest
                )

            verified = False
            if vendor:
                profile = operation("profile")
                if profile["confirmed"]:
                    verified = approve(
                        prompt,
                        "Reuse this stored confirmed panel profile without new calibration? Only approve if this is the same physically verified panel/wiring.",
                    )
                if (
                    not verified
                    and approve(
                        prompt,
                        "Calibrate the panel now? Decline to keep the installed app offline-only (existing timers are not silently disabled).",
                    )
                    and pause_display(runner, prompt)
                ):
                    if not device_access():
                        raise ValueError(
                            "SPI/GPIO devices are not readable/writable. Reboot or start a fresh login, then rerun; do not reset state"
                        )
                    verified = calibrate(operation, prompt)
                if verified:
                    if not device_access():
                        raise ValueError(
                            "SPI/GPIO device access is required before display scheduling"
                        )
                    if operation("profile")["confirmed"] is not True:
                        raise ValueError(
                            "Stored profile is no longer confirmed; display scheduling not enabled"
                        )
                    units = (
                        installer._xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")
                        / "systemd/user"
                    )
                    if pin_factory(units, runner, prompt):
                        offer_timer("display", runner, prompt)
            if not verified:
                print(
                    "No new hardware verification claimed. Display scheduling was not enabled by setup."
                )
                timer_status("display", runner)
            offer_timer("update", runner, prompt)
            if approve(
                prompt,
                "Allow this user's enabled timers to run at boot without login using sudo loginctl enable-linger?",
            ):
                username = pwd.getpwuid(os.getuid()).pw_name
                command(
                    runner,
                    ["sudo", "loginctl", "enable-linger", username],
                    capture=False,
                )
            print(
                "Setup prepared the app. Review timer states above; physical verification is operator-only."
            )
    return 0


def main(
    argv=None, *, runner=subprocess.run, prompt=terminal_prompt, probe=probe_platform
):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, help="Existing manual profile to use in place"
    )
    parser.add_argument(
        "--state", type=Path, help="Original persistent display state; never a reset"
    )
    parser.add_argument(
        "--vendor-lib", type=Path, help="Existing stable Waveshare python/lib"
    )
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print(
            "Setup error: run as the display user, not root; do not use sudo",
            file=sys.stderr,
        )
        return 1
    if sys.version_info < (3, 11):  # noqa: UP036 -- standalone entrypoint
        print("Setup error: use distro Python 3.11 or newer", file=sys.stderr)
        return 1
    manager_module = module("manager")
    try:
        validate_platform(probe())
        root = Path.home() / ".local/share/paiper"
        if any(
            (root / name).exists() or (root / name).is_symlink()
            for name in ("settings.json", "setup-choices.json")
        ):
            with manager_module.lock(root / "setup.lock"):
                choices = choose_paths(root, args, prompt)
                state = Path(choices["state"])
                with manager_module.lock(state.with_name(state.name + ".lock")):
                    check_setup_state(root, state, allow_pending=True)
        command(
            runner,
            [PYTHON, "-I", "-c", "import sys; assert sys.version_info >= (3, 11)"],
        )
        return run_setup(args, runner=runner, prompt=prompt)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        print(
            "Setup error: " + manager_module.command_failure("Setup command", error),
            file=sys.stderr,
        )
    except (
        ValueError,
        manager_module.DeploymentError,
        zipfile.BadZipFile,
        EOFError,
        KeyboardInterrupt,
    ) as error:
        print(
            "Setup error: "
            + manager_module.sanitize_diagnostic(
                str(error) or "Interrupted; rerun safely"
            ),
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
