import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from PIL import Image
from tqdm import tqdm

from bubblepix.catalog.db import CatalogDB
from bubblepix.catalog.walker import FileWalker
from bubblepix.catalog.hasher import sha256_file, perceptual_hash
from bubblepix.catalog.exif import extract_exif
from bubblepix.catalog.video_exif import extract_video_metadata
from bubblepix.catalog.name_date import parse_filename_date

USE_COLOR = sys.stdout.isatty()

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mts", ".3gp"}


def ansi(code, text):
    if USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text


def _process_file(filepath: str, source_root: str, source_type: str) -> dict | None:
    if not os.path.exists(filepath):
        print(f"  [WARN] File disappeared: {filepath}", file=sys.stderr)
        return None
    filename = os.path.basename(filepath)
    _, ext = os.path.splitext(filename)
    ext = ext.lower()
    stat = os.stat(filepath)

    row = {
        "path": filepath,
        "filename": filename,
        "extension": ext,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": sha256_file(filepath),
        "phash": None,
        "exif_date": None,
        "exif_original_date": None,
        "exif_digitized_date": None,
        "exif_modify_date": None,
        "video_creation_date": None,
        "name_date": parse_filename_date(filename),
        "exif_camera": None,
        "exif_gps_lat": None,
        "exif_gps_lon": None,
        "exif_width": None,
        "exif_height": None,
        "exif_orientation": None,
        "has_exif": 0,
        "source_root": os.path.expanduser(source_root),
        "source_rel": os.path.relpath(filepath, os.path.expanduser(source_root)),
        "source_type": source_type,
    }

    if ext in IMAGE_EXT:
        try:
            row["phash"] = perceptual_hash(filepath)
            exif = extract_exif(filepath)
        except (OSError, ValueError, Image.DecompressionBombError):
            exif = {}
    elif ext in VIDEO_EXT:
        exif = extract_video_metadata(filepath)
    else:
        exif = {}
    row["exif_date"] = exif.get("date")
    row["exif_original_date"] = exif.get("original_date")
    row["exif_digitized_date"] = exif.get("digitized_date")
    row["exif_modify_date"] = exif.get("modify_date")
    row["video_creation_date"] = exif.get("creation_date") or exif.get("apple_creation_date")
    row["exif_camera"] = exif.get("camera")
    row["exif_gps_lat"] = exif.get("gps_lat")
    row["exif_gps_lon"] = exif.get("gps_lon")
    row["exif_width"] = exif.get("width")
    row["exif_height"] = exif.get("height")
    row["exif_orientation"] = exif.get("orientation")
    row["has_exif"] = 1 if exif.get("has_exif") else 0
    return row


class CatalogBuilder:
    def __init__(self, dry_run: bool = False,
                 ingest_dirs: list[str] | None = None,
                 archive_dirs: list[str] | None = None,
                 limit: int = 0, workers: int = 0,
                 skip_prefixes: tuple[str, ...] | None = None):
        self.dry_run = dry_run
        self.ingest_dirs = ingest_dirs or []
        self.archive_dirs = archive_dirs or []
        self.limit = limit
        self.workers = workers if workers > 0 else (os.cpu_count() or 1)
        self.skip_prefixes = skip_prefixes
        self.db = None if dry_run else CatalogDB()

    def run(self):
        if not self.ingest_dirs and not self.archive_dirs:
            print("Error: at least one --ingest or --archive directory required", file=sys.stderr)
            sys.exit(1)
        print(ansi("1", "BubblePix — Catalog Build"))
        if self.dry_run:
            print(ansi("33", "DRY RUN — no data will be written"))

        source_pairs = ([(r, "ingest") for r in self.ingest_dirs]
                        + [(r, "archive") for r in self.archive_dirs])
        walker = FileWalker(source_pairs, skip_prefixes=self.skip_prefixes)
        file_count = 0
        new_count = 0
        upd_count = 0
        skip_count = 0

        paths = list(walker.walk())
        if self.limit > 0:
            paths = paths[:self.limit]
        print(f"Found {len(paths):,} media files")
        print(f"Processing with {self.workers} workers...")

        if self.dry_run:
            file_count = len(paths)
        else:
            with ProcessPoolExecutor(max_workers=self.workers) as executor:
                futures = [executor.submit(_process_file, fp, sr, st)
                           for fp, sr, st in paths
                           if not self.db.file_exists(fp)]
                for future in tqdm(as_completed(futures), total=len(futures),
                                   desc="Processing", unit="files"):
                    row = future.result()
                    if row is None:
                        skip_count += 1
                        continue
                    file_count += 1
                    if self.db.file_exists(row["path"]):
                        upd_count += 1
                    else:
                        new_count += 1
                    self.db.insert_file(row)
                    if file_count % 5000 == 0:
                        self.db.commit()

        if not self.dry_run:
            self.db.commit()
            s = self.db.summary()
            gb = (s["total_bytes"] or 0) / (1024**3)
            dup_count = len(self.db.dup_groups())
            orphan_count = len(self.db.orphan_files())
            no_phash = s["total"] - s["hashed"]
            print(f"\nCatalog: {ansi('1', self.db.db_path)}")
            print(f"  {s['total']:>8,} files  ({gb:.1f} GB)")
            print(f"  {new_count:>8,} new  {upd_count:>8,} updated")
            if skip_count:
                print(f"  {skip_count:>8,} skipped (missing or corrupt)")
            if s["no_phash"]:
                print(f"  {s['no_phash']:>8,} no phash (non-image or oversized)")
            print(f"  {s['with_date']:>8,} with EXIF date")
            print(f"  {orphan_count:>8,} without date (orphans)")
            print(f"  {dup_count:>8,} duplicate groups")
            self.db.close()
        else:
            print(f"\nWould process {file_count:,} files")
