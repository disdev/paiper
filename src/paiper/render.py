"""Self-contained HTML → Chromium → six-color logical frame, without network I/O."""

import base64
import os
from datetime import datetime
from importlib.resources import files
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from paiper.panel import RGB_TO_CODE, quantize_photo, quantize_ui
from paiper.scenes import scene_context


class RenderError(RuntimeError):
    """A frame could not be rendered completely and safely."""


def _environment() -> Environment:
    return Environment(
        loader=PackageLoader("paiper", "templates"),
        autoescape=select_autoescape(("html", "xml"), default_for_string=True),
        undefined=StrictUndefined,
    )


def render_html(scene: str, now: datetime) -> str:
    """Return self-contained HTML for an aware image-generation timestamp."""
    context = scene_context(scene, now)
    package = files("paiper")
    return (
        _environment()
        .get_template(f"{scene}.html")
        .render(
            **context,
            **_fonts(),
            css=package.joinpath("templates", "paper.css").read_text(encoding="utf-8"),
        )
    )


def _fonts() -> dict[str, str]:
    package = files("paiper")
    return {
        key: base64.b64encode(package.joinpath("assets", filename).read_bytes()).decode(
            "ascii"
        )
        for key, filename in (
            ("serif_font", "newsreader-latin.woff2"),
            ("sans_font", "public-sans-latin.woff2"),
        )
    }


def render_dashboard_html(context, now: datetime, timezone: str) -> str:
    """Render an already collected typed Page; never open a cache/provider here."""
    from zoneinfo import ZoneInfo

    from paiper.dashboard import stamp

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Image generation time must be aware")
    photo = base64.b64encode(context.photo).decode("ascii") if context.photo else None
    return (
        _environment()
        .get_template("dashboard.html")
        .render(
            page=context,
            generated=stamp(now, timezone),
            clock=now.astimezone(ZoneInfo(timezone)).strftime("%H:%M %Z"),
            photo=photo,
            **_fonts(),
            css=files("paiper").joinpath("templates", "dashboard.css").read_text(),
        )
    )


def render_dashboard(
    context, now: datetime, timezone: str = "America/Chicago"
) -> Image.Image:
    from paiper.dashboard import PHOTO_BOX

    return _render_frame(
        render_dashboard_html(context, now, timezone),
        PHOTO_BOX if context.photo else None,
    )


def _check_layout(page) -> None:
    failures = page.evaluate("""() => {
        const problems = [];
        const root = document.documentElement;
        if (root.clientWidth !== 600 || root.clientHeight !== 400 ||
            root.scrollWidth !== 600 || root.scrollHeight !== 400)
            problems.push("document dimensions");
        for (const el of document.querySelectorAll("body, [data-fit]")) {
            const box = el.getBoundingClientRect();
            const label = el.id || el.className || el.tagName;
            if (el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight)
                problems.push(`${label}: content overflow`);
            if (box.left < -0.5 || box.top < -0.5 ||
                box.right > 600.5 || box.bottom > 400.5)
                problems.push(`${label}: outside frame`);
            const parent = el.parentElement?.closest("[data-fit]");
            if (parent) {
                const bounds = parent.getBoundingClientRect();
                if (box.left < bounds.left - 0.5 || box.top < bounds.top - 0.5 ||
                    box.right > bounds.right + 0.5 || box.bottom > bounds.bottom + 0.5)
                    problems.push(`${label}: outside content region`);
            }
        }
        return problems;
    }""")
    if failures:
        raise RenderError("Layout overflow: " + "; ".join(failures))


def _fit_editorial(page) -> None:
    """Measure actual fonts; editorial ellipses reduce headlines, never clip text."""
    page.evaluate("""() => {
        for (const el of document.querySelectorAll('[data-editorial-lines]')) {
            const limit = Number(el.dataset.editorialLines) *
                parseFloat(getComputedStyle(el).lineHeight);
            const original = el.textContent.trim();
            let length = original.length;
            while (el.getBoundingClientRect().height > limit + 0.5 && length > 1) {
                length--;
                el.textContent = original.slice(0, length).trimEnd() + '…';
            }
        }
    }""")


def render_scene(scene: str, now: datetime) -> Image.Image:
    """Render an RGB 600×400 six-color frame; fail rather than silently clip."""
    return _render_frame(render_html(scene, now))


def _render_frame(html: str, photo_box=None) -> Image.Image:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={"width": 600, "height": 400},
                    device_scale_factor=1,
                    java_script_enabled=False,
                    service_workers="block",
                )
                page.route("**/*", lambda route: route.abort())
                page.set_content(html, wait_until="load")
                page.evaluate("document.fonts.ready")
                if not page.evaluate(
                    "() => [...document.fonts].every(font => font.status === 'loaded')"
                ):
                    raise RenderError("Packaged fonts failed to load")
                _fit_editorial(page)
                _check_layout(page)
                screenshot = page.screenshot(type="png")
            finally:
                browser.close()
    except PlaywrightError as error:
        raise RenderError(f"Chromium render failed: {error}") from error
    with Image.open(BytesIO(screenshot)) as image:
        frame = quantize_ui(image)
        if photo_box:
            frame.paste(quantize_photo(image.crop(photo_box)), photo_box)
        return frame


def save_preview(image: Image.Image, path: Path) -> None:
    """Atomically save a palette-validated logical or native RGB PNG."""
    if image.size not in ((600, 400), (400, 600)) or image.mode != "RGB":
        raise ValueError(
            "Preview must be an RGB 600x400 logical or 400x600 native frame"
        )
    colors = image.getcolors(maxcolors=6)
    if colors is None or any(color not in RGB_TO_CODE for _, color in colors):
        raise ValueError("Preview contains a non-palette pixel")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with staging.open("xb") as output:
            image.save(output, format="PNG")
            output.flush()
            os.fsync(output.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)
