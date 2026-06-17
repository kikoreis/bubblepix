import os

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".JPG", ".JPEG", ".png", ".PNG",
    ".heic", ".HEIC", ".webp", ".bmp", ".gif", ".tiff",
    ".mp4", ".MP4", ".mov", ".MOV", ".m4v", ".avi", ".mts",
    ".3gp", ".ppm",
}

# Dir names to always skip (output dirs we create ourselves)
ALWAYS_SKIP: set[str] = set()

# Prefixes to skip only in archive roots (admin/event dirs that shouldn't be re-sorted)
ARCHIVE_SKIP_PREFIXES = ("00",)


class FileWalker:
    def __init__(self, source_roots: list[tuple[str, str]],
                 skip_prefixes: tuple[str, ...] | None = None):
        self.source_roots = [(os.path.expanduser(r), t) for r, t in source_roots]
        self.skip_prefixes = skip_prefixes if skip_prefixes is not None else ARCHIVE_SKIP_PREFIXES

    def _should_skip_dir(self, dirname: str, source_type: str) -> bool:
        if dirname in ALWAYS_SKIP:
            return True
        if source_type == "archive":
            for pfx in self.skip_prefixes:
                if dirname.startswith(pfx):
                    return True
        return False

    @staticmethod
    def _is_media(filename: str) -> bool:
        _, ext = os.path.splitext(filename)
        return ext.lower() in {e.lower() for e in MEDIA_EXTENSIONS}

    def walk(self):
        all_roots = {r for r, _ in self.source_roots}
        exclude_map = {
            r: {o for o in all_roots if o != r and o.startswith(r.rstrip("/") + "/")}
            for r, _ in self.source_roots
        }
        for root, source_type in self.source_roots:
            if os.path.isdir(root):
                yield from self._walk_dir(root, root, source_type, exclude_map[root])
            elif os.path.isfile(root) and self._is_media(os.path.basename(root)):
                parent = os.path.dirname(root)
                yield root, parent, source_type

    def _walk_dir(self, start: str, root: str, source_type: str,
                  exclude_subdirs: set | None = None):
        if exclude_subdirs is None:
            exclude_subdirs = set()
        for dirpath, dirnames, filenames in os.walk(start, topdown=True):
            dirnames[:] = sorted(
                d for d in dirnames
                if not self._should_skip_dir(d, source_type)
                and os.path.join(dirpath, d) not in exclude_subdirs
            )
            for fname in sorted(filenames):
                if self._is_media(fname):
                    yield os.path.join(dirpath, fname), root, source_type
