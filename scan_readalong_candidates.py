#!/usr/bin/env python3
"""Stage 1: Library Scanner.

Recursively scans AUDIOBOOK_ROOT for read-along candidates and maintains
three tracking files incrementally:

  * potential-read-alongs.txt  -> folder contains both audiobook + ebook
  * needs_ebook.txt            -> folder contains audiobook only
  * needs_abook.txt            -> folder contains ebook only

Existing statuses (NEW / CREATED / COMPLETED / IGNORED) plus any ``job_id``
are preserved across rescans; entries whose folders disappeared are removed.

Folder identification relies strictly on file contents and folder hierarchy -
never on publication years in folder names or fixed folder depths.  Only files
directly inside a folder are considered; parent folders that only contain
subfolders are not treated as a book.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    AUDIOBOOK_ROOT,
    LOGS_DIR,
    MANAGER_DIR,
    NEEDS_ABOOK_FILE,
    NEEDS_EBOOK_FILE,
    POTENTIAL_FILE,
)

AUDIO_EXTENSIONS = {".m4b", ".mp3", ".opus"}
EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3"}

STATUS_NEW = "NEW"
STATUS_CREATED = "CREATED"
STATUS_COMPLETED = "COMPLETED"
STATUS_IGNORED = "IGNORED"

LOCK_FILE = MANAGER_DIR / "scanner.lock"

POTENTIAL_HEADER = """# Potential Read-Along Candidates
# [NEW]       Not submitted
# [CREATED]   Job accepted by AudioSilo
# [COMPLETED] Job finished successfully
# [IGNORED]   Manually excluded"""

NEEDS_EBOOK_HEADER = "# Audiobooks without matching ebooks"
NEEDS_ABOOK_HEADER = "# Ebooks without matching audiobooks"


class Logger:
    def __init__(self, log_path: Path):
        self.path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line)


def parse_tracking_file(path: Path) -> Dict[str, dict]:
    """Return {relative_folder: {"status": ..., "job_id": ... | None}}."""
    entries: Dict[str, dict] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        if len(parts) < 2:
            continue
        status_tok = parts[0]
        if not (status_tok.startswith("[") and status_tok.endswith("]")):
            continue
        status = status_tok[1:-1]
        folder = parts[1]
        job_id: Optional[str] = None
        for extra in parts[2:]:
            extra = extra.strip()
            if extra.startswith("job_id="):
                job_id = extra[len("job_id="):].strip() or None
        entries[folder] = {"status": status, "job_id": job_id}
    return entries


def write_tracking_file(
    path: Path,
    entries: Dict[str, dict],
    header: str,
    dry_run: bool,
    logger: Logger,
) -> None:
    """Atomically write a tracking file (temp file + rename)."""
    lines = [header, ""]
    for folder in sorted(entries.keys()):
        info = entries[folder]
        status = info["status"]
        line = f"[{status}] | {folder}"
        if info.get("job_id"):
            line += f" | job_id={info['job_id']}"
        lines.append(line)
    content = "\n".join(lines) + "\n"

    if dry_run:
        logger.log(f"[dry-run] would write {len(entries)} entries to {path.name}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    logger.log(f"Wrote {len(entries)} entries to {path.name}")


def classify_folder(folder: Path) -> Optional[str]:
    """Return 'both', 'audio', 'ebook', or None for the given folder.

    None means the folder is not a book (no directly-classifiable files or
    only subfolders).
    """
    try:
        direct_files = [f for f in folder.iterdir() if f.is_file()]
    except OSError:
        return None
    if not direct_files:
        return None  # parent folder holding only subfolders -> not a book

    has_audio = any(f.suffix.lower() in AUDIO_EXTENSIONS for f in direct_files)
    has_ebook = any(f.suffix.lower() in EBOOK_EXTENSIONS for f in direct_files)

    if has_audio and has_ebook:
        return "both"
    if has_audio:
        return "audio"
    if has_ebook:
        return "ebook"
    return None


def discover(root: Path) -> Dict[str, str]:
    """Walk root and return {relative_folder_posix: classification}."""
    discovered: Dict[str, str] = {}
    for dirpath, _dirnames, _filenames in os.walk(root):
        dir_path = Path(dirpath)
        rel = dir_path.relative_to(root).as_posix()
        classification = classify_folder(dir_path)
        if classification:
            discovered[rel] = classification
    return discovered


def run(dry_run: bool = False) -> int:
    logger = Logger(LOGS_DIR / "scanner.log")

    # Single-instance lock to protect shared tracking files.
    MANAGER_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.log("Another scanner instance is already running. Exiting.")
        os.close(lock_fd)
        return 1

    try:
        return _scan(dry_run, logger)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _scan(dry_run: bool, logger: Logger) -> int:
    start = time.time()
    logger.log(f"Scan started. Root: {AUDIOBOOK_ROOT}")

    if not AUDIOBOOK_ROOT.exists():
        logger.log(f"ERROR: AUDIOBOOK_ROOT does not exist: {AUDIOBOOK_ROOT}")
        return 1

    discovered = discover(AUDIOBOOK_ROOT)
    logger.log(f"Folders examined: {len(discovered)}")

    # --- potential-read-alongs (both) -------------------------------------
    potential_existing = parse_tracking_file(POTENTIAL_FILE)
    potential_entries: Dict[str, dict] = {}
    for rel, cls in discovered.items():
        if cls != "both":
            continue
        if rel in potential_existing:
            potential_entries[rel] = potential_existing[rel]  # preserve
        else:
            potential_entries[rel] = {"status": STATUS_NEW, "job_id": None}

    # --- needs_ebook (audio only) ----------------------------------------
    needs_ebook_existing = parse_tracking_file(NEEDS_EBOOK_FILE)
    needs_ebook_entries: Dict[str, dict] = {}
    for rel, cls in discovered.items():
        if cls != "audio":
            continue
        if rel in needs_ebook_existing:
            needs_ebook_entries[rel] = needs_ebook_existing[rel]
        else:
            needs_ebook_entries[rel] = {"status": "NEEDS_EBOOK", "job_id": None}

    # --- needs_abook (ebook only) -----------------------------------------
    needs_abook_existing = parse_tracking_file(NEEDS_ABOOK_FILE)
    needs_abook_entries: Dict[str, dict] = {}
    for rel, cls in discovered.items():
        if cls != "ebook":
            continue
        if rel in needs_abook_existing:
            needs_abook_entries[rel] = needs_abook_existing[rel]
        else:
            needs_abook_entries[rel] = {"status": "NEEDS_ABOOK", "job_id": None}

    # Counts for the summary report.
    potential_new = sum(1 for r in potential_entries if r not in potential_existing)
    potential_preserved = len(potential_entries) - potential_new
    needs_ebook_new = sum(1 for r in needs_ebook_entries if r not in needs_ebook_existing)
    needs_ebook_preserved = len(needs_ebook_entries) - needs_ebook_new
    needs_abook_new = sum(1 for r in needs_abook_entries if r not in needs_abook_existing)
    needs_abook_preserved = len(needs_abook_entries) - needs_abook_new

    # Write tracking files atomically.
    write_tracking_file(POTENTIAL_FILE, potential_entries, POTENTIAL_HEADER, dry_run, logger)
    write_tracking_file(NEEDS_EBOOK_FILE, needs_ebook_entries, NEEDS_EBOOK_HEADER, dry_run, logger)
    write_tracking_file(NEEDS_ABOOK_FILE, needs_abook_entries, NEEDS_ABOOK_HEADER, dry_run, logger)

    duration = time.time() - start
    logger.log("=" * 46)
    logger.log("             SCAN COMPLETE")
    logger.log("=" * 46)
    logger.log(f"Folders examined:          {len(discovered)}")
    logger.log(f"Potential read-alongs:     {len(potential_entries)}")
    logger.log(f"Needs ebook:               {len(needs_ebook_entries)}")
    logger.log(f"Needs audiobook:           {len(needs_abook_entries)}")
    logger.log(f"New candidates added:      {potential_new}")
    logger.log(f"Existing candidates:       {potential_preserved}")
    logger.log(f"Existing statuses preserved:{potential_preserved}")
    logger.log(f"Scan duration:             {time.strftime('%H:%M:%S', time.gmtime(duration))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 1: Library Scanner")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview scanning without modifying tracking files",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

