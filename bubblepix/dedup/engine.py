import logging
import os
import sys

from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
from sklearn.neighbors import NearestNeighbors

from bubblepix.catalog.db import CatalogDB


PHASH_GROUP_SQL = """
    SELECT phash, path, source_root, source_rel, size, source_type
    FROM catalog
    WHERE extension IN ('.jpg', '.jpeg', '.png', '.heic', '.webp')
      AND source_root NOT LIKE ?
      AND phash IS NOT NULL AND phash != '0000000000000000'
      AND NOT EXISTS (
        SELECT 1 FROM dedup_group_files f WHERE f.file_path = catalog.path)
      AND phash IN (
        SELECT phash FROM catalog
        WHERE phash IS NOT NULL AND phash != '0000000000000000'
          AND extension IN ('.jpg', '.jpeg', '.png', '.heic', '.webp')
          AND source_root NOT LIKE ?
          AND NOT EXISTS (
            SELECT 1 FROM dedup_group_files f WHERE f.file_path = catalog.path)
        GROUP BY phash
        HAVING COUNT(DISTINCT path) > 1
      )
    ORDER BY phash, size DESC
"""

UNGOUPED_META_SQL = """
    SELECT path, source_root, source_rel, size, source_type
    FROM catalog
    WHERE extension IN ('.jpg', '.jpeg', '.png', '.heic', '.webp')
      AND source_root NOT LIKE ?
      AND NOT EXISTS (
        SELECT 1 FROM dedup_group_files f WHERE f.file_path = catalog.path
      )
"""



MODEL = "mobilenetv3_small"


def encode_unencoded_images(db: CatalogDB, limit: int = 0,
                            model: str = MODEL) -> int:
    try:
        from imagededup.methods import CNN
    except ImportError:
        sys.exit("imagededup not installed. Run: pip install imagededup")
    from tqdm import tqdm
    import pillow_heif
    pillow_heif.register_heif_opener()

    paths = db.get_uncoded_paths(model)
    if limit > 0:
        paths = paths[:limit]
    if not paths:
        return 0
    cnn = CNN(verbose=False)
    for i, fp in enumerate(tqdm(paths, desc="Encoding", unit="img", smoothing=0.05)):
        if not os.path.exists(fp):
            logging.warning("File missing during encoding: %s", fp)
            continue
        vec = cnn.encode_image(fp)
        if vec is not None:
            db.store_encoding(fp, vec.tobytes(), model)
        if i % 100 == 0:
            db.commit()
    db.commit()
    return len(paths)


class DedupEngine:
    def __init__(self, threshold: float = 0.75,
                 dups_dir: str = "~/.bubblepix/00DUPLICATES"):
        self.threshold = threshold
        self.dups_dir = os.path.expanduser(dups_dir)

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

    # ── CNN groups (NN hub clustering, assumes encodings already exist) ──

    def find_cnn_groups_all_images(self, db: CatalogDB,
                                   limit: int = 0) -> list[list[dict]]:
        all_rows = db.get_encodings(MODEL)
        if len(all_rows) < 2:
            return []

        paths = [r[0] for r in all_rows]
        blobs = [r[1] for r in all_rows]
        matrix = np.frombuffer(b''.join(blobs), dtype=np.float32).reshape(len(blobs), -1)

        pattern = f"{self.dups_dir}/%"
        cur = db.conn.execute(UNGOUPED_META_SQL, (pattern,))
        meta = {
            r[0]: {"source_root": r[1], "source_rel": r[2],
                   "size": r[3], "source_type": r[4]}
            for r in cur.fetchall()
        }

        ungrouped_paths = [p for p in paths if p in meta]
        if limit > 0:
            ungrouped_paths = ungrouped_paths[:limit]
        if len(ungrouped_paths) < 2:
            return []
        path_to_idx = {p: i for i, p in enumerate(paths)}
        ungrouped_idx = [path_to_idx[p] for p in ungrouped_paths]
        ungrouped_matrix = matrix[ungrouped_idx]

        nn = NearestNeighbors(radius=1.0 - self.threshold,
                              metric="cosine", algorithm="brute", n_jobs=-1)
        nn.fit(ungrouped_matrix)
        sparse_graph = nn.radius_neighbors_graph(ungrouped_matrix, mode="distance")

        order = sorted(
            range(len(ungrouped_paths)),
            key=lambda i: (
                self._org_score(meta[ungrouped_paths[i]]["source_type"],
                                meta[ungrouped_paths[i]]["source_rel"]),
                meta[ungrouped_paths[i]]["size"],
            ),
            reverse=True,
        )

        assigned: set[str] = set()
        groups: list[list[dict]] = []

        for hub_rank in order:
            hub_path = ungrouped_paths[hub_rank]
            if hub_path in assigned:
                continue
            row = sparse_graph[hub_rank]
            neigh_indices = row.indices
            if len(neigh_indices) < 2:
                assigned.add(hub_path)
                continue

            members = []
            for ni in neigh_indices:
                npath = ungrouped_paths[ni]
                if npath != hub_path and npath not in assigned:
                    dist = row[0, ni]
                    sim = float(max(0.0, 1.0 - dist))
                    members.append({"path": npath, "similarity": sim})

            if not members:
                assigned.add(hub_path)
                continue

            group = [{"path": hub_path, "is_original": True, "similarity": None}]
            group[0].update(meta[hub_path])
            for m in members:
                entry = {"path": m["path"], "is_original": False,
                         "similarity": m["similarity"]}
                entry.update(meta[m["path"]])
                group.append(entry)
                assigned.add(m["path"])
            assigned.add(hub_path)
            groups.append(group)

        return groups

    # ── Storage ──

    def store_groups(self, db: CatalogDB, groups: list[list[dict]], group_type: str):
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
