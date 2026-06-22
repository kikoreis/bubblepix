from PIL import Image
from PIL.ExifTags import TAGS as EXIF_TAGS
import datetime
import sys
import warnings


CAMERA_MAKE_OVERRIDES = {
    "samsung": "Samsung",
    "apple": "Apple",
    "canon": "Canon",
    "nikon": "Nikon",
    "sony": "Sony",
    "google": "Google",
    "oneplus": "OnePlus",
    "xiaomi": "Xiaomi",
    "huawei": "Huawei",
    "motorola": "Motorola",
    "lg": "LG",
}


def _normalize_camera(make: str, model: str) -> str | None:
    make = make.strip()
    model = model.strip()
    if not make and not model:
        return None
    if make:
        lower = make.lower()
        make = CAMERA_MAKE_OVERRIDES.get(lower, make.title())
    if model and model.lower().startswith(make.lower()):
        model = model[len(make):].strip()
    if make and model:
        return f"{make} {model}"
    return make or model


def _parse_exif_date(val: str) -> str | None:
    if not val:
        return None
    try:
        return datetime.datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        return str(val)


def extract_exif(path: str) -> dict:
    result = {
        "date": None,
        "original_date": None,
        "digitized_date": None,
        "modify_date": None,
        "camera": None,
        "gps_lat": None,
        "gps_lon": None,
        "width": None,
        "height": None,
        "orientation": None,
        "has_exif": False,
    }
    try:
        img = Image.open(path)
        result["width"], result["height"] = img.size
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            exif_data = img._getexif()
            for warning in caught:
                if "Corrupt EXIF data" in str(warning.message):
                    print(f"  [WARN] Corrupt EXIF in {path}", file=sys.stderr)
        if exif_data is None:
            return result
        result["has_exif"] = True

        exif = {}
        for tag_id, value in exif_data.items():
            name = EXIF_TAGS.get(tag_id, tag_id)
            exif[name] = value

        make = exif.get("Make", "")
        model = exif.get("Model", "")
        result["camera"] = _normalize_camera(make, model)

        result["original_date"] = _parse_exif_date(exif.get("DateTimeOriginal"))
        result["digitized_date"] = _parse_exif_date(exif.get("DateTimeDigitized"))
        result["modify_date"] = _parse_exif_date(exif.get("DateTime"))
        result["date"] = result["original_date"] or result["digitized_date"] or result["modify_date"]

        orient = exif.get("Orientation")
        result["orientation"] = orient
        if orient is not None and orient >= 5:
            result["width"], result["height"] = result["height"], result["width"]

        gps_info = exif.get("GPSInfo")
        if gps_info:
            lat = _gps_to_decimal(gps_info.get(2), gps_info.get(1))
            lon = _gps_to_decimal(gps_info.get(4), gps_info.get(3))
            if lat is not None:
                result["gps_lat"] = lat
            if lon is not None:
                result["gps_lon"] = lon

    except (OSError, ValueError, Image.DecompressionBombError):
        pass
    return result


def _gps_to_decimal(coord, ref):
    if coord is None:
        return None
    try:
        degrees, minutes, seconds = float(coord[0]), float(coord[1]), float(coord[2])
        decimal = degrees + minutes / 60.0 + seconds / 3600.0
        if ref in ("S", "W"):
            decimal = -decimal
        return round(decimal, 6)
    except (TypeError, IndexError, ValueError, ZeroDivisionError):
        return None
