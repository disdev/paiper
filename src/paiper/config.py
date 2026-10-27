"""Validated profiles for the E6 driver's fixed native buffer."""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import yaml


@dataclass(frozen=True)
class PanelProfile:
    """Rotation uses Pillow's counterclockwise convention; hardware is unverified."""

    rotation: int = 270
    confirmed: bool = False
    native_size: ClassVar[tuple[int, int]] = (400, 600)

    def __post_init__(self) -> None:
        if type(self.rotation) is not int or self.rotation not in (90, 270):
            raise ValueError("rotation must be the integer 90 or 270")
        if type(self.confirmed) is not bool:
            raise ValueError("confirmed must be a boolean")


def load_profile(path: Path | None) -> PanelProfile:
    """Load YAML with a required ``panel`` mapping, or defaults with no path."""
    if path is None:
        return PanelProfile()
    try:
        values = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError("Invalid panel profile YAML") from error
    if not isinstance(values, dict):
        raise ValueError("Panel profile must be a mapping")  # noqa: TRY004
    if not values.keys() <= {"panel"}:
        raise ValueError("Unknown configuration fields")
    panel = values.get("panel")
    if not isinstance(panel, dict):
        raise ValueError("panel must be a mapping")  # noqa: TRY004
    if not panel.keys() <= {"rotation", "confirmed"}:
        raise ValueError("Unknown panel profile fields")
    return PanelProfile(**panel)
