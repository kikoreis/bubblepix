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


def _move_file(db, dups_dir, fpath: str):
    if not os.path.exists(fpath):
        print(f"  [SKIP] not found: {fpath}")
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
    print(f"  [MOVE] {os.path.basename(fpath)} → {dest}")
    return True


def _parse_review_answer(answer: str, labels: list[str]
                         ) -> tuple[set[str], list[tuple[str, str]]] | None:
    try:
        raw_tokens = [t.strip() for t in answer.replace(",", " ").split() if t.strip()]
        if not raw_tokens:
            return None
        keeps: set[str] = set()
        replaces: list[tuple[str, str]] = []
        for tok in raw_tokens:
            if "<" in tok:
                parts = tok.split("<")
                if len(parts) != 2:
                    return None
                victim, winner = parts[0], parts[1]
            elif ">" in tok:
                parts = tok.split(">")
                if len(parts) != 2:
                    return None
                winner, victim = parts[0], parts[1]
            else:
                keeps.add(tok)
                continue
            if len(victim) != 1 or len(winner) != 1:
                return None
            if not victim.isalpha() or not winner.isalpha():
                return None
            replaces.append((victim, winner))
        all_mentioned = keeps | {v for v, _ in replaces} | {w for _, w in replaces}
        if not all(c in labels for c in all_mentioned):
            return None
        if len(all_mentioned) != len(keeps) + 2 * len(replaces):
            return None
        return (keeps, replaces)
    except (ValueError, IndexError):
        return None


def _review_group(db, gid, group_type, fcount, mcount, mbytes, has_ingest,
                   dups_dir, can_gui, group_count):
    cur = db.conn.execute("""
        SELECT f.id, f.file_path, f.is_original, f.similarity,
               f.action, c.size, c.has_exif, c.exif_date,
               c.exif_camera, c.exif_gps_lat
        FROM dedup_group_files f
        JOIN catalog c ON c.path = f.file_path
        WHERE f.group_id = ? AND c.tombstone = 0
        ORDER BY f.is_original DESC, f.similarity DESC
    """, (gid,))
    files = cur.fetchall()
    feh_proc = None
    save_mb = mbytes // (1024*1024)
    print(f"\nGroup #{gid} ({group_type}, {fcount} files, {mcount} to move, ~{save_mb}MB saved)")
    file_entries = []
    labels = [chr(ord('a') + i) for i in range(len(files))]
    for i, (fid, fpath, is_orig, sim, action, csize,
            has_exif, exif_date, exif_camera, exif_gps_lat) in enumerate(files):
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
        print(f" {labels[i]:>2s}) {os.path.basename(fpath):40s}  {size_str:>10}{meta}{sim_str}{stale}  {fpath}")
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
    while True:
        answer = input("  Keep/replace (e.g. a<b), n=jump, s=skip, x=exit: ").strip().lower()
        if answer in ("s", "x"):
            break
        if answer.isdigit():
            n = int(answer)
            if 1 <= n <= group_count:
                break
        if _parse_review_answer(answer, labels) is not None:
            break
    if feh_proc:
        feh_proc.kill()
    if answer == "x":
        return False
    if answer == "s":
        return None
    if answer.isdigit():
        return int(answer)
    keeps, replaces = _parse_review_answer(answer, labels)

    moved = 0
    replaced = 0

    # Phase 1: replaces — move winner to victim's path
    handles = set()
    for victim_label, winner_label in replaces:
        vi = labels.index(victim_label)
        wi = labels.index(winner_label)
        v_fid, v_path, *_ = files[vi]
        w_fid, w_path, *_ = files[wi]
        handles.update([victim_label, winner_label])

        if not os.path.exists(w_path):
            print(f"  [SKIP] winner not found: {w_path}")
            db.conn.execute(
                "UPDATE dedup_group_files SET action='skip', reviewed=1 WHERE id=?",
                (w_fid,))
            continue

        # Move victim out of the way
        _move_file(db, dups_dir, v_path)
        db.conn.execute(
            "UPDATE dedup_group_files SET action='moved', reviewed=1 WHERE id=?",
            (v_fid,))

        # Move winner to victim's old path
        shutil.move(w_path, v_path)
        db.conn.execute(
            "UPDATE catalog SET path = ? WHERE path = ?",
            (v_path, w_path))
        db.conn.execute(
            "UPDATE dedup_group_files SET file_path = ?, action='keep', reviewed=1 WHERE id=?",
            (v_path, w_fid))
        replaced += 1
        print(f"  [REPLACE] {os.path.basename(w_path)} → {v_path}")

    # Phase 2: plain keeps
    for keep_label in keeps:
        handles.add(keep_label)
        ki = labels.index(keep_label)
        k_fid = files[ki][0]
        db.conn.execute(
            "UPDATE dedup_group_files SET action='keep', reviewed=1 WHERE id=?",
            (k_fid,))

    # Phase 3: everything else → dups
    for i, (fid, fpath, *_) in enumerate(files):
        if labels[i] in handles:
            continue
        if _move_file(db, dups_dir, fpath):
            moved += 1
            db.conn.execute(
                "UPDATE dedup_group_files SET action='moved', reviewed=1 WHERE id=?",
                (fid,))

    db.commit()
    if replaced:
        print(f"  Replaced {replaced}, moved {moved} file(s) to {dups_dir}")
    else:
        print(f"  Moved {moved} file(s) to {dups_dir}")
    return None




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
    review_p.add_argument("group_ids", nargs="*", type=int, default=None,
                          help="Specific group IDs to review")
    review_p.add_argument("--no-feh", action="store_true",
                          help="Disable the feh image viewer")
    review_p.add_argument("--filter", type=str, default=None,
                          help="Only review groups with files matching text in path or camera model")
    review_p.add_argument("--method", type=str, default=None,
                          choices=["sha256", "phash", "cnn"],
                          help="Only review groups of this type")

    list_p = dedup_sub.add_parser("list", help="List duplicate groups")
    list_p.add_argument("--limit", type=int, default=0,
                        help="Max groups to show (0 = unlimited)")
    list_p.add_argument("filter", nargs="?", default=None,
                        help="Only show groups with files matching this text")

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
        else:
            print("Unrecognized catalog subcommand")

    elif args.command == "dedup":
        from bubblepix.catalog.db import CatalogDB
        db = CatalogDB()

        dups_dir = os.path.expanduser(args.dups_dir)

        if args.subcommand == "find":
            from bubblepix.dedup import DedupEngine
            engine = DedupEngine(threshold=args.threshold, dups_dir=args.dups_dir)
            total = engine.find(db, method=args.method, cnn_limit=args.limit)
            from bubblepix.dedup.engine import MODEL
            encoded = db.encoding_count(MODEL)
            print(f"  {encoded:,} images encoded")
            print(f"Stored {total:,} duplicate groups in catalog")

        elif args.subcommand == "review":
            from bubblepix.dedup import DedupEngine
            if args.group_ids:
                rows = DedupEngine.get_groups_by_ids(db, args.group_ids)
                if not rows:
                    print("No groups found for the given IDs.")
                    return
                print(f"Reviewing {len(rows)} specified group(s)")
            else:
                rows = DedupEngine.get_unreviewed_groups(db, args.limit, args.filter, args.method)
                if not rows:
                    print("No unreviewed groups.")
                    return
                print(f"{len(rows)} unreviewed groups")
            can_gui = (not args.no_feh and bool(os.environ.get("DISPLAY"))
                       and shutil.which("feh"))
            print("  MOVE: worse score (less organized, smaller size)")
            print("  KEEP: best score (most organized, largest size)")
            if can_gui:
                print("  Images open in feh (cycle with right-arrow)")
            i = 0
            while i < len(rows):
                gid, group_type, fcount, mcount, mbytes, has_ingest = rows[i]
                result = _review_group(db, gid, group_type, fcount, mcount, mbytes,
                                       has_ingest, dups_dir, can_gui, len(rows))
                if result is False:
                    break
                if isinstance(result, int):
                    i = result - 1
                else:
                    i += 1

        elif args.subcommand == "list":
            from bubblepix.dedup import DedupEngine
            DedupEngine.list_groups(db, args.limit, args.filter)

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
