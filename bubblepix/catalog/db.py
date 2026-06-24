import logging
import sqlite3
import os
from pathlib import Path


MIGRATIONS = [
    "ALTER TABLE catalog ADD COLUMN source_type TEXT",
    "ALTER TABLE catalog ADD COLUMN moved_to TEXT",
    "ALTER TABLE catalog ADD COLUMN tombstone INTEGER DEFAULT 0",
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
    person_tags TEXT,
    moved_to TEXT,
    tombstone INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_path ON catalog(path);
CREATE INDEX IF NOT EXISTS idx_sha256 ON catalog(sha256);
CREATE INDEX IF NOT EXISTS idx_source ON catalog(source_root);
CREATE INDEX IF NOT EXISTS idx_exif_date ON catalog(exif_date);
CREATE INDEX IF NOT EXISTS idx_name_date ON catalog(name_date);
CREATE INDEX IF NOT EXISTS idx_tier ON catalog(tier);

CREATE TABLE IF NOT EXISTS encodings (
    file_path TEXT PRIMARY KEY
        REFERENCES catalog(path) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_encodings_model ON encodings(model);

CREATE TABLE IF NOT EXISTS dedup_groups (
    id INTEGER PRIMARY KEY,
    group_type TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dedup_group_files (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    is_original INTEGER DEFAULT 0,
    similarity REAL,
    reviewed INTEGER DEFAULT 0,
    action TEXT,
    FOREIGN KEY (group_id) REFERENCES dedup_groups(id)
);

CREATE INDEX IF NOT EXISTS idx_dedup_group_files_path
    ON dedup_group_files(file_path);
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
        cur = self.conn.execute(
            "SELECT 1 FROM catalog WHERE path = ? AND tombstone = 0", (path,))
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

    def revive_by_sha256(self, row: dict) -> str | None:
        cur = self.conn.execute(
            "SELECT id, path FROM catalog WHERE sha256 = ? AND tombstone = 1"
            " LIMIT 1",
            (row["sha256"],),
        )
        hit = cur.fetchone()
        if not hit:
            return None
        old_id, old_path = hit
        self.conn.execute("""
            UPDATE catalog SET
                path=:path, filename=:filename, extension=:extension,
                size=:size, mtime=:mtime, tombstone=0, source_root=:source_root,
                source_rel=:source_rel, source_type=:source_type
            WHERE id=:id
        """, row | {"id": old_id})
        return old_path

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
                SUM(CASE WHEN phash IS NULL THEN 1 ELSE 0 END) as no_phash,
                SUM(size) as total_bytes
            FROM catalog
        """)
        return dict(zip(["total", "with_date", "hashed", "no_phash", "total_bytes"], cur.fetchone()))

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

    # ── Encodings ──

    def get_encodings(self, model: str):
        """Return list of (file_path, bytes) for all encodings of this model."""
        return self.conn.execute(
            "SELECT file_path, vector FROM encodings WHERE model=? AND length(vector) > 0 ORDER BY rowid",
            (model,),
        ).fetchall()

    def get_uncoded_paths(self, model: str):
        """Return paths of ungrouped image files not yet encoded."""
        return [r[0] for r in self.conn.execute("""
            SELECT c.path FROM catalog c
            WHERE c.extension IN ('.jpg','.jpeg','.png','.heic','.webp')
              AND NOT EXISTS (
                  SELECT 1 FROM dedup_group_files f WHERE f.file_path = c.path)
              AND NOT EXISTS (
                  SELECT 1 FROM encodings e
                  WHERE e.file_path = c.path AND e.model=?)
            ORDER BY c.path
        """, (model,))]

    def store_encoding(self, path: str, vector_bytes: bytes, model: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO encodings (file_path, model, vector) VALUES (?, ?, ?)",
            (path, model, vector_bytes),
        )

    def encoding_count(self, model: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM encodings WHERE model=?", (model,),
        )
        return cur.fetchone()[0]

    def delete_encodings(self, model: str):
        self.conn.execute("DELETE FROM encodings WHERE model=?", (model,))
