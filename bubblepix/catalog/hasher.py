import hashlib
import logging
import math
import warnings
from PIL import Image, ImageOps
import imagehash
import pillow_heif
pillow_heif.register_heif_opener()


MAX_MP = 50
MAX_PIXELS = MAX_MP * 1_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS * 2


def sha256_file(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        logging.warning("Failed to read: %s", path)
        return None


def _ensure_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ('LA', 'PA', 'RGBA'):
        bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img.convert('RGBA'))
    return img.convert('RGB')


def perceptual_hash(path: str) -> str | None:
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            img = Image.open(path)
            for warning in caught:
                if "DecompressionBomb" in str(warning.message):
                    logging.warning("Oversized image: %s", path)
        w, h = img.size
        if w * h > MAX_PIXELS:
            scale = math.sqrt(MAX_PIXELS / (w * h))
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        with warnings.catch_warnings(record=True) as caught2:
            warnings.simplefilter("always")
            img = ImageOps.exif_transpose(img) or img
            for warning in caught2:
                if "Corrupt EXIF data" in str(warning.message):
                    logging.warning("Corrupt EXIF in %s", path)
        img = _ensure_rgb(img)
        h = imagehash.phash(img)
        return str(h)
    except (OSError, ValueError, Image.DecompressionBombError):
        logging.warning("Corrupt image: %s", path)
        return None
