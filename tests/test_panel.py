import pytest
from PIL import Image

from paiper.config import PanelProfile
from paiper.panel import encode_native, prepare_native, quantize_photo, quantize_ui

COLORS = (
    (0, 0, 0),
    (255, 255, 255),
    (255, 255, 0),
    (255, 0, 0),
    (0, 0, 255),
    (0, 255, 0),
)


def pixel_values(image):
    return [
        image.getpixel((x, y)) for y in range(image.height) for x in range(image.width)
    ]


def test_wire_codes_are_not_palette_indices():
    image = Image.new("RGB", (400, 600), "white")
    image.putpixel((0, 0), (0, 0, 255))
    image.putpixel((1, 0), (0, 255, 0))
    result = encode_native(image)
    assert len(result) == 120000
    assert result[:2] == bytes([0x56, 0x11])


def test_ccw_mount_compensation_is_clockwise():
    image = Image.new("RGB", (600, 400), "white")
    image.putpixel((0, 0), (255, 0, 0))
    result = prepare_native(image, PanelProfile(rotation=270))
    assert result.size == (400, 600)
    assert result.getpixel((399, 0)) == (255, 0, 0)


@pytest.mark.parametrize(
    ("rotation", "corners"),
    [
        (270, ((399, 0), (399, 599), (0, 0), (0, 599))),
        (90, ((0, 599), (0, 0), (399, 599), (399, 0))),
    ],
)
def test_prepare_native_rotates_all_corners_once(rotation, corners):
    image = Image.new("RGB", (600, 400), "white")
    for point, color in zip(((0, 0), (599, 0), (0, 399), (599, 399)), COLORS[2:]):
        image.putpixel(point, color)
    before = image.tobytes()
    result = prepare_native(image, PanelProfile(rotation=rotation))
    assert result.size == (400, 600)
    assert tuple(result.getpixel(point) for point in corners) == COLORS[2:]
    assert image.tobytes() == before


@pytest.mark.parametrize("size", [(400, 600), (600, 399), (599, 400), (1, 1)])
def test_prepare_native_rejects_non_logical_dimensions(size):
    with pytest.raises(ValueError):
        prepare_native(Image.new("RGB", size), PanelProfile())


@pytest.mark.parametrize("quantize", [quantize_ui, quantize_photo])
def test_quantization_gradient_contains_only_panel_colors(quantize):
    image = Image.new("RGB", (256, 32))
    image.putdata([(x, y * 8, 255 - x) for y in range(32) for x in range(256)])
    before = image.tobytes()
    result = quantize(image)
    assert result.mode == "RGB"
    assert result.size == image.size
    assert set(pixel_values(result)) <= set(COLORS)
    assert image.tobytes() == before


@pytest.mark.parametrize("quantize", [quantize_ui, quantize_photo])
def test_quantization_preserves_all_six_colors_including_white_black(quantize):
    image = Image.new("RGB", (6, 1))
    image.putdata(COLORS)
    assert pixel_values(quantize(image)) == list(COLORS)


def test_ui_does_not_diffuse_uniform_gray():
    result = quantize_ui(Image.new("RGB", (32, 32), (100, 100, 100)))
    assert set(pixel_values(result)) == {(0, 0, 0)}


def test_photo_diffuses_uniform_gray():
    result = quantize_photo(Image.new("RGB", (32, 32), (100, 100, 100)))
    assert len(set(pixel_values(result))) > 1


def test_photo_uses_floyd_steinberg_error_distribution():
    result = quantize_photo(Image.new("RGB", (4, 1), (100, 100, 100)))
    assert pixel_values(result) == [(0, 0, 0), (255, 255, 255), (0, 0, 0), (0, 0, 0)]


@pytest.mark.parametrize("quantize", [quantize_ui, quantize_photo])
@pytest.mark.parametrize("mode", ["L", "P", "RGBA"])
def test_quantization_accepts_non_rgb_images(quantize, mode):
    image = Image.new(mode, (6, 4))
    result = quantize(image)
    assert result.mode == "RGB"
    assert result.size == (6, 4)
    assert set(pixel_values(result)) <= set(COLORS)


def test_encoder_packs_all_codes_in_row_major_order():
    image = Image.new("RGB", (400, 600), "white")
    for x, color in enumerate(COLORS):
        image.putpixel((x, 0), color)
    image.putpixel((399, 0), (255, 0, 0))
    image.putpixel((0, 1), (0, 0, 255))
    image.putpixel((399, 599), (0, 255, 0))
    result = encode_native(image)
    assert isinstance(result, bytes)
    assert result[:3] == bytes([0x01, 0x23, 0x56])
    assert result[199:201] == bytes([0x13, 0x51])
    assert result[-1] == 0x16


@pytest.mark.parametrize("size", [(600, 400), (399, 600), (400, 599), (1, 1)])
def test_encoder_rejects_non_native_dimensions(size):
    with pytest.raises(ValueError):
        encode_native(Image.new("RGB", size, "white"))


@pytest.mark.parametrize("point", [(0, 0), (1, 0), (399, 599)])
@pytest.mark.parametrize("color", [(255, 128, 0), (254, 255, 255), (1, 0, 0)])
def test_encoder_rejects_non_palette_pixels(point, color):
    image = Image.new("RGB", (400, 600), "white")
    image.putpixel(point, color)
    with pytest.raises(ValueError):
        encode_native(image)


@pytest.mark.parametrize("mode", ["L", "P", "RGBA"])
def test_encoder_rejects_non_rgb_images(mode):
    with pytest.raises(ValueError):
        encode_native(Image.new(mode, (400, 600)))
