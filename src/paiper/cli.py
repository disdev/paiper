"""One-shot daily-paper previews and explicitly confirmed, protected E6 refreshes."""

import argparse
import fcntl
import json
import logging
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from paiper.cache import ContentCache
from paiper.config import load_profile
from paiper.dashboard import build_pages
from paiper.display import (
    DriverInitializationError,
    DriverUnavailableError,
    render_and_display,
)
from paiper.panel import prepare_native
from paiper.playlist import record_success, select_page
from paiper.preferences import PAGES, load_preferences
from paiper.render import RenderError, render_dashboard, render_scene, save_preview
from paiper.scenes import SCENES
from paiper.state import (
    DisplayState,
    IncompleteAttemptError,
    StateCorruptError,
    StateLockedError,
)

logger = logging.getLogger(__name__)


def _default_state_path() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    root = Path(configured)
    if not configured or not root.is_absolute():
        root = Path.home() / ".local" / "state"
    return root / "paiper" / "display.sqlite3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Save a six-color PNG")
    preview.add_argument("--scene", choices=SCENES, required=True)
    preview.add_argument("--output", type=Path, required=True)
    preview.add_argument("--config", type=Path)
    preview.add_argument(
        "--native", action="store_true", help="Rotate to native 400x600 orientation"
    )
    display = commands.add_parser(
        "display", help="Refresh a confirmed E6 panel using persistent protection"
    )
    display.add_argument("--scene", choices=SCENES, required=True)
    display.add_argument("--config", type=Path, required=True)
    for command in (preview, display):
        command.add_argument("--page", choices=PAGES, help="Dashboard page override")
        command.add_argument(
            "--offline",
            action="store_true",
            help="Dashboard: cached/bundled only; no providers",
        )
        command.add_argument(
            "--cache",
            type=Path,
            default=_default_state_path().with_name("content.sqlite3"),
        )
        command.add_argument(
            "--dashboard-config", type=Path, help="Separate dashboard preferences YAML"
        )
    status = commands.add_parser(
        "status", help="Read persistent safety metadata as JSON"
    )
    for command in (display, status):
        command.add_argument(
            "--state",
            type=Path,
            default=_default_state_path(),
            help="Shared persistent SQLite path (default: %(default)s)",
        )
    return parser


def _reject_alias(cache: Path, safety: Path) -> None:
    if cache.resolve() == safety.resolve() or (
        cache.exists() and safety.exists() and cache.samefile(safety)
    ):
        raise ValueError("Content cache must not alias hardware safety state")


def _cache_lock_path(cache: Path) -> Path:
    identity = cache.stat()
    # A bounded per-user lock set also covers hardlinks in different directories.
    # Never flock SQLite itself: macOS flock conflicts with SQLite's own locks.
    slot = (identity.st_dev ^ identity.st_ino) % 64
    return _default_state_path().parent / "cache-locks" / f"{slot}.lock"


def _dashboard(args, profile) -> None:
    from paiper.sources import collect

    safety = args.state if args.command == "display" else _default_state_path()
    _reject_alias(args.cache, safety)
    prefs = load_preferences(args.dashboard_config, panel_path=args.config)
    proposal = None

    def factory():
        nonlocal proposal
        with ContentCache(args.cache) as cache:
            records = collect(
                cache,
                datetime.now(UTC),
                latitude=prefs.latitude,
                longitude=prefs.longitude,
                timezone=prefs.timezone,
                offline=args.offline,
            )
            for key, record in records.items():
                if record.error or record.payload is None:
                    logger.warning("%s: %s", key, record.error or "source unavailable")
            # Re-evaluate flight/alert validity after potentially slow collection.
            now = datetime.now(UTC)
            state = cache.get_state("playlist", {})
            pages = build_pages(
                records, now, prefs, recent_ids=tuple(state.get("recent_ids", ()))
            )
            proposal = select_page(pages, records, now, prefs, state, page=args.page)
        return render_dashboard(proposal.page, now, prefs.timezone)

    # Serialize dashboard collection AND its post-display commit. The existing
    # hardware lock still owns all safety decisions and actual device access.
    # Initialization is not collection and holds no transaction during rendering.
    with ContentCache(args.cache):
        pass
    lock_path = _cache_lock_path(args.cache)
    _reject_alias(lock_path, safety)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "Dashboard cache is busy; another collection/display is running"
            ) from error
        if args.command == "preview":
            image = factory()
            if args.native:
                image = prepare_native(image, profile)
            save_preview(image, args.output)
            print(
                f"Preview saved: {args.output} ({image.width}x{image.height}, dashboard"
                + (", offline sources)" if args.offline else ")")
            )
        elif render_and_display(factory, profile, args.state):
            if proposal is None:
                raise ValueError("Display returned success without generating a page")
            with ContentCache(args.cache) as cache:
                record_success(cache, proposal, datetime.now(UTC))
            print(f"Displayed dashboard page: {proposal.page.key}")
        else:
            print("Display skipped: unchanged frame or refresh cooldown (see stderr).")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; operational failures return 1, argument errors exit 2."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            with DisplayState(args.state) as state:
                print(
                    json.dumps(
                        {"state_path": str(state.path), **state.status()}, indent=2
                    )
                )
            return 0

        profile = load_profile(args.config)
        if args.command == "display" and not profile.confirmed:
            raise ValueError(
                "Panel profile is unconfirmed. Check the physical six-color E6 "
                "panel label, then set panel.confirmed: true before display."
            )
        if args.scene == "dashboard":
            _dashboard(args, profile)
            return 0
        if args.page or args.dashboard_config:
            raise ValueError("--page and --dashboard-config require --scene dashboard")
        if args.command == "preview":
            image = render_scene(args.scene, datetime.now(UTC))
            if args.native:
                image = prepare_native(image, profile)
            save_preview(image, args.output)
            print(
                f"Preview saved: {args.output} ({image.width}x{image.height}, OFFLINE)"
            )
        elif render_and_display(
            lambda: render_scene(args.scene, datetime.now(UTC)), profile, args.state
        ):
            print(f"Displayed offline scene: {args.scene}")
        else:
            print("Display skipped: unchanged frame or refresh cooldown (see stderr).")
        return 0
    except RenderError as error:
        logger.error(
            "%s. For missing Chromium run: python -m playwright install chromium. "
            "On supported Linux, also install browser OS dependencies with "
            "python -m playwright install-deps chromium.",
            error,
        )
    except DriverUnavailableError as error:
        logger.error(
            "%s. See README's Pi setup: install the official epd4in0e driver, "
            "set PYTHONPATH to its RaspberryPi_JetsonNano/python/lib directory, "
            "and install matching SPI/GPIO dependencies.",
            error,
        )
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        DriverInitializationError,
        StateLockedError,
        StateCorruptError,
        IncompleteAttemptError,
    ) as error:
        logger.error("%s", error)
    return 1
