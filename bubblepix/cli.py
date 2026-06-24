import argparse
import logging
import os
import signal
import shutil
import struct
import subprocess
import sys


def _ensure_process_group():
    """Put this process in its own group so children inherit it.
    On SIGTERM/SIGHUP, kill the whole group instead of just the parent."""
    try:
        os.setpgrp()
    except PermissionError:
        pass

    def _handler(signum, frame):
        signal.signal(signum, signal.SIG_DFL)
        try:
            os.killpg(os.getpgid(0), signum)
        except (OSError, ValueError):
            pass
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGHUP, _handler)


def main():
    _ensure_process_group()
    log_dir = os.path.expanduser("~/.bubblepix")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        filename=os.path.join(log_dir, "bubblepix.log"),
        format="[%(levelname)s] %(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.captureWarnings(True)
    logging.info("=== bubblepix %s ===", " ".join(sys.argv))
    parser = argparse.ArgumentParser(prog="bubblepix")
    sub = parser.add_subparsers(dest="command", required=True)

    # catalog build
    cat = sub.add_parser("catalog", help="Catalog commands")
    cat_sub = cat.add_subparsers(dest="subcommand", required=True)

    build_p = cat_sub.add_parser("build", help="Build file catalog")
    build_p.add_argument("--dry-run", action="store_true")
    build_p.add_argument("--limit", type=int, default=0,
                         help="Max files to process (0 = unlimited)")
    build_p.add_argument("--workers", type=int, default=0,
                         help="Parallel worker count (0 = auto-detect)")
    build_p.add_argument("--ingest", type=str, action="append", default=None,
                         help="Source/ingest directory (can repeat)")
    build_p.add_argument("--archive", type=str, action="append", default=None,
                         help="Archive/organized directory (can repeat)")
    build_p.add_argument("--skip-prefix", type=str, action="append", default=None,
                         help="Skip dirs with this prefix in archive roots (can repeat; default: 00)")
    build_p.add_argument("--encode", action="store_true",
                         help="Encode images for CNN-based dedup (slow on first run)")
    build_p.add_argument("--rebuild-encodings", action="store_true",
                         help="Re-encode all images from scratch")
    build_p.add_argument("--rescan", action="store_true",
                         help="Re-process all already-cataloged files")
    build_p.add_argument("--rescan-incomplete", action="store_true",
                         help="Re-process files with missing phash or EXIF")


    report_p = cat_sub.add_parser("report", help="Print catalog summary")
    report_p.add_argument("--dups", type=int, default=0,
                          help="Show top N duplicate groups")

    query_p = cat_sub.add_parser("query", help="Query the catalog")
    query_p.add_argument("--limit", type=int, default=20,
                         help="Max rows (0 = unlimited)")
    query_p.add_argument("--where", type=str, default=None,
                         help="SQL WHERE clause (e.g. 'exif_date IS NULL')")
    query_p.add_argument("--order", type=str, default="size DESC, path ASC",
                         help="ORDER BY clause (default: size DESC, path ASC)")
    query_p.add_argument("--format", choices=["table", "csv"], default="table",
                         help="Output format")
    query_p.add_argument("--dates", action="store_true",
                         help="Show all date columns (name_date, exif_original, exif_digitized, video_creation)")

    verify_p = cat_sub.add_parser("verify", help="Check for and remove stale catalog entries")

    # dedup commands
    dedup = sub.add_parser("dedup", help="Near-duplicate detection")
    dedup.add_argument("--dups-dir", type=str, default="~/.bubblepix/00DUPLICATES",
                       help="Directory for moved duplicates (default: ~/.bubblepix/00DUPLICATES)")
    dedup_sub = dedup.add_subparsers(dest="subcommand", required=True)

    find_p = dedup_sub.add_parser("find", help="Find duplicate groups")
    find_p.add_argument("--method", choices=["sha256", "phash", "cnn"],
                        default="sha256",
                        help="Detection method: sha256 (exact), phash (altered), "
                             "cnn (similar); each includes lower methods (default: sha256)")
    find_p.add_argument("--threshold", type=float, default=0.75,
                        help="CNN similarity threshold (default: 0.75)")
    find_p.add_argument("--limit", type=int, default=0,
                        help="Max ungrouped images for CNN (0 = unlimited)")

    review_p = dedup_sub.add_parser("review", help="Review found pairs interactively")
    review_p.add_argument("--limit", type=int, default=20,
                          help="Max pairs to review")

    resolve_p = dedup_sub.add_parser("resolve", help="Move resolved duplicates to dups-dir")
    resolve_p.add_argument("--dry-run", action="store_true",
                           help="Show what would be moved")

    args = parser.parse_args()

    if args.command == "catalog":
        if args.subcommand == "build":
            from bubblepix.catalog import CatalogBuilder
            from bubblepix.dedup.engine import MODEL, encode_unencoded_images
            from bubblepix.catalog.db import CatalogDB
            db = CatalogDB()
            if args.rebuild_encodings:
                print("Removing stored encodings...")
                db.delete_encodings(MODEL)
                db.commit()
            builder = CatalogBuilder(dry_run=args.dry_run,
                                      ingest_dirs=args.ingest,
                                      archive_dirs=args.archive,
                                      limit=args.limit, workers=args.workers,
                                      skip_prefixes=tuple(args.skip_prefix) if args.skip_prefix else None,
                                      rescan=args.rescan,
                                      rescan_incomplete=args.rescan_incomplete)
            builder.run()
            if args.encode and not args.dry_run:
                encode_unencoded_images(db)
        elif args.subcommand == "report":
            from bubblepix.catalog import CatalogReport
            CatalogReport(show_dups=args.dups).run()
        elif args.subcommand == "query":
            from bubblepix.catalog import CatalogQuery
            CatalogQuery(limit=args.limit, where=args.where,
                         order=args.order, fmt=args.format,
                         show_dates=args.dates).run()
        elif args.subcommand == "verify":
            from bubblepix.catalog.db import CatalogDB
            db = CatalogDB()
            stale = db.verify(prune=True)
            if stale:
                print(f"{stale:,} stale entries found — tombstoned")
            else:
                print("All catalog entries exist — no stale entries")

    elif args.command == "dedup":
        from bubblepix.catalog.db import CatalogDB
        db = CatalogDB()

        dups_dir = os.path.expanduser(args.dups_dir)

        def _move_file(fpath: str):
            if not os.path.exists(fpath):
                return False
            os.makedirs(dups_dir, exist_ok=True)
            dest = os.path.join(dups_dir, os.path.basename(fpath))
            if os.path.exists(dest):
                base, ext = os.path.splitext(os.path.basename(fpath))
                dest = os.path.join(dups_dir, f"{base}_{hash(fpath) & 0xFFFF}{ext}")
            shutil.move(fpath, dest)
            db.conn.execute(
                "UPDATE catalog SET path = ?, moved_to = ? WHERE path = ?",
                (dest, dest, fpath),
            )
            return True

        if args.subcommand == "find":
            from bubblepix.dedup import DedupEngine
            engine = DedupEngine(threshold=args.threshold, dups_dir=args.dups_dir)
            total = engine.find(db, method=args.method, cnn_limit=args.limit)
            from bubblepix.dedup.engine import MODEL
            encoded = db.encoding_count(MODEL)
            print(f"  {encoded:,} images encoded")
            print(f"Stored {total:,} duplicate groups in catalog")

        elif args.subcommand == "review":
            cur = db.conn.execute("""
                SELECT g.id, g.group_type,
                       COUNT(*) as file_count,
                       SUM(CASE WHEN f.action = 'move' THEN 1 ELSE 0 END) as move_count,
                       SUM(CASE WHEN f.action = 'move' THEN c.size ELSE 0 END) as move_bytes,
                       MAX(CASE WHEN c.source_type = 'ingest' THEN 1 ELSE 0 END) as has_ingest
                FROM dedup_groups g
                JOIN dedup_group_files f ON f.group_id = g.id
                JOIN catalog c ON c.path = f.file_path
                WHERE f.reviewed = 0
                GROUP BY g.id
                ORDER BY
                  CASE g.group_type
                    WHEN 'sha256' THEN 0 WHEN 'phash' THEN 1 WHEN 'cnn' THEN 2 ELSE 3
                  END,
                  has_ingest DESC,
                  move_bytes DESC
                LIMIT ?
            """, (args.limit,))
            rows = cur.fetchall()
            if not rows:
                print("No unreviewed groups.")
                return
            can_gui = bool(os.environ.get("DISPLAY")) and shutil.which("feh")
            print(f"{len(rows)} unreviewed groups")
            print("  MOVE: worse score (less organized, smaller size)")
            print("  KEEP: best score (most organized, largest size)")
            if can_gui:
                print("  Images open in feh (cycle with right-arrow)")
            for gid, group_type, fcount, mcount, mbytes, has_ingest in rows:
                cur2 = db.conn.execute("""
                    SELECT f.id, f.file_path, f.is_original, f.similarity,
                           f.action, c.size, c.has_exif, c.exif_date,
                           c.exif_camera, c.exif_gps_lat
                    FROM dedup_group_files f
                    JOIN catalog c ON c.path = f.file_path
                    WHERE f.group_id = ?
                    ORDER BY f.is_original DESC, f.similarity DESC
                """, (gid,))
                files = cur2.fetchall()
                feh_proc = None
                save_mb = mbytes // (1024*1024)
                print(f"\nGroup #{gid} ({group_type}, {fcount} files, {mcount} to move, ~{save_mb}MB saved)")
                file_entries = []
                for i, (fid, fpath, is_orig, sim, action, csize,
                        has_exif, exif_date, exif_camera, exif_gps_lat) in enumerate(files, 1):
                    cur_disk = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                    size_str = f"{csize // 1024:,}KB" if csize else "0KB"
                    meta = ""
                    if has_exif:
                        parts = []
                        if exif_date:
                            parts.append("DT")
                        if exif_camera:
                            cam = exif_camera.split()[0] if exif_camera else ""
                            if cam:
                                parts.append(cam)
                        if exif_gps_lat is not None:
                            parts.append("GPS")
                        if parts:
                            meta = " [" + "/".join(parts) + "]"
                    sim_val = None
                    if sim is not None:
                        if isinstance(sim, bytes):
                            sim_val = struct.unpack("f", sim)[0]
                        else:
                            sim_val = float(sim)
                    sim_str = f" sim={sim_val:.3f}" if sim_val is not None else ""
                    stale = " [STALE]" if cur_disk != csize else ""
                    print(f" {i:2d}) {os.path.basename(fpath):40s}  {size_str:>10}{meta}{sim_str}{stale}  {fpath}")
                    file_entries.append((fid, is_orig))
                if can_gui:
                    paths = [f[1] for f in files if os.path.exists(f[1])]
                    if paths:
                        feh_proc = subprocess.Popen(
                            ["feh", "--scale-down", "--geometry", "800x600"] + paths,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                else:
                    print(f"  feh {' '.join(f[1] for f in files)}")
                orig_nums = {i for i, (_, is_orig) in enumerate(file_entries, 1) if is_orig}
                while True:
                    answer = input("  Keep (numbers, comma-sep), s=skip, x=exit: ").strip().lower()
                    if answer in ("s", "x") or (answer and all(c.isdigit() or c in " ," for c in answer)):
                        break
                if feh_proc:
                    feh_proc.kill()
                if answer == "x":
                    break
                if answer == "s":
                    continue
                try:
                    keep_ids = {file_entries[int(n)-1][0]
                                for n in answer.split(",") if n.strip().isdigit()}
                except (ValueError, IndexError):
                    print("  Invalid selection, skipping group.")
                    continue
                    moved = 0
                    for fid, fpath, *_ in files:
                        new_action = "keep" if fid in keep_ids else "move"
                        db.conn.execute("""
                            UPDATE dedup_group_files
                            SET action=?, reviewed=1 WHERE id=?
                        """, (new_action, fid))
                        if new_action == "move" and _move_file(fpath):
                            moved += 1
                            db.conn.execute("""
                                UPDATE dedup_group_files
                                SET action='moved' WHERE id=?
                            """, (fid,))
                    db.commit()
                    print(f"  Moved {moved} file(s) to {dups_dir}")

        elif args.subcommand == "resolve":
            cur = db.conn.execute("""
                SELECT f.id, f.file_path
                FROM dedup_group_files f
                JOIN dedup_groups g ON g.id = f.group_id
                WHERE f.action = 'move'
                  AND f.reviewed = 1
            """)
            move_files = cur.fetchall()
            if not move_files:
                print("No resolved duplicates to move.")
                return
            if args.dry_run:
                print(f"Would move {len(move_files):,} files to {dups_dir}:")
                for fid, fpath in move_files:
                    print(f"  {fpath}")
                return
            moved = 0
            for fid, fpath in move_files:
                if _move_file(fpath):
                    moved += 1
                    db.conn.execute(
                        "UPDATE dedup_group_files SET action='moved' WHERE id=?",
                        (fid,),
                    )
            db.commit()
            print(f"Moved {moved} file(s) to {dups_dir}.")

def main_entry():
    try:
        main()
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(130)
    finally:
        logging.info("=== end ===")

if __name__ == "__main__":
    main_entry()
