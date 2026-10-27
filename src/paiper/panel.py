"""Palette conversion, orientation, and E6 wire encoding without hardware I/O."""

from PIL import Image

from paiper.config import PanelProfile

RGB_TO_CODE = {
    (0, 0, 0): 0,
    (255, 255, 255): 1,
    (255, 255, 0): 2,
    (255, 0, 0): 3,
    (0, 0, 255): 5,
    (0, 255, 0): 6,
}


def _quantize(image: Image.Image, dither: Image.Dither) -> Image.Image:
    palette = Image.new("P", (1, 1))
    colors = list(RGB_TO_CODE)
    # Pad with a real pigment; wire codes must never depend on palette indices.
    colors.extend([colors[0]] * (256 - len(colors)))
    palette.putpalette([channel for color in colors for channel in color])
    return image.convert("RGB").quantize(palette=palette, dither=dither).convert("RGB")


def quantize_ui(image: Image.Image) -> Image.Image:
    """Return crisp RGB pixels in the six-color palette, without diffusion."""
    return _quantize(image, Image.Dither.NONE)


def quantize_photo(image: Image.Image) -> Image.Image:
    """Return six-color RGB imagery with Floyd-Steinberg error diffusion."""
    return _quantize(image, Image.Dither.FLOYDSTEINBERG)


def prepare_native(image: Image.Image, profile: PanelProfile) -> Image.Image:
    """Rotate one logical 600x400 frame once, without resizing or quantization."""
    if image.size != (600, 400):
        raise ValueError("Logical image must be exactly 600x400")
    operation = (
        Image.Transpose.ROTATE_270
        if profile.rotation == 270
        else Image.Transpose.ROTATE_90
    )
    return image.transpose(operation)


def encode_native(image: Image.Image) -> bytes:
    """Pack RGB native pixels row-major, left pixel in each byte's high nibble."""
    if image.size != PanelProfile.native_size:
        raise ValueError("Native image must be exactly 400x600")
    if image.mode != "RGB":
        raise ValueError("Native image must be RGB")
    pixels = image.load()
    output = bytearray()
    for y in range(image.height):
        for x in range(0, image.width, 2):
            left, right = pixels[x, y], pixels[x + 1, y]
            if left not in RGB_TO_CODE or right not in RGB_TO_CODE:
                raise ValueError("Native image contains a non-palette pixel")
            output.append((RGB_TO_CODE[left] << 4) | RGB_TO_CODE[right])
    return bytes(output)
