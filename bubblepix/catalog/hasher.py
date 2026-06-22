import hashlib
import sys
import warnings
from PIL import Image, ImageOps
import imagehash


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
        return None


def _ensure_rgb(img: Image.Image) -> Image.Image:
    """Convert image to RGB, compositing alpha onto white if needed."""
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
                    print(f"  [WARN] Oversized image: {path}", file=sys.stderr)
        w, h = img.size
        if w * h > MAX_PIXELS:
            return None
        img = ImageOps.exif_transpose(img) or img
        img = _ensure_rgb(img)
        h = imagehash.phash(img)
        return str(h)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
