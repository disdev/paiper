import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

from paiper import cli
from paiper.display import DriverUnavailableError
from paiper.render import RenderError
from paiper.state import DisplayState, StateLockedError


def forbidden(*args, **kwargs):
    pytest.fail("Unexpected rendering or hardware access")


def profile(tmp_path, *, rotation=270, confirmed=False):
    path = tmp_path / "config.yaml"
    path.write_text(
        f"panel:\n  rotation: {rotation}\n  confirmed: {str(confirmed).lower()}\n"
    )
    return path


@pytest.mark.parametrize("scene", ["stillness", "calibration"])
def test_preview_writes_real_png_without_hardware(tmp_path, monkeypatch, capsys, scene):
    monkeypatch.setattr(cli, "render_and_display", forbidden)
    monkeypatch.setattr(cli, "DisplayState", forbidden)
    output = tmp_path / "nested" / f"{scene}.png"
    assert cli.main(["preview", "--scene", scene, "--output", str(output)]) == 0
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.size == (600, 400)
    assert str(output) in capsys.readouterr().out
    assert "waveshare_epd.epd4in0e" not in sys.modules


@pytest.mark.parametrize(("rotation", "corner"), [(270, (399, 0)), (90, (0, 599))])
def test_native_preview_uses_unconfirmed_profile_rotation(
    tmp_path, monkeypatch, rotation, corner
):
    image = Image.new("RGB", (600, 400), "white")
    image.putpixel((0, 0), (255, 0, 0))
    monkeypatch.setattr(cli, "render_scene", lambda *_: image)
    monkeypatch.setattr(cli, "render_and_display", forbidden)
    monkeypatch.setattr(cli, "DisplayState", forbidden)
    output = tmp_path / "native.png"
    assert (
        cli.main(
            [
                "preview",
                "--scene",
                "calibration",
                "--output",
                str(output),
                "--config",
                str(profile(tmp_path, rotation=rotation)),
                "--native",
            ]
        )
        == 0
    )
    with Image.open(output) as saved:
        assert saved.size == (400, 600)
        assert saved.getpixel(corner) == (255, 0, 0)


def test_missing_chromium_has_actionable_error(tmp_path, monkeypatch, caplog):
    def fail(*_):
        raise RenderError("Chromium render failed: Executable doesn't exist")

    monkeypatch.setattr(cli, "render_scene", fail)
    output = tmp_path / "missing.png"
    with caplog.at_level(logging.ERROR):
        result = cli.main(["preview", "--scene", "stillness", "--output", str(output)])
    assert result != 0
    assert "python -m playwright install chromium" in caplog.text
    assert not output.exists()


def test_unknown_scene_is_rejected_before_rendering(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "render_scene", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(["preview", "--scene", "news", "--output", str(tmp_path / "x.png")])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("contents", ["panel: [", "panel:\n  rotation: 180\n"])
def test_malformed_configuration_prevents_rendering(
    tmp_path, monkeypatch, caplog, contents
):
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    monkeypatch.setattr(cli, "render_scene", forbidden)
    assert (
        cli.main(
            [
                "preview",
                "--scene",
                "stillness",
                "--output",
                str(tmp_path / "x.png"),
                "--config",
                str(path),
            ]
        )
        != 0
    )
    assert caplog.records


def test_unconfirmed_display_refused_before_browser_or_state(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(cli, "render_scene", forbidden)
    monkeypatch.setattr(cli, "render_and_display", forbidden)
    state = tmp_path / "state.sqlite3"
    assert (
        cli.main(
            [
                "display",
                "--scene",
                "calibration",
                "--config",
                str(profile(tmp_path)),
                "--state",
                str(state),
            ]
        )
        != 0
    )
    assert "confirm" in caplog.text.lower()
    assert not state.exists()
    assert not state.with_name(state.name + ".lock").exists()


def test_competing_display_never_enters_renderer(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli, "render_scene", forbidden)
    path = tmp_path / "state.sqlite3"
    args = [
        "display",
        "--scene",
        "calibration",
        "--config",
        str(profile(tmp_path, confirmed=True)),
        "--state",
        str(path),
    ]
    with DisplayState(path) as state:
        before = state.status()
        assert cli.main(args) == 1
        assert state.status() == before
    assert "locked" in caplog.text


@pytest.mark.parametrize("existing_attempt", [False, True])
@pytest.mark.parametrize("error", [RenderError("render failed"), KeyboardInterrupt()])
def test_display_render_failure_releases_lock_without_recording_attempt(
    tmp_path, monkeypatch, caplog, existing_attempt, error
):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        if existing_attempt:
            state.reserve(1000, "previous")
            state.succeed(1100, "previous")
        before = state.status()

    def fail(*_):
        with pytest.raises(StateLockedError), DisplayState(path):
            pass
        raise error

    monkeypatch.setattr(cli, "render_scene", fail)
    args = [
        "display",
        "--scene",
        "stillness",
        "--config",
        str(profile(tmp_path, confirmed=True)),
        "--state",
        str(path),
    ]
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            cli.main(args)
    else:
        assert cli.main(args) == 1
        assert "render failed" in caplog.text
    with DisplayState(path) as state:
        assert state.status() == before


@pytest.mark.parametrize("displayed", [False, True])
def test_display_passes_shared_state_and_distinguishes_skips(
    tmp_path, monkeypatch, capsys, displayed
):
    image = Image.new("RGB", (600, 400), "white")
    state = tmp_path / "shared.sqlite3"
    monkeypatch.setattr(cli, "render_scene", lambda *_: image)

    def display(image_factory, selected, state_path):
        assert image_factory() is image
        assert selected.confirmed
        assert state_path == state
        return displayed

    monkeypatch.setattr(cli, "render_and_display", display)
    assert (
        cli.main(
            [
                "display",
                "--scene",
                "stillness",
                "--config",
                str(profile(tmp_path, confirmed=True)),
                "--state",
                str(state),
            ]
        )
        == 0
    )
    message = capsys.readouterr().out.lower()
    assert ("displayed" if displayed else "skipped") in message
    if not displayed:
        assert "displayed" not in message


def test_missing_vendor_error_is_nonzero_and_actionable(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli, "render_scene", lambda *_: Image.new("RGB", (600, 400)))

    def fail(*_):
        raise DriverUnavailableError("Missing waveshare_epd.epd4in0e")

    monkeypatch.setattr(cli, "render_and_display", fail)
    assert (
        cli.main(
            [
                "display",
                "--scene",
                "calibration",
                "--config",
                str(profile(tmp_path, confirmed=True)),
                "--state",
                str(tmp_path / "state"),
            ]
        )
        != 0
    )
    assert "PYTHONPATH" in caplog.text
    assert "epd4in0e" in caplog.text


@pytest.mark.parametrize("xdg", [None, "state-data", "absolute"])
def test_default_state_is_persistent_and_independent_of_cwd(
    tmp_path, monkeypatch, capsys, xdg
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    expected = tmp_path / ".local" / "state" / "paiper" / "display.sqlite3"
    if xdg is not None:
        value = str(tmp_path / "xdg") if xdg == "absolute" else xdg
        monkeypatch.setenv("XDG_STATE_HOME", value)
        if xdg == "absolute":
            expected = Path(value) / "paiper" / "display.sqlite3"
    monkeypatch.setattr(cli, "render_scene", forbidden)
    for directory in (tmp_path / "one", tmp_path / "two"):
        directory.mkdir()
        monkeypatch.chdir(directory)
        assert cli.main(["status"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["state_path"] == str(expected)
        assert result["in_progress"] == 0
    assert expected.is_file()


def test_status_reports_persisted_incomplete_attempt_without_rendering(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "state.sqlite3"
    with DisplayState(path) as state:
        state.reserve(1000, "digest")
        state.begin_attempt()
    monkeypatch.setattr(cli, "render_scene", forbidden)
    monkeypatch.setattr(cli, "render_and_display", forbidden)
    assert cli.main(["status", "--state", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["last_attempt_at"] == 1000
    assert result["last_success_at"] is None
    assert result["in_progress"] == 1


def test_corrupt_state_fails_without_resetting(tmp_path, caplog):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not sqlite")
    assert cli.main(["status", "--state", str(path)]) != 0
    assert path.read_bytes() == b"not sqlite"
    assert "state" in caplog.text.lower()


def test_module_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "paiper", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert all(command in result.stdout for command in ("preview", "display", "status"))
