import argparse
import os
import shutil
import struct
import subprocess
import sys


def main():
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

    verify_p = cat_sub.add_parser("verify", help="Check for stale catalog entries")
    verify_p.add_argument("--prune", action="store_true",
                          help="Delete stale entries from catalog")

    # dedup commands
    dedup = sub.add_parser("dedup", help="Near-duplicate detection")
    dedup.add_argument("--dups-dir", type=str, default="~/.bubblepix/00DUPLICATES",
                       help="Directory for moved duplicates (default: ~/.bubblepix/00DUPLICATES)")
    dedup_sub = dedup.add_subparsers(dest="subcommand", required=True)

    find_p = dedup_sub.add_parser("find", help="Find near-duplicate groups")
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
                                      skip_prefixes=tuple(args.skip_prefix) if args.skip_prefix else None)
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
            stale = db.verify(prune=args.prune)
            if stale:
                msg = f"{stale:,} stale entries found"
                if args.prune:
                    msg += " — tombstoned"
                else:
                    msg += " (re-run with --prune to tombstone them)"
                print(msg)
                if not args.prune:
                    print("  Re-run with --prune to tombstone them")
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

            total_groups = 0

            print("Finding phash-based near-duplicates...")
            phash_groups = engine.find_phash_groups(db)
            print(f"  Found {len(phash_groups):,} phash groups")
            engine.store_groups(db, phash_groups, "phash")
            total_groups += len(phash_groups)

            print("Finding CNN-based near-duplicates (hub clustering)...")
            cnn_groups = engine.find_cnn_groups_all_images(db, limit=args.limit)
            print(f"  Found {len(cnn_groups):,} CNN groups")
            if cnn_groups:
                engine.store_groups(db, cnn_groups, "cnn")
                total_groups += len(cnn_groups)

            from bubblepix.dedup.engine import MODEL
            encoded = db.encoding_count(MODEL)
            print(f"  {encoded:,} images encoded")
            print(f"Stored {total_groups:,} near-duplicate groups in catalog")

        elif args.subcommand == "review":
            cur = db.conn.execute("""
                SELECT g.id, g.group_type,
                       COUNT(*) as file_count,
                       SUM(CASE WHEN f.action = 'move' THEN 1 ELSE 0 END) as move_count,
                       SUM(CASE WHEN f.action = 'move' THEN c.size ELSE 0 END) as move_bytes
                FROM dedup_groups g
                JOIN dedup_group_files f ON f.group_id = g.id
                JOIN catalog c ON c.path = f.file_path
                WHERE f.reviewed = 0
                GROUP BY g.id
                ORDER BY move_bytes DESC
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
            for gid, group_type, fcount, mcount, mbytes in rows:
                cur2 = db.conn.execute("""
                    SELECT id, file_path, is_original, similarity, action
                    FROM dedup_group_files
                    WHERE group_id = ?
                    ORDER BY is_original DESC, similarity DESC
                """, (gid,))
                files = cur2.fetchall()
                feh_proc = None
                save_mb = mbytes // (1024*1024)
                print(f"\nGroup #{gid} ({group_type}, {fcount} files, {mcount} to move, ~{save_mb}MB saved)")
                for fid, fpath, is_orig, sim, action in files:
                    tag = "KEEP" if is_orig else "MOVE"
                    fsize = os.path.getsize(fpath) if os.path.exists(fpath) else 0
                    sim_val = None
                    if sim is not None:
                        if isinstance(sim, bytes):
                            sim_val = struct.unpack("f", sim)[0]
                        else:
                            sim_val = float(sim)
                    sim_str = f" sim={sim_val:.3f}" if sim_val is not None else ""
                    print(f"  [{tag}] {os.path.basename(fpath):40s}  {fsize//1024:>8}KB{sim_str}  {fpath}")
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
                while True:
                    answer = input("  Confirm (c), Override (o N,M,... keep those), Skip (s), Exit (x)? ").strip().lower()
                    if answer in ("c", "s", "x") or answer.startswith("o"):
                        break
                if feh_proc:
                    feh_proc.kill()
                if answer == "x":
                    break
                if answer == "s":
                    continue
                if answer in ("c",) or answer.startswith("o"):
                    if answer == "c":
                        keep_ids = {f[0] for f in files if f[2]}
                    else:
                        try:
                            keep_ids = {int(x.strip()) for x in answer[1:].split(",") if x.strip()}
                        except ValueError:
                            print("  Invalid selection, skipping group.")
                            continue
                    moved = 0
                    for fid, fpath, is_orig, sim, action in files:
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


if __name__ == "__main__":
    main()
