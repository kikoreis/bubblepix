import hashlib
from PIL import Image, ImageOps
import imagehash


def sha256_file(path: str) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def perceptual_hash(path: str) -> str | None:
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img) or img
        img = img.convert("RGB")
        h = imagehash.phash(img)
        return str(h)
    except Exception:
        return None
