"""Serialized E6 display lifecycle, with no vendor/GPIO import at module load."""

import hashlib
import importlib
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from PIL import Image

from paiper.config import PanelProfile
from paiper.panel import encode_native, prepare_native, quantize_ui
from paiper.state import ClockError, DisplayState

logger = logging.getLogger(__name__)


class Driver(Protocol):
    def init(self) -> int: ...
    def display(self, data: bytes) -> None: ...
    def sleep(self) -> None: ...
    def close(self) -> None: ...


class DriverInitializationError(RuntimeError):
    """The driver cannot safely initialize the expected E6 panel."""


class DriverUnavailableError(RuntimeError):
    """The optional Waveshare driver or its hardware dependencies are missing."""


def send_frame(driver: Driver, payload: bytes) -> None:
    """Send once, always attempt sleep/close, and preserve the primary failure.

    This low-level primitive does not own refresh protection: production callers
    must use ``display_frame`` or ``render_and_display``, which hold persistent
    state across this call.
    """
    primary = None
    try:
        result = driver.init()
        if type(result) is not int or result != 0:
            raise DriverInitializationError(f"EPD init returned {result!r}, expected 0")
        driver.display(payload)
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_error = None
        for name in ("sleep", "close"):
            try:
                getattr(driver, name)()
            except BaseException as error:
                logger.exception("Display %s cleanup failed", name)
                if cleanup_error is None:
                    cleanup_error = error
        if primary is None and cleanup_error is not None:
            raise cleanup_error


class WaveshareDriver:
    """Thin adapter for the separately installed official ``epd4in0e`` driver.

    Only the vendor BUSY polling loop is overridden. The vendor's SPI sequence,
    including its sleep implementation, is kept intact. Cleanup uses the vendor
    ``module_exit()`` defaults, compatible with both optional-argument and
    no-argument backends.
    """

    def __init__(self, busy_timeout: float = 45.0):
        if (
            type(busy_timeout) not in (int, float)
            or not math.isfinite(busy_timeout)
            or busy_timeout <= 0
        ):
            raise ValueError("BUSY timeout must be positive and finite")
        self.busy_timeout = busy_timeout
        self._epd = None
        self._config = None
        self._initialized = False
        self._slept = False

    def init(self) -> int:
        self._slept = False
        try:
            vendor = importlib.import_module("waveshare_epd.epd4in0e")
        except ImportError as error:
            raise DriverUnavailableError(
                "Install the official waveshare_epd.epd4in0e driver and its "
                "hardware dependencies on the target device"
            ) from error
        self._config = vendor.epdconfig
        if (vendor.EPD_WIDTH, vendor.EPD_HEIGHT) != (400, 600):
            raise DriverInitializationError("E6 driver must be native 400x600")
        config, timeout = self._config, self.busy_timeout

        class BoundedEPD(vendor.EPD):
            def ReadBusyH(self):
                deadline = time.monotonic() + timeout
                while config.digital_read(self.busy_pin) == 0:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("EPD BUSY did not release")
                    config.delay_ms(5)
                config.delay_ms(200)

        self._epd = BoundedEPD()
        if (self._epd.width, self._epd.height) != (400, 600):
            raise DriverInitializationError("E6 driver must be native 400x600")
        result = self._epd.init()
        self._initialized = type(result) is int and result == 0
        return result

    def display(self, data: bytes) -> None:
        if not self._initialized:
            raise DriverInitializationError("EPD has not initialized successfully")
        self._epd.display(data)

    def sleep(self) -> None:
        if self._initialized:
            self._epd.sleep()
            self._slept = True
            self._initialized = False

    def close(self) -> None:
        if self._config is not None:
            # Official sleep already calls module_exit(). Do not double-release
            # GPIO on other backends, or force cleanup=True: that permanently
            # closes the Pi's module-cached GPIO objects, breaking the next init.
            if not self._slept:
                self._config.module_exit()
            self._config = None
        self._initialized = False


def _completion_time(start_wall: float, start_monotonic: float) -> float:
    elapsed = time.monotonic() - start_monotonic
    wall = time.time()
    if not math.isfinite(elapsed) or elapsed < 0 or not math.isfinite(wall):
        raise ClockError("Cannot establish a safe display completion time")
    # A backwards wall adjustment during a slow operation must not shorten its
    # reservation. Forward adjustments are retained as a conservative bound.
    return max(wall, start_wall + elapsed)


def display_frame(image: Image.Image, profile: PanelProfile, state_path: Path) -> bool:
    """Display an existing image under the same protection as rendered frames."""
    return render_and_display(lambda: image, profile, state_path)


def render_and_display(
    image_factory: Callable[[], Image.Image], profile: PanelProfile, state_path: Path
) -> bool:
    """Render and display one frame, or return False for a logged safe skip.

    All physical refresh entry points must share ``state_path``. The lock spans
    rendering, encoding, reservation, lazy vendor import, init, refresh, sleep,
    close, and completion. Rendering/encoding failures do not reserve an attempt.
    Failure to persist completion leaves the interrupted-attempt guard in place.
    """
    if not profile.confirmed:
        raise ValueError("Panel profile must be confirmed before hardware access")
    with DisplayState(state_path) as state:
        image = image_factory()
        payload = encode_native(prepare_native(quantize_ui(image), profile))
        digest = hashlib.sha256(payload).hexdigest()
        start_wall, start_monotonic = time.time(), time.monotonic()
        if not state.reserve(start_wall, digest):
            return False
        state.begin_attempt()
        try:
            send_frame(WaveshareDriver(), payload)
        except BaseException:
            try:
                state.finish_attempt(_completion_time(start_wall, start_monotonic))
            except BaseException:
                logger.exception("Failed to persist display attempt completion")
            raise
        state.succeed(_completion_time(start_wall, start_monotonic), digest)
    return True
