import json
import subprocess
import re

from .exif import _normalize_camera


def extract_video_metadata(path: str) -> dict:
    result = {
        "date": None,
        "creation_date": None,
        "apple_creation_date": None,
        "camera": None,
        "gps_lat": None,
        "gps_lon": None,
        "width": None,
        "height": None,
        "orientation": None,
        "duration": None,
        "has_exif": False,
    }
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return result
        data = json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return result

    result["has_exif"] = True

    tags = {}
    fmt = data.get("format") or {}
    tags.update(fmt.get("tags") or {})

    video_stream = None
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video":
            video_stream = s
            result["width"] = s.get("width")
            result["height"] = s.get("height")
            dur = fmt.get("duration")
            if dur:
                result["duration"] = round(float(dur), 2)
            stags = s.get("tags") or {}
            if "rotate" in stags:
                result["orientation"] = int(stags["rotate"])
            break
    orient = result["orientation"]
    if orient in (90, 270):
        result["width"], result["height"] = result["height"], result["width"]
    if video_stream and video_stream.get("tags"):
        tags.update(video_stream["tags"])

    # Dates
    result["creation_date"] = _normalize_date(tags.get("creation_time"))
    result["apple_creation_date"] = _normalize_date(tags.get("com.apple.quicktime.creationdate"))
    result["date"] = result["creation_date"] or result["apple_creation_date"]

    # Camera — try ffprobe tags first, then exiftool fallback for non-standard fields
    result["camera"] = _detect_camera(tags)
    if result["camera"] in (None, "Samsung Galaxy", "Android Phone"):
        et_camera = _exiftool_camera(path)
        if et_camera:
            result["camera"] = et_camera

    # GPS
    gps = _parse_video_gps(tags)
    if gps:
        result["gps_lat"], result["gps_lon"] = gps

    return result


def _detect_camera(tags: dict) -> str | None:
    # 1. Apple quicktime tags
    make = tags.get("com.apple.quicktime.make", "")
    model = tags.get("com.apple.quicktime.model", "")
    if make or model:
        return _normalize_camera(make, model)

    # 2. Standard Make/Model
    make = tags.get("Make", "")
    model = tags.get("Model", "")
    if make or model:
        return _normalize_camera(make, model)

    # 3. Case-insensitive scan across all tags for make/model
    for k, v in tags.items():
        kl = k.lower()
        if "model" in kl or "device" in kl:
            v = str(v).strip()
            if v and v != " ":
                return v

    # 4. Android-specific inference
    has_samsung = any("samsung" in k.lower() for k in tags)
    has_android = any("android" in k.lower() for k in tags)
    if has_samsung:
        return "Samsung Galaxy"
    if has_android:
        return "Android Phone"

    return None


def _normalize_date(val: str | None) -> str | None:
    if not val:
        return None
    val = val.replace("Z", "+00:00")
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(val)
        return dt.isoformat()
    except ValueError:
        return val


def _exiftool_camera(path: str) -> str | None:
    """Fallback: use exiftool to read non-standard MP4 camera fields
    (Samsung Author/Model, etc.) that ffprobe ignores."""
    try:
        out = subprocess.run(
            ["exiftool", "-json", "-Author", "-SamsungModel",
             "-Make", "-Model", path],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
        if not data:
            return None
        d = data[0]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None

    # Priority: explicit Make/Model > Author + SamsungModel > Author alone
    make = (d.get("Make") or "").strip()
    model = (d.get("Model") or "").strip()
    if make or model:
        return _normalize_camera(make, model)

    author = (d.get("Author") or "").strip()
    samsung_model = (d.get("SamsungModel") or "").strip()
    if author and samsung_model:
        return f"Samsung {author} ({samsung_model})"
    if author:
        # Author is e.g. "Galaxy S23+" — prepend Samsung
        if "samsung" not in author.lower() and "galaxy" in author.lower():
            return f"Samsung {author}"
        return author
    if samsung_model:
        return f"Samsung {samsung_model}"
    return None


GPS_RE = re.compile(r"^([+-]\d+\.\d+)([+-]\d+\.\d+)/?$")


def _parse_video_gps(tags: dict) -> tuple | None:
    raw = tags.get("location") or tags.get("location-eng")
    if not raw:
        return None
    m = GPS_RE.match(raw.strip())
    if m:
        return float(m.group(1)), float(m.group(2))
    return None
