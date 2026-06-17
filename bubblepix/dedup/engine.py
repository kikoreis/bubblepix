import os
import sys
import tempfile
import shutil
from collections import defaultdict
from datetime import datetime, timedelta

from bubblepix.catalog.db import CatalogDB

try:
    from imagededup.methods import CNN
    HAS_IMAGEDEDUP = True
except ImportError:
    HAS_IMAGEDEDUP = False


PHASH_GROUP_SQL = """
    SELECT phash, path, source_root, source_rel, size, source_type
    FROM catalog
    WHERE extension IN ('.jpg', '.jpeg', '.png', '.heic', '.webp')
      AND source_root NOT LIKE ?
      AND phash IS NOT NULL
      AND phash IN (
        SELECT phash FROM catalog
        WHERE phash IS NOT NULL
          AND extension IN ('.jpg', '.jpeg', '.png', '.heic', '.webp')
          AND source_root NOT LIKE ?
        GROUP BY phash
        HAVING COUNT(DISTINCT path) > 1
      )
    ORDER BY phash, size DESC
"""

CNN_IMAGE_SQL = """
    SELECT path, source_root, source_rel, size, source_type
    FROM catalog c
    WHERE extension IN ('.jpg', '.jpeg', '.png', '.heic', '.webp')
      AND source_root NOT LIKE ?
      AND NOT EXISTS (
        SELECT 1 FROM dedup_group_files f WHERE f.file_path = c.path
      )
    ORDER BY size DESC
"""


def symlink_files(file_map: dict[str, str], target_dir: str):
    for short_name, full_path in file_map.items():
        link_path = os.path.join(target_dir, short_name)
        if not os.path.exists(link_path):
            os.symlink(full_path, link_path)


def clean_temp_dir(tmp_dir: str):
    try:
        shutil.rmtree(tmp_dir)
    except OSError:
        pass


class DedupEngine:
    def __init__(self, threshold: float = 0.75, dups_dir: str = "~/.bubblepix/00DUPLICATES"):
        self.threshold = threshold
        self.dups_dir = os.path.expanduser(dups_dir)
        self._cnn = None

    @property
    def cnn(self):
        if self._cnn is None:
            if not HAS_IMAGEDEDUP:
                sys.exit("imagededup not installed. Run: pip install imagededup")
            self._cnn = CNN(verbose=False)
        return self._cnn

    # ── Helpers ──

    @staticmethod
    def _org_score(source_type: str, source_rel: str) -> int:
        if source_type == "archive":
            return 3
        if source_type == "ingest":
            if "/" not in source_rel.rstrip("/"):
                return 2
            return 1
        return 0

    @staticmethod
    def _pick_original(files: list[dict]) -> int:
        best_i = 0
        for i, f in enumerate(files):
            cur = (DedupEngine._org_score(f["source_type"], f["source_rel"]), f["size"])
            best = (DedupEngine._org_score(files[best_i]["source_type"], files[best_i]["source_rel"]), files[best_i]["size"])
            if cur > best:
                best_i = i
        return best_i

    # ── Phash groups ──

    def find_phash_groups(self, db: CatalogDB) -> list[list[dict]]:
        pattern = f"{self.dups_dir}/%"
        groups: dict[str, list[dict]] = {}
        cur = db.conn.execute(PHASH_GROUP_SQL, (pattern, pattern))
        for row in cur.fetchall():
            phash, path, root, rel, size, stype = row
            if phash not in groups:
                groups[phash] = []
            groups[phash].append({
                "path": path, "source_root": root, "source_rel": rel,
                "size": size, "source_type": stype,
            })
        result = []
        for _phash, files in groups.items():
            if len(files) < 2:
                continue
            best_i = self._pick_original(files)
            for i, f in enumerate(files):
                f["is_original"] = (i == best_i)
                f["similarity"] = None
            result.append(files)
        return result

    # ── CNN groups (hub clustering on all ungrouped images) ──

    def find_cnn_groups_all_images(self, db: CatalogDB) -> list[list[dict]]:
        pattern = f"{self.dups_dir}/%"
        cur = db.conn.execute(CNN_IMAGE_SQL, (pattern,))
        rows = cur.fetchall()
        if len(rows) < 2:
            return []

        meta = {
            r[0]: {"path": r[0], "source_root": r[1], "source_rel": r[2],
                   "size": r[3], "source_type": r[4]}
            for r in rows
        }

        tmp_dir = tempfile.mkdtemp(prefix="bubblepix_cnn_")
        try:
            short_to_full = {}
            for fp in meta:
                short = os.path.basename(fp)
                if short in short_to_full:
                    base, ext = os.path.splitext(short)
                    short = f"{base}_{hash(fp) & 0xFFFF}{ext}"
                short_to_full[short] = fp
            symlink_files(short_to_full, tmp_dir)
            encodings = self.cnn.encode_images(image_dir=tmp_dir)
            if len(encodings) < 2:
                return []
            duplicates = self.cnn.find_duplicates(
                encoding_map=encodings,
                min_similarity_threshold=self.threshold,
                scores=True,
            )
        finally:
            clean_temp_dir(tmp_dir)

        full_to_short = {v: k for k, v in short_to_full.items()}

        all_paths = sorted(
            meta.keys(),
            key=lambda p: (
                self._org_score(meta[p]["source_type"], meta[p]["source_rel"]),
                meta[p]["size"],
            ),
            reverse=True,
        )

        assigned: set[str] = set()
        groups: list[list[dict]] = []

        for hub_path in all_paths:
            if hub_path in assigned:
                continue
            hub_short = full_to_short.get(hub_path)
            if not hub_short or hub_short not in duplicates:
                assigned.add(hub_path)
                continue

            members = []
            for dup_short, score in duplicates[hub_short]:
                dup_path = short_to_full.get(dup_short)
                if dup_path and dup_path != hub_path and dup_path not in assigned:
                    members.append({"path": dup_path, "similarity": float(score)})

            if not members:
                assigned.add(hub_path)
                continue

            group = [{"path": hub_path, "is_original": True, "similarity": None}]
            group[0].update(meta[hub_path])
            for m in members:
                entry = {"path": m["path"], "is_original": False, "similarity": m["similarity"]}
                entry.update(meta[m["path"]])
                group.append(entry)
                assigned.add(m["path"])
            assigned.add(hub_path)
            groups.append(group)

        return groups

    # ── Storage ──

    def store_groups(self, db: CatalogDB, groups: list[list[dict]], group_type: str):
        db.conn.execute("""
            CREATE TABLE IF NOT EXISTS dedup_groups (
                id INTEGER PRIMARY KEY,
                group_type TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        db.conn.execute("""
            CREATE TABLE IF NOT EXISTS dedup_group_files (
                id INTEGER PRIMARY KEY,
                group_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                is_original INTEGER DEFAULT 0,
                similarity REAL,
                reviewed INTEGER DEFAULT 0,
                action TEXT,
                FOREIGN KEY (group_id) REFERENCES dedup_groups(id)
            )
        """)
        for group_files in groups:
            gid = db.conn.execute(
                "INSERT INTO dedup_groups (group_type) VALUES (?)",
                (group_type,),
            ).lastrowid
            for f in group_files:
                action = "keep" if f.get("is_original") else "move"
                db.conn.execute("""
                    INSERT INTO dedup_group_files
                        (group_id, file_path, is_original, similarity, action)
                    VALUES (?, ?, ?, ?, ?)
                """, (gid, f["path"], int(f.get("is_original", False)),
                      f.get("similarity"), action))
        db.commit()
