import sys
from bubblepix.catalog.db import CatalogDB

USE_COLOR = sys.stdout.isatty()


def ansi(code, text):
    if USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text


class CatalogReport:
    def __init__(self, show_dups: int = 0):
        self.show_dups = show_dups
        self.db = CatalogDB()

    def run(self):
        print(ansi("1", "BubblePix — Catalog Report"))
        print()

        s = self.db.summary()
        gb = s["total_bytes"] / (1024**3)
        print(f"  Files:       {s['total']:>9,}")
        print(f"  With date:   {s['with_date']:>9,}")
        print(f"  Hashed:      {s['hashed']:>9,}")
        print(f"  Size:        {gb:>8.1f} GB")
        print()

        dups = self.db.dup_groups()
        print(f"  Duplicate groups: {len(dups):,}")

        if self.show_dups > 0 and dups:
            rows = []
            for h, cnt, paths in dups[:self.show_dups]:
                first = paths.split("|")[0]
                rows.append([h[:16], str(cnt), first])
            widths = [max(len(r[i]) for r in rows) for i in range(3)]
            widths[0] = max(widths[0], 16)
            headers = ["HASH", "CNT", "FIRST PATH"]
            widths = [max(len(h), w) for h, w in zip(headers, widths)]
            print("   " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
            for r in rows:
                print("   " + "  ".join(r[i].ljust(widths[i]) for i in range(3)))
            print()

        orphans = self.db.orphan_files()
        print(f"  Orphans (no EXIF): {len(orphans):,}")
        self.db.close()
