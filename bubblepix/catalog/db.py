import sqlite3
import os
from pathlib import Path


MIGRATIONS = [
    "ALTER TABLE catalog ADD COLUMN source_type TEXT",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT,
    phash TEXT,
    exif_date TEXT,
    exif_original_date TEXT,
    exif_digitized_date TEXT,
    exif_modify_date TEXT,
    video_creation_date TEXT,
    name_date TEXT,
    exif_camera TEXT,
    exif_gps_lat REAL,
    exif_gps_lon REAL,
    exif_width INTEGER,
    exif_height INTEGER,
    exif_orientation INTEGER,
    has_exif INTEGER DEFAULT 0,
    source_root TEXT NOT NULL,
    source_rel TEXT NOT NULL,
    source_type TEXT,
    category TEXT,
    tier TEXT,
    person_tags TEXT
);

CREATE INDEX IF NOT EXISTS idx_path ON catalog(path);
CREATE INDEX IF NOT EXISTS idx_sha256 ON catalog(sha256);
CREATE INDEX IF NOT EXISTS idx_source ON catalog(source_root);
CREATE INDEX IF NOT EXISTS idx_exif_date ON catalog(exif_date);
CREATE INDEX IF NOT EXISTS idx_name_date ON catalog(name_date);
CREATE INDEX IF NOT EXISTS idx_tier ON catalog(tier);
"""


class CatalogDB:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path.home() / ".bubblepix" / "catalog.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=OFF")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def file_exists(self, path: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM catalog WHERE path = ?", (path,))
        return cur.fetchone() is not None

    def insert_file(self, row: dict) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO catalog
                (path, filename, extension, size, mtime, sha256, phash,
                 exif_date, exif_original_date, exif_digitized_date,
                 exif_modify_date, video_creation_date, name_date,
                 exif_camera, exif_gps_lat, exif_gps_lon,
                 exif_width, exif_height, exif_orientation, has_exif,
                 source_root, source_rel, source_type)
            VALUES
                (:path, :filename, :extension, :size, :mtime, :sha256, :phash,
                 :exif_date, :exif_original_date, :exif_digitized_date,
                 :exif_modify_date, :video_creation_date, :name_date,
                 :exif_camera, :exif_gps_lat, :exif_gps_lon,
                 :exif_width, :exif_height, :exif_orientation, :has_exif,
                 :source_root, :source_rel, :source_type)
        """, row)

    def commit(self):
        self.conn.commit()

    def row_count(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM catalog")
        return cur.fetchone()[0]

    def dup_groups(self, min_count: int = 2):
        cur = self.conn.execute("""
            SELECT sha256, COUNT(*) as cnt, GROUP_CONCAT(path, '|')
            FROM catalog
            WHERE sha256 IS NOT NULL
            GROUP BY sha256
            HAVING cnt >= ?
            ORDER BY cnt DESC
        """, (min_count,))
        return cur.fetchall()

    def orphan_files(self):
        cur = self.conn.execute("""
            SELECT path, size, mtime FROM catalog
            WHERE exif_date IS NULL AND name_date IS NULL
              AND video_creation_date IS NULL
        """)
        return cur.fetchall()

    def summary(self):
        cur = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN exif_date IS NOT NULL
                              OR video_creation_date IS NOT NULL
                              OR name_date IS NOT NULL
                         THEN 1 ELSE 0 END) as with_date,
                SUM(CASE WHEN sha256 IS NOT NULL THEN 1 ELSE 0 END) as hashed,
                SUM(size) as total_bytes
            FROM catalog
        """)
        return dict(zip(["total", "with_date", "hashed", "total_bytes"], cur.fetchone()))

    def format_summary(self):
        s = self.summary()
        dup_count = len(self.dup_groups())
        orphan_count = len(self.orphan_files())
        lines = [
            f"Total files:     {s['total']:,}",
            f"With EXIF date:  {s['with_date']:,}",
            f"Hashed:          {s['hashed']:,}",
            f"Dup groups:      {dup_count:,}",
            f"Orphan files:    {orphan_count:,}",
        ]
        if s["total_bytes"]:
            gb = s["total_bytes"] / (1024**3)
            lines.append(f"Total size:      {gb:.1f} GB"),
        return "\n".join(lines)

    def close(self):
        self.conn.close()
