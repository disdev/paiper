import hashlib
from types import SimpleNamespace

import pytest
from PIL import Image

from paiper import cli, display
from paiper.config import PanelProfile
from paiper.state import DisplayState, IncompleteAttemptError, StateLockedError


class FakeDriver:
    def __init__(self, *, result=0, failures=None, callback=None):
        self.result = result
        self.failures = failures or {}
        self.callback = callback
        self.calls = []
        self.payload = None

    def step(self, name):
        self.calls.append(name)
        if self.callback:
            self.callback(name)
        if name in self.failures:
            raise self.failures[name]

    def init(self):
        self.step("init")
        return self.result

    def display(self, payload):
        self.payload = payload
        self.step("display")

    def sleep(self):
        self.step("sleep")

    def close(self):
        self.step("close")


def test_send_frame_has_ordered_lifecycle_and_passes_bytes_unchanged():
    driver = FakeDriver()
    display.send_frame(driver, b"\x56\x11")
    assert driver.calls == ["init", "display", "sleep", "close"]
    assert driver.payload == b"\x56\x11"


@pytest.mark.parametrize("result", [-1, 1, None, False, "0"])
def test_invalid_init_result_never_displays(result):
    driver = FakeDriver(result=result)
    with pytest.raises(display.DriverInitializationError):
        display.send_frame(driver, b"\x11")
    assert "display" not in driver.calls
    assert driver.calls[-1] == "close"


@pytest.mark.parametrize("stage", ["init", "display"])
def test_primary_failure_survives_cleanup_failures(stage, caplog):
    primary = OSError("original failure")
    driver = FakeDriver(
        failures={
            stage: primary,
            "sleep": RuntimeError("sleep failed"),
            "close": RuntimeError("close failed"),
        }
    )
    with pytest.raises(OSError) as caught:
        display.send_frame(driver, b"\x11")
    assert caught.value is primary
    assert driver.calls[-2:] == ["sleep", "close"]
    assert "sleep failed" in caplog.text
    assert "close failed" in caplog.text


def test_successful_sleep_does_not_swallow_failed_refresh():
    error = OSError("refresh failed")
    driver = FakeDriver(failures={"display": error})
    with pytest.raises(OSError) as caught:
        display.send_frame(driver, b"\x11")
    assert caught.value is error
    assert driver.calls == ["init", "display", "sleep", "close"]


@pytest.mark.parametrize("stage", ["sleep", "close"])
def test_cleanup_only_failure_is_not_success(stage):
    error = OSError(f"{stage} failed")
    driver = FakeDriver(failures={stage: error})
    with pytest.raises(OSError) as caught:
        display.send_frame(driver, b"\x11")
    assert caught.value is error
    assert driver.calls[-1] == "close"


@pytest.fixture
def clock(monkeypatch):
    values = SimpleNamespace(wall=1000.0, monotonic=10.0)
    monkeypatch.setattr(display.time, "time", lambda: values.wall)
    monkeypatch.setattr(display.time, "monotonic", lambda: values.monotonic)
    return values


def test_unconfirmed_profile_does_not_construct_driver_or_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        display, "WaveshareDriver", lambda: pytest.fail("hardware touched")
    )
    path = tmp_path / "state.sqlite3"
    with pytest.raises(ValueError, match="confirmed"):
        display.display_frame(Image.new("RGB", (600, 400)), PanelProfile(), path)
    assert not path.exists()


@pytest.mark.parametrize("hardware_failure", [False, True])
def test_cli_lock_spans_render_encoding_hardware_and_durable_completion(
    tmp_path, monkeypatch, clock, hardware_failure
):
    path = tmp_path / "state.sqlite3"
    config = tmp_path / "config.yaml"
    config.write_text("panel:\n  confirmed: true\n")
    stages = []

    def check_lock(stage):
        with pytest.raises(StateLockedError), DisplayState(path):
            pass
        stages.append(stage)

    def render(*_):
        check_lock("render")
        clock.wall += 500
        clock.monotonic += 500
        return Image.new("RGB", (600, 400), "white")

    def checked(stage, operation):
        def run(*args, **kwargs):
            check_lock(stage)
            result = operation(*args, **kwargs)
            check_lock(f"{stage}:done")
            return result

        return run

    monkeypatch.setattr(cli, "render_scene", render)
    for name in ("quantize_ui", "prepare_native", "encode_native"):
        monkeypatch.setattr(display, name, checked(name, getattr(display, name)))
    for name in ("reserve", "begin_attempt", "succeed", "finish_attempt"):
        monkeypatch.setattr(
            DisplayState, name, checked(name, getattr(DisplayState, name))
        )
    driver = FakeDriver(
        failures={"display": OSError("hardware failed")} if hardware_failure else {},
        callback=check_lock,
    )
    monkeypatch.setattr(
        display, "WaveshareDriver", checked("construct", lambda: driver)
    )
    assert cli.main(
        [
            "display", "--scene", "stillness", "--config", str(config),
            "--state", str(path),
        ]
    ) == (1 if hardware_failure else 0)
    completion = "finish_attempt" if hardware_failure else "succeed"
    assert stages == [
        "render", "quantize_ui", "quantize_ui:done",
        "prepare_native", "prepare_native:done", "encode_native", "encode_native:done",
        "reserve", "reserve:done", "begin_attempt", "begin_attempt:done",
        "construct", "construct:done", "init", "display", "sleep", "close",
        completion, f"{completion}:done",
    ]
    with DisplayState(path) as state:
        row = state.status()
        assert row["last_attempt_at"] == 1500
        assert row["cooldown_at"] == 1500
        assert row["last_success_at"] == (None if hardware_failure else 1500)
        assert row["in_progress"] == 0


def test_full_frame_native_hash_and_persistent_skips(tmp_path, monkeypatch, clock):
    path = tmp_path / "state.sqlite3"
    profile = PanelProfile(confirmed=True)
    driver = FakeDriver()
    monkeypatch.setattr(display, "WaveshareDriver", lambda: driver)
    white = Image.new("RGB", (600, 400), "white")
    assert display.display_frame(white, profile, path)
    assert driver.payload == b"\x11" * 120000
    with DisplayState(path) as state:
        assert (
            state.status()["last_success_digest"]
            == hashlib.sha256(b"\x11" * 120000).hexdigest()
        )
    monkeypatch.setattr(
        display, "WaveshareDriver", lambda: pytest.fail("skip touched hardware")
    )
    clock.wall = 1300
    assert not display.display_frame(white, profile, path)
    # Use a fresh clock-consistent state to exercise cooldown independently.
    other = tmp_path / "cooldown.sqlite3"
    with DisplayState(other) as state:
        state.reserve(1200, "previous")
    assert not display.display_frame(white, profile, other)


@pytest.mark.parametrize("stage", [None, "init", "display", "sleep", "close"])
@pytest.mark.parametrize("backwards", [False, True])
def test_slow_lifecycle_extends_cooldown_and_lock_spans_cleanup(
    tmp_path, monkeypatch, clock, stage, backwards
):
    path = tmp_path / "state.sqlite3"
    error = OSError("hardware error")

    def advance_and_check_lock(name):
        with pytest.raises(StateLockedError), DisplayState(path):
            pass
        clock.monotonic += 200
        clock.wall += -200 if backwards else 200

    driver = FakeDriver(
        failures={stage: error} if stage else {}, callback=advance_and_check_lock
    )
    monkeypatch.setattr(display, "WaveshareDriver", lambda: driver)
    image = Image.new("RGB", (600, 400), "white")
    if stage:
        with pytest.raises(OSError) as caught:
            display.display_frame(image, PanelProfile(confirmed=True), path)
        assert caught.value is error
    else:
        assert display.display_frame(image, PanelProfile(confirmed=True), path)
    end = 1000 + 200 * len(driver.calls)
    with DisplayState(path) as state:
        assert state.status()["last_success_at"] == (None if stage else end)
        assert not state.reserve(end + 299, "next")
        assert state.reserve(end + 300, "next")


def install_vendor(monkeypatch, *, size=(400, 600), optional_cleanup=False):
    calls = []

    def close():
        calls.append(("close",))

    def close_optional(cleanup=False):
        calls.append(("close", cleanup))

    config = SimpleNamespace(
        digital_read=lambda pin: 1,
        delay_ms=lambda delay: calls.append(("delay", delay)),
        module_exit=close_optional if optional_cleanup else close,
    )

    class EPD:
        def __init__(self):
            self.width, self.height = size
            self.busy_pin = 24

        def init(self):
            calls.append(("init",))
            self.ReadBusyH()
            return 0

        def display(self, payload):
            calls.append(("display", payload))

        def sleep(self):
            calls.append(("sleep",))
            config.module_exit()

    vendor = SimpleNamespace(EPD=EPD, EPD_WIDTH=400, EPD_HEIGHT=600, epdconfig=config)
    monkeypatch.setattr(display.importlib, "import_module", lambda name: vendor)
    return vendor, config, calls


def test_vendor_import_is_lazy_and_missing_driver_error_is_explicit(monkeypatch):
    imports = []

    def missing(name):
        imports.append(name)
        raise ModuleNotFoundError("no waveshare")

    monkeypatch.setattr(display.importlib, "import_module", missing)
    driver = display.WaveshareDriver()
    assert imports == []
    with pytest.raises(display.DriverUnavailableError, match="waveshare_epd"):
        display.send_frame(driver, b"\x11")
    assert imports == ["waveshare_epd.epd4in0e"]


@pytest.mark.parametrize("optional_cleanup", [False, True])
def test_adapter_uses_vendor_sequence_bytes_and_cleanup_signature(
    monkeypatch, optional_cleanup
):
    _, _, calls = install_vendor(monkeypatch, optional_cleanup=optional_cleanup)
    display.send_frame(display.WaveshareDriver(), b"\x56\x11")
    assert ("init",) in calls
    assert ("display", b"\x56\x11") in calls
    assert ("delay", 200) in calls
    assert ("sleep",) in calls
    assert calls[-1] == (("close", False) if optional_cleanup else ("close",))
    assert sum(call[0] == "close" for call in calls) == 1


def test_vendor_busy_timeout_is_bounded_and_closes(monkeypatch, clock):
    _, config, calls = install_vendor(monkeypatch)
    config.digital_read = lambda pin: 0

    def delay(ms):
        calls.append(("delay", ms))
        clock.monotonic += ms / 1000

    config.delay_ms = delay
    with pytest.raises(TimeoutError, match="BUSY"):
        display.send_frame(display.WaveshareDriver(busy_timeout=0.02), b"\x11")
    assert 10.02 <= clock.monotonic < 10.03
    assert not any(call[0] == "display" for call in calls)
    assert calls[-1] == ("close",)


@pytest.mark.parametrize("size", [(600, 400), (400, 300)])
def test_vendor_dimension_mismatch_rejects_before_init(monkeypatch, size):
    _, _, calls = install_vendor(monkeypatch, size=size)
    with pytest.raises(display.DriverInitializationError, match="400.*600"):
        display.send_frame(display.WaveshareDriver(), b"\x11")
    assert ("init",) not in calls
    assert calls[-1] == ("close",)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_invalid_busy_timeout_does_not_import_vendor(monkeypatch, timeout):
    monkeypatch.setattr(
        display.importlib, "import_module", lambda name: pytest.fail("vendor import")
    )
    with pytest.raises(ValueError):
        display.WaveshareDriver(busy_timeout=timeout)


def test_failed_success_commit_keeps_interrupted_guard(tmp_path, monkeypatch, clock):
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(display, "WaveshareDriver", FakeDriver)
    persistence_error = OSError("disk full")

    def fail_commit(self, now, digest):
        raise persistence_error

    monkeypatch.setattr(DisplayState, "succeed", fail_commit)
    with pytest.raises(OSError) as caught:
        display.display_frame(
            Image.new("RGB", (600, 400)), PanelProfile(confirmed=True), path
        )
    assert caught.value is persistence_error
    with DisplayState(path) as state:
        assert state.status()["last_success_at"] is None
        with pytest.raises(IncompleteAttemptError):
            state.reserve(2000, "another")


def test_failed_failure_commit_preserves_hardware_error(
    tmp_path, monkeypatch, clock, caplog
):
    path = tmp_path / "state.sqlite3"
    hardware_error = OSError("refresh failed")
    driver = FakeDriver(failures={"display": hardware_error})
    monkeypatch.setattr(display, "WaveshareDriver", lambda: driver)

    def fail_commit(self, now):
        raise OSError("disk full")

    monkeypatch.setattr(DisplayState, "finish_attempt", fail_commit)
    with pytest.raises(OSError) as caught:
        display.display_frame(
            Image.new("RGB", (600, 400)), PanelProfile(confirmed=True), path
        )
    assert caught.value is hardware_error
    assert "disk full" in caplog.text
    with DisplayState(path) as state:
        assert state.status()["in_progress"]
        assert state.status()["last_success_at"] is None


def test_driver_construction_is_inside_persistent_reservation(
    tmp_path, monkeypatch, clock
):
    path = tmp_path / "state.sqlite3"
    error = OSError("construction failed")

    def broken_driver():
        with pytest.raises(StateLockedError), DisplayState(path):
            pass
        clock.monotonic += 400
        clock.wall += 400
        raise error

    monkeypatch.setattr(display, "WaveshareDriver", broken_driver)
    with pytest.raises(OSError) as caught:
        display.display_frame(
            Image.new("RGB", (600, 400)), PanelProfile(confirmed=True), path
        )
    assert caught.value is error
    with DisplayState(path) as state:
        assert not state.reserve(1699, "next")
        assert state.reserve(1700, "next")


def test_adapter_cleanup_typeerror_is_not_retried_or_masked(monkeypatch, caplog):
    _, config, calls = install_vendor(monkeypatch)
    error = TypeError("internal cleanup failure")

    def broken_cleanup(cleanup=False):
        calls.append(("close", cleanup))
        raise error

    config.module_exit = broken_cleanup
    with pytest.raises(TypeError) as caught:
        display.send_frame(display.WaveshareDriver(), b"\x11")
    assert caught.value is error
    assert [call for call in calls if call[0] == "close"] == [
        ("close", False),
        ("close", False),
    ]
    assert "internal cleanup failure" in caplog.text


def test_vendor_module_dimensions_are_checked_before_constructing_epd(monkeypatch):
    vendor, _, calls = install_vendor(monkeypatch)
    vendor.EPD_WIDTH = 600
    vendor.EPD = lambda: pytest.fail("constructed mismatched vendor driver")
    with pytest.raises(display.DriverInitializationError, match="400.*600"):
        display.send_frame(display.WaveshareDriver(), b"\x11")
    assert calls == [("close",)]


@pytest.mark.parametrize("result", [-1, None, False])
def test_vendor_failed_init_does_not_send_sleep_commands(monkeypatch, result):
    vendor, _, calls = install_vendor(monkeypatch)
    vendor.EPD.init = lambda self: result
    with pytest.raises(display.DriverInitializationError):
        display.send_frame(display.WaveshareDriver(), b"\x11")
    assert calls == [("close",)]


def test_display_frame_rotates_and_quantizes_before_sending(
    tmp_path, monkeypatch, clock
):
    driver = FakeDriver()
    monkeypatch.setattr(display, "WaveshareDriver", lambda: driver)
    image = Image.new("RGB", (600, 400), (254, 254, 254))
    image.putpixel((0, 0), (255, 0, 0))
    assert display.display_frame(
        image, PanelProfile(confirmed=True), tmp_path / "state.sqlite3"
    )
    # Clockwise rotation sends the top-left red pixel to native (399, 0).
    assert driver.payload == b"\x11" * 199 + b"\x13" + b"\x11" * 119800


def test_no_argument_vendor_cleanup_is_not_repeated_after_successful_sleep(monkeypatch):
    _, config, calls = install_vendor(monkeypatch)

    def close_once():
        if ("close",) in calls:
            raise RuntimeError("GPIO pins were already released")
        calls.append(("close",))

    config.module_exit = close_once
    display.send_frame(display.WaveshareDriver(), b"\x11")
    assert calls.count(("close",)) == 1


def test_repeated_vendor_lifecycles_keep_cached_gpio_objects_reusable(monkeypatch):
    vendor, config, _ = install_vendor(monkeypatch, optional_cleanup=True)
    gpio_closed = False

    def cleanup(cleanup=False):
        nonlocal gpio_closed
        if cleanup:
            gpio_closed = True

    def initialize(self):
        if gpio_closed:
            raise RuntimeError("Cached gpiozero LED is closed")
        return 0

    config.module_exit = cleanup
    vendor.EPD.init = initialize
    display.send_frame(display.WaveshareDriver(), b"\x11")
    display.send_frame(display.WaveshareDriver(), b"\x11")
