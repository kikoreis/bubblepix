import csv
import io
import sys
from bubblepix.catalog.db import CatalogDB

USE_COLOR = sys.stdout.isatty()

BASE_COLS = [
    ("filename",    "FILE"),
    ("exif_date",   "DATE"),
    ("exif_camera", "CAMERA"),
    ("exif_width",  "W"),
    ("exif_height", "H"),
    ("size",        "SIZE"),
    ("exif_gps_lat","LAT"),
    ("exif_gps_lon","LON"),
    ("source_rel",  "SOURCE"),
    ("extension",   "EXT"),
    ("sha256",      "HASH"),
    ("tier",        "TIER"),
]

DATE_COLS = [
    ("name_date",            "NAME_DATE"),
    ("exif_original_date",   "ORIG_DATE"),
    ("exif_digitized_date",  "DIG_DATE"),
    ("exif_modify_date",     "MOD_DATE"),
    ("video_creation_date",  "VID_DATE"),
]


def fmt_size(b):
    b = b or 0
    if b < 1024:     return f"{b}B"
    if b < 1024**2:  return f"{b//1024}K"
    if b < 1024**3:  return f"{b/(1024**2):.1f}M"
    return f"{b/(1024**3):.2f}G"


def ansi(code, text):
    if USE_COLOR:
        return f"\033[{code}m{text}\033[0m"
    return text


def fmt_val(col, v):
    if v is None:
        return "-"
    if col == "size":
        return fmt_size(v)
    if col == "sha256":
        return str(v)[:16]
    return str(v)


class CatalogQuery:
    def __init__(self, limit: int = 20, where: str | None = None,
                 order: str = "size DESC", fmt: str = "table",
                 show_dates: bool = False):
        self.limit = limit
        self.where = where
        self.order = order
        self.fmt = fmt
        self.show_dates = show_dates
        self.db = CatalogDB()

    @property
    def cols(self):
        return BASE_COLS + (DATE_COLS if self.show_dates else [])

    def run(self):
        cols = self.cols
        sql_cols = ", ".join(c for c, _ in cols)
        labels = [l for _, l in cols]

        sql = f"SELECT {sql_cols} FROM catalog"
        if self.where:
            sql += f" WHERE {self.where}"
        sql += f" ORDER BY {self.order}"
        if self.limit > 0:
            sql += f" LIMIT {self.limit}"

        try:
            rows = self.db.conn.execute(sql).fetchall()
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            self.db.close()
            return

        if not rows:
            print("(no results)")
            self.db.close()
            return

        if self.fmt == "csv":
            self._dump_csv(rows, labels)
        else:
            self._dump_table(rows, labels)
        self.db.close()

    def _dump_table(self, rows, labels):
        cells = []
        for r in rows:
            cells.append([fmt_val(self.cols[i][0], r[i]) for i in range(len(labels))])

        widths = [max(len(l), *(len(c[i]) for c in cells)) for i, l in enumerate(labels)]

        def emit(vals, codes=None):
            codes = codes or [None] * len(vals)
            parts = []
            for i, raw in enumerate(vals):
                pad = " " * (widths[i] - len(raw))
                txt = ansi(codes[i], raw) if codes[i] else raw
                parts.append(txt + pad)
            print("  ".join(parts).rstrip())

        emit(labels, ["1;38;5;39"] * len(labels))
        for row_cells in cells:
            emit(row_cells)

        print(ansi("90", f"\n{len(rows)} row(s)"))

    def _dump_csv(self, rows, labels):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(labels)
        for r in rows:
            w.writerow(str(v or "") for v in r)
        sys.stdout.write(out.getvalue())
