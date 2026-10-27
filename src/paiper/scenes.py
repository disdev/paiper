"""Offline scene content; timestamps describe image generation, not a live clock."""

from datetime import datetime
from zoneinfo import ZoneInfo

VERSE = "Be still, and know that I am God."
SCENES = ("stillness", "calibration", "dashboard")


def scene_context(scene: str, now: datetime) -> dict[str, str]:
    """Validate a scene and format its generation time in America/Chicago."""
    if scene not in SCENES:
        raise ValueError(f"Unknown scene: {scene!r}")
    if scene == "dashboard":
        raise ValueError("Dashboard requires a collected Page; use render_dashboard")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Image generation time must be timezone-aware")
    local = now.astimezone(ZoneInfo("America/Chicago"))
    return {
        "scene": scene,
        "updated": local.strftime("%H:%M %Z"),
        "date": local.strftime("%d %b %Y"),
        "verse": VERSE,
    }
