import logging
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

_LOG_DIR = os.path.expanduser("~/.bubblepix")


def _worker_init():
    """Configure logging in worker processes (forkserver/spawn don't inherit parent)."""
    os.makedirs(_LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        filename=os.path.join(_LOG_DIR, "bubblepix.log"),
        format="[%(levelname)s] %(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.captureWarnings(True)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tiff"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mts", ".3gp"}


def ansi(code, text):
    if USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text


def _process_file(filepath: str, source_root: str, source_type: str) -> dict | None:
    if not os.path.exists(filepath):
        logging.warning("File disappeared: %s", filepath)
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
    row["has_exif"] = 1 if any((
        row["exif_date"], row["exif_original_date"],
        row["exif_digitized_date"], row["exif_modify_date"],
        row["video_creation_date"],
        row["exif_camera"],
        row["exif_gps_lat"] is not None,
        row["exif_gps_lon"] is not None,
    )) else 0
    return row


class CatalogBuilder:
    def __init__(self, dry_run: bool = False,
                 ingest_dirs: list[str] | None = None,
                 archive_dirs: list[str] | None = None,
                 limit: int = 0, workers: int = 0,
                 skip_prefixes: tuple[str, ...] | None = None,
                 rescan: bool = False,
                 rescan_incomplete: bool = False):
        self.dry_run = dry_run
        self.ingest_dirs = ingest_dirs or []
        self.archive_dirs = archive_dirs or []
        self.limit = limit
        self.workers = workers if workers > 0 else (os.cpu_count() or 1)
        self.skip_prefixes = skip_prefixes
        self.rescan = rescan
        self.rescan_incomplete = rescan_incomplete
        self.db = None if dry_run else CatalogDB()

    def _collect_futures(self, executor, paths, existing_paths, rescan_paths):
        futures = []
        for fp, sr, st in paths:
            if self.rescan:
                pass
            elif self.rescan_incomplete:
                if fp in existing_paths and fp not in rescan_paths:
                    continue
            elif fp in existing_paths:
                continue
            futures.append(executor.submit(_process_file, fp, sr, st))
        return futures

    def _process_futures(self, executor, futures):
        file_count = new_count = upd_count = rev_count = skip_count = 0
        if not futures:
            print("  (nothing to process)")
            return file_count, new_count, upd_count, skip_count, rev_count
        try:
            for future in tqdm(as_completed(futures), total=len(futures),
                               desc="Processing", unit="files", smoothing=0.05):
                row = future.result()
                if row is None:
                    skip_count += 1
                    continue
                file_count += 1
                if self.db.file_exists(row["path"]):
                    upd_count += 1
                    self.db.insert_file(row)
                elif self.db.revive_by_sha256(row):
                    rev_count += 1
                else:
                    new_count += 1
                    self.db.insert_file(row)
                if file_count % 5000 == 0:
                    self.db.commit()
        except KeyboardInterrupt:
            executor.shutdown(wait=False, cancel_futures=True)
            print("\nShutting down...")
            os._exit(130)
        return file_count, new_count, upd_count, skip_count, rev_count

    def _print_summary(self, new_count, upd_count, skip_count, rev_count):
        if self.dry_run:
            return
        self.db.commit()
        s = self.db.summary()
        gb = (s["total_bytes"] or 0) / (1024**3)
        dup_count = len(self.db.dup_groups())
        orphan_count = len(self.db.orphan_files())
        no_phash = s["total"] - s["hashed"]
        print(f"\nCatalog: {ansi('1', self.db.db_path)}")
        print(f"  {s['total']:>8,} files  ({gb:.1f} GB)")
        print(f"  {new_count:>8,} new  {rev_count:>8,} revived"
              f"  {upd_count:>8,} unchanged")
        if skip_count:
            print(f"  {skip_count:>8,} skipped (missing or corrupt)")
        if s["no_phash"]:
            print(f"  {s['no_phash']:>8,} no phash (non-image or oversized)")
        print(f"  {s['with_date']:>8,} with EXIF date")
        print(f"  {orphan_count:>8,} without date (orphans)")
        print(f"  {dup_count:>8,} duplicate groups")
        self.db.close()

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
        paths = list(walker.walk())
        if self.limit > 0:
            paths = paths[:self.limit]
        print(f"Found {len(paths):,} media files")
        print(f"Processing with {self.workers} workers...")

        if self.dry_run:
            print(f"\nWould process {len(paths):,} files")
            return

        existing_paths = {r[0] for r in self.db.conn.execute(
            "SELECT path FROM catalog WHERE tombstone = 0").fetchall()}
        rescan_paths = set()
        if self.rescan_incomplete:
            rescan_paths = {r[0] for r in self.db.conn.execute(
                "SELECT path FROM catalog WHERE tombstone = 0"
                " AND (phash IS NULL OR has_exif = 0)").fetchall()}

        if not self.limit:
            walked = {fp for fp, _, _ in paths}
            stale = existing_paths - walked
            if stale:
                for path in stale:
                    self.db.conn.execute(
                        "UPDATE catalog SET tombstone = 1 WHERE path = ?", (path,))
                self.db.commit()
                print(f"  Tombstoned {len(stale):,} stale entries")

        with ProcessPoolExecutor(max_workers=self.workers, initializer=_worker_init) as executor:
            futures = self._collect_futures(executor, paths, existing_paths, rescan_paths)
            _, new_count, upd_count, skip_count, rev_count = self._process_futures(executor, futures)
        self._print_summary(new_count, upd_count, skip_count, rev_count)
