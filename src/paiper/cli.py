"""One-shot offline previews and explicitly confirmed, protected E6 refreshes."""

import argparse
import json
import logging
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from paiper.config import load_profile
from paiper.display import (
    DriverInitializationError,
    DriverUnavailableError,
    render_and_display,
)
from paiper.panel import prepare_native
from paiper.render import RenderError, render_scene, save_preview
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
    preview = commands.add_parser("preview", help="Save an offline six-color PNG")
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
