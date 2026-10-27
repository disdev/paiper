import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jinja2 import UndefinedError
from PIL import Image
from playwright.sync_api import Route

from paiper import render, scenes
from paiper.panel import RGB_TO_CODE
from paiper.render import RenderError, render_html, render_scene, save_preview

NOW = datetime(2026, 10, 26, 14, tzinfo=UTC)


def test_offline_scene_does_not_invent_live_data():
    html = render_html("stillness", NOW)
    assert "OFFLINE" in html
    assert "World English Bible" in html
    assert "Be still, and know that I am God." in html
    assert "Psalm 46:10" in html
    assert "Excerpt" in html
    assert "Updated" in html and "09:00" in html and "CDT" in html
    assert "Live sources are not connected" in html
    assert "https://" not in html
    assert "data:font/woff2;base64," in html


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-11-01T06:30:00+00:00", "01:30 CDT"),
        ("2026-11-01T07:30:00+00:00", "01:30 CST"),
        ("2026-03-08T07:30:00+00:00", "01:30 CST"),
        ("2026-03-08T08:30:00+00:00", "03:30 CDT"),
    ],
)
def test_updated_time_tracks_chicago_dst(instant, expected):
    assert expected in render_html("stillness", datetime.fromisoformat(instant))


@pytest.mark.parametrize("renderer", [render_html, render_scene])
def test_unknown_scene_is_rejected(renderer):
    with pytest.raises(ValueError, match="Unknown scene"):
        renderer("../base", NOW)


@pytest.mark.parametrize("renderer", [render_html, render_scene])
def test_naive_datetime_is_rejected(renderer):
    with pytest.raises(ValueError, match="aware"):
        renderer("stillness", datetime(2026, 10, 26))


def test_content_is_escaped_by_actual_scene_environment(monkeypatch):
    monkeypatch.setattr(scenes, "VERSE", '<script>alert("remote")</script>')
    html = render_html("stillness", NOW)
    assert "&lt;script&gt;alert(&#34;remote&#34;)&lt;/script&gt;" in html
    assert '<script>alert("remote")</script>' not in html
    with pytest.raises(UndefinedError):
        render._environment().from_string("{{ missing }}").render()


@pytest.mark.parametrize("scene", ["stillness", "calibration"])
def test_real_chromium_frame(scene, monkeypatch):
    check_layout = render._check_layout

    def check_readable_layout(page):
        check_layout(page)
        metadata = (
            ".edition, .updated, .section-label, .attribution span, .context-rail span"
            if scene == "stillness"
            else ".corner, .calibration-title > span, .swatch span, "
            ".size-label, .sample-small, .calibration-note"
        )
        sizes = page.locator(metadata).evaluate_all(
            "elements => elements.map(el => parseFloat(getComputedStyle(el).fontSize))"
        )
        assert sizes and all(18 <= size <= 20 for size in sizes)
        if scene == "stillness":
            assert (
                page.locator("blockquote").evaluate(
                    "el => getComputedStyle(el).fontSize"
                )
                == "48px"
            )
            assert page.locator(".masthead, .hero, .context-rail").evaluate_all(
                "elements => elements.map(el => el.getBoundingClientRect().height)"
            ) == [48, 304, 48]
        else:
            for selector, size in (
                (".sample-small", 18),
                (".sample-body", 26),
                (".sample-serif", 40),
            ):
                sample = page.locator(selector)
                assert (
                    sample.evaluate("el => getComputedStyle(el).fontSize")
                    == f"{size}px"
                )
                assert sample.locator("..").locator(".size-label").inner_text() == (
                    f"{size} px"
                )

    monkeypatch.setattr(render, "_check_layout", check_readable_layout)
    frame = render_scene(scene, NOW)
    assert frame.mode == "RGB"
    assert frame.size == (600, 400)
    colors = {color for _, color in frame.getcolors(maxcolors=240000)}
    assert colors <= set(RGB_TO_CODE)
    assert {(0, 0, 0), (255, 255, 255), (255, 0, 0)} <= colors
    if scene == "calibration":
        assert colors == set(RGB_TO_CODE)


def test_calibration_has_named_corners_swatches_and_type_sizes():
    html = render_html("calibration", NOW)
    for label in (
        "TL",
        "TR",
        "BL",
        "BR",
        "Black",
        "White",
        "Yellow",
        "Red",
        "Blue",
        "Green",
        "18 px",
        "26 px",
        "40 px",
    ):
        assert label in html


@pytest.mark.parametrize(
    "body",
    [
        '<div style="width:601px;height:400px"></div>',
        '<div data-fit style="width:40px;height:20px">unbreakableoverflow</div>',
        '<div data-fit style="position:absolute;left:-10px">outside</div>',
        '<div data-fit style="height:10px;width:100px">wrapped text overflows</div>',
    ],
)
def test_real_browser_rejects_overflow(monkeypatch, body):
    monkeypatch.setattr(
        render,
        "render_html",
        lambda *_: "<style>html,body{margin:0;width:600px;height:400px}</style>" + body,
    )
    with pytest.raises(RenderError, match="overflow"):
        render_scene("stillness", NOW)


def test_real_browser_aborts_network_requests(monkeypatch):
    attempted = []
    abort = Route.abort

    def record_abort(route, *args, **kwargs):
        attempted.append(route.request.url)
        return abort(route, *args, **kwargs)

    monkeypatch.setattr(Route, "abort", record_abort)
    monkeypatch.setattr(
        render,
        "render_html",
        lambda *_: (
            "<style>html,body{margin:0;width:600px;height:400px}</style>"
            '<img src="https://example.invalid/never-fetch.png" width="20" height="20">'
        ),
    )
    assert render_scene("stillness", NOW).size == (600, 400)
    assert attempted == ["https://example.invalid/never-fetch.png"]


@pytest.mark.parametrize("size", [(600, 400), (400, 600)])
def test_save_preview_writes_png_and_replaces_atomically(tmp_path, monkeypatch, size):
    path = tmp_path / "preview.png"
    path.write_bytes(b"previous")
    image = Image.new("RGB", size, "red")
    replace = render.os.replace

    def check_replace(source, destination):
        assert path.read_bytes() == b"previous"
        assert Path(source).parent == path.parent
        with Image.open(source) as staged:
            assert staged.format == "PNG"
            assert staged.getpixel((0, 0)) == (255, 0, 0)
        replace(source, destination)

    monkeypatch.setattr(render.os, "replace", check_replace)
    save_preview(image, path)
    with Image.open(path) as saved:
        assert saved.format == "PNG"
        assert saved.mode == "RGB" and saved.size == size
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "image",
    [
        Image.new("RGB", (1, 1)),
        Image.new("RGBA", (600, 400)),
        Image.new("RGB", (600, 400), (1, 2, 3)),
        Image.new("RGBA", (400, 600)),
        Image.new("RGB", (400, 600), (1, 2, 3)),
    ],
)
def test_save_rejects_invalid_frame_without_touching_existing(tmp_path, image):
    path = tmp_path / "preview.png"
    path.write_bytes(b"previous")
    with pytest.raises(ValueError):
        save_preview(image, path)
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_failed_atomic_replace_preserves_previous_and_cleans_staging(
    tmp_path, monkeypatch
):
    path = tmp_path / "preview.png"
    path.write_bytes(b"previous")

    def fail(*_):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(render.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        save_preview(Image.new("RGB", (600, 400), "white"), path)
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_installed_wheel_has_templates_and_fonts_and_renders(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wheel_dir = tmp_path / "wheels"
    target = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--quiet",
            "--wheel-dir",
            str(wheel_dir),
            str(root),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--quiet",
            "--target",
            str(target),
            str(next(wheel_dir.glob("paiper-*.whl"))),
        ],
        check=True,
    )
    code = f"""
import sys
sys.path.insert(0, {str(target)!r})
from importlib.resources import files
from datetime import datetime, timezone
import paiper
from paiper.render import render_scene
assert str(paiper.__file__).startswith({str(target)!r})
package = files("paiper")
for name in ("base.html", "stillness.html", "calibration.html", "paper.css"):
    assert package.joinpath("templates", name).is_file(), name
for name in ("README.md", "newsreader-latin.woff2", "Newsreader-OFL.txt",
             "public-sans-latin.woff2", "PublicSans-OFL.txt"):
    assert package.joinpath("assets", name).is_file(), name
assert render_scene("calibration", datetime.now(timezone.utc)).size == (600, 400)
"""
    subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, check=True)
