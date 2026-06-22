import re
import datetime

# Dropbox Camera Upload:  "2026-06-03 18.11.50.mp4"
PAT_DROPBOX = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})")

# Screenshots:  "Screenshot from 2026-06-03 18-11-50.png"
PAT_SCREENSHOT = re.compile(r"Screenshot from (\d{4})-(\d{2})-(\d{2}) (\d{2})-(\d{2})-(\d{2})")

# WhatsApp:  "IMG-20240703-WA0000.jpg"  or  "VID-20240703-WA0000.mp4"
PAT_WHATSAPP = re.compile(r"(?:IMG|VID|IMG_|VID_)-?(\d{4})(\d{2})(\d{2})-WA")

# Camera:  "IMG_20240603_181150.jpg"  or  "20240603_181150.jpg"
PAT_CAMERA = re.compile(r"(?:IMG_|VID_)?(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")

# iPhone burst:  "IMG_1234.JPG"  (no date in name — skip)

# PHOTO:  "PHOTO-2026-05-30-15-44-01.jpg"
PAT_PHOTO = re.compile(r"PHOTO-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")

PATTERNS = [PAT_DROPBOX, PAT_SCREENSHOT, PAT_WHATSAPP, PAT_CAMERA, PAT_PHOTO]


def parse_filename_date(filename: str) -> str | None:
    for pat in PATTERNS:
        m = pat.search(filename)
        if m:
            try:
                parts = [int(g) for g in m.groups()]
                dt = datetime.datetime(*parts)
                return dt.isoformat()
            except (ValueError, OverflowError):
                continue
    return None
