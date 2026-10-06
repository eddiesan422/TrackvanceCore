"""Restore committed POSIX file modes after copying a Windows Git checkout."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def restore_source_modes(root: Path) -> dict:
    root = root.resolve(strict=True)
    entries = subprocess.check_output(["git", "-C", str(root), "ls-files", "--stage", "-z"])
    pending = []
    links = 0
    excluded_outputs = []
    ignore_file = root / ".dockerignore"
    excludes_outputs = ignore_file.is_file() and "outputs" in ignore_file.read_text(encoding="utf-8").splitlines()
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, _object_id, stage = metadata.split()
        if stage != b"0" or mode not in {b"100644", b"100755", b"120000"}:
            raise ValueError("Unknown or unmerged committed file mode")
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
            raise ValueError("Committed path escapes the source checkout")
        path = root / relative
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError("Committed path parent escapes the source checkout")
        try:
            current = path.lstat()
        except FileNotFoundError:
            # The committed Docker context deliberately omits example outputs.
            # Missing source, tests or any other tracked path remains an error.
            if excludes_outputs and relative.parts[0] == "outputs":
                excluded_outputs.append(relative.as_posix())
                continue
            raise
        if mode == b"120000":
            if not stat.S_ISLNK(current.st_mode):
                raise ValueError("Committed symlink was materialized as another file type")
            links += 1
            continue  # Never chmod a symlink or follow its target.
        if not stat.S_ISREG(current.st_mode):
            raise ValueError("Committed regular file is not a regular file")
        pending.append((path, relative.as_posix(), 0o755 if mode == b"100755" else 0o644))
    # Validate the entire index before changing any file.
    for path, _relative, mode in pending:
        os.chmod(path, mode, follow_symlinks=False)
        if stat.S_IMODE(path.lstat().st_mode) != mode:
            raise ValueError("Committed file mode was not restored")
    digest = hashlib.sha256()
    for _path, relative, mode in sorted(pending, key=lambda row: row[1]):
        digest.update(f"{mode:o}\t{relative}\n".encode("utf-8", errors="surrogateescape"))
    return {"status": "PASS", "regular_files": len(pending),
        "executable_files": sum(mode == 0o755 for _, _, mode in pending),
        "symlinks_untouched": links, "committed_modes_sha256": digest.hexdigest(),
        "copy_excluded_tracked_outputs": len(excluded_outputs),
        "copy_excluded_paths_sha256": hashlib.sha256("\n".join(sorted(excluded_outputs)).encode("utf-8")).hexdigest(),
        "scope": "Git-index regular files only; no symlink targets or untracked files"}


if __name__ == "__main__":
    if os.name != "posix":
        raise SystemExit("Committed POSIX mode restoration requires Linux")
    print(json.dumps(restore_source_modes(Path(__file__).resolve().parents[2]), sort_keys=True))
