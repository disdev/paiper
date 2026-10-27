"""Strict dashboard preferences, deliberately separate from the panel profile."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PAGES = (
    "morning",
    "weather",
    "cardinals",
    "markets",
    "usd-pen",
    "news",
    "ai",
    "verse",
    "gallery",
    "flights",
    "alert",
)
DEFAULT_PLAYLISTS = {
    "morning": ("morning", "verse", "weather", "cardinals"),
    "day": ("markets", "ai", "news", "cardinals", "usd-pen", "gallery", "flights"),
    "evening": ("cardinals", "weather", "gallery", "news", "usd-pen", "verse"),
    "night": ("verse", "gallery"),
}


@dataclass(frozen=True)
class Preferences:
    location: str = "Conway"
    latitude: float = 35.0887
    longitude: float = -92.4421
    timezone: str = "America/Chicago"
    photo_directory: Path | None = None
    playlists: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        if (
            not isinstance(self.location, str)
            or not 1 <= len(self.location.strip()) <= 24
        ):
            raise ValueError("location must be 1–24 characters")
        for key, bound in (("latitude", 90), ("longitude", 180)):
            value = getattr(self, key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or abs(value) > bound
            ):
                raise ValueError(f"Invalid {key}")
        try:
            if not isinstance(self.timezone, str):
                raise ValueError("timezone must be a name")  # noqa: TRY004
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Invalid timezone") from error
        if self.photo_directory is not None and not isinstance(
            self.photo_directory, Path
        ):
            raise ValueError("photo_directory must be a path")
        if (
            not isinstance(self.playlists, dict)
            or not self.playlists.keys() <= DEFAULT_PLAYLISTS.keys()
        ):
            raise ValueError("Unknown daypart")
        for values in self.playlists.values():
            if (
                not isinstance(values, (list, tuple))
                or not 1 <= len(values) <= 16
                or any(
                    not isinstance(p, str) or p not in PAGES or p == "alert"
                    for p in values
                )
                or len(set(values)) != len(values)
            ):
                raise ValueError(
                    "Playlists require unique known pages (1–16), excluding alerts"
                )
        object.__setattr__(
            self,
            "playlists",
            {
                key: tuple(self.playlists.get(key, default))
                for key, default in DEFAULT_PLAYLISTS.items()
            },
        )


def load_preferences(
    path: Path | None = None, *, panel_path: Path | None = None
) -> Preferences:
    explicit = path is not None
    if path is None:
        root = Path(os.environ.get("XDG_CONFIG_HOME", ""))
        if not root.is_absolute():
            root = Path.home() / ".config"
        path = (
            panel_path.parent / "dashboard.yaml"
            if panel_path
            else root / "paiper" / "dashboard.yaml"
        )
    if not explicit and not path.exists():
        return Preferences()
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError("Invalid dashboard YAML") from error
    if not isinstance(values, dict) or not values.keys() <= {
        "location",
        "latitude",
        "longitude",
        "timezone",
        "photo_directory",
        "playlists",
    }:
        raise ValueError("Unknown dashboard fields or invalid mapping")
    if "photo_directory" in values:
        if (
            not isinstance(values["photo_directory"], str)
            or not values["photo_directory"]
        ):
            raise ValueError("photo_directory must be a nonempty path string")
        photo = Path(values["photo_directory"]).expanduser()
        values["photo_directory"] = (
            photo if photo.is_absolute() else path.parent / photo
        )
    return Preferences(**values)
