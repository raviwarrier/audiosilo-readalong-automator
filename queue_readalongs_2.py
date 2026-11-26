#!/usr/bin/env python3
"""Stage 2: Job Queuer (targeted submission variant).

Reads potential-read-alongs.txt and submits eligible jobs to AudioSilo via
POST /api/v1/admin/bridge/jobs.

Batch mode (no --title/--author):
  * Only [NEW] entries from potential-read-alongs.txt are eligible.
  * Batch size is selected interactively or passed as a CLI argument.

Targeted mode (--title or --author):
  * Searches AUDIOBOOK_ROOT directly for folders matching the title/author.
  * For each match: if audio + EPUB are present, submit the alignment job.
  * If audio only: report missing EPUB and add to needs_ebook.txt.
  * If EPUB only: report missing audiobook and add to needs_abook.txt.
  * Skips potential-read-alongs.txt entirely in targeted mode.

--dry-run previews without making API requests or modifying files.
On confirmed HTTP success, [NEW] -> [CREATED] | job_id=<id>.
Waits BATCH_DELAY seconds between submissions.
Clean failures leave the entry as [NEW]; timeouts are never retried.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (  # noqa: E402
    AUDIOSILO_CONTAINER_LIBRARY,
    AUDIOSILO_LIBRARY_ID,
    AUDIOSILO_TOKEN,
    AUDIOSILO_URL,
    AUDIOBOOK_ROOT,
    AUDIO_EXTENSIONS,
    EPUB_EXTENSIONS,
    OTHER_EBOOK_EXTENSIONS,
    API_REQUEST_TIMEOUT,
    BATCH_DELAY,
    BATCH_SIZE,
    LOGS_DIR,
    MANAGER_DIR,
    NEEDS_ABOOK_FILE,
    NEEDS_EBOOK_FILE,
    POTENTIAL_FILE,
)

API_JOBS_ENDPOINT = "/api/v1/admin/bridge/jobs"
API_JOB_KIND = "readalong.align"

LOCK_FILE = MANAGER_DIR / "queuer.lock"


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


class APIError(Exception):
    """Raised for clean (non-timeout) API failures."""


class APITimeoutError(Exception):
    """Raised when a request times out after being sent (uncertain outcome)."""


def api_post_job(
    library_id: int,
    path: str,
    token: str,
    timeout: int,
    dry_run: bool,
    logger: Logger,
) -> Tuple[bool, Optional[str], str]:
    """Submit one alignment job.

    Returns (success, job_id_or_None, detail_message).
    """
    payload = {
        "kind": API_JOB_KIND,
        "library_id": library_id,
        "path": path,
        "params": {},
    }

    if dry_run:
        logger.log(f"[dry-run] POST {API_JOBS_ENDPOINT} {payload}")
        return True, None, "dry-run"

    url = AUDIOSILO_URL.rstrip("/") + API_JOBS_ENDPOINT
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "AudioSilo-ReadAlong-Queue/1.0")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            job_id = _extract_job_id(body)
            return True, job_id, f"HTTP {resp.status}: {body[:200]}"
    except socket.timeout as exc:
        raise APITimeoutError(f"Request timed out after {timeout}s") from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise APIError(f"HTTP {exc.code}: {body[:200]}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            raise APITimeoutError(f"Request timed out after {timeout}s") from exc
        raise APIError(f"Connection error: {exc.reason}") from exc


def _extract_job_id(body: str) -> Optional[str]:
    """Best-effort extraction of a job id from the JSON response body."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        for key in ("job_id", "id", "jobId"):
            if key in data and data[key] is not None:
                return str(data[key])
        for nested in ("data", "result", "job"):
            if isinstance(data.get(nested), dict):
                val = data[nested].get("job_id") or data[nested].get("id")
                if val is not None:
                    return str(val)
    return None


def parse_tracking_file(path: Path) -> List[dict]:
    """Return a list of {"status":..., "folder":..., "job_id":...|None} in file order."""
    rows: List[dict] = []
    if not path.exists():
        return rows
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
        rows.append({"status": status, "folder": folder, "job_id": job_id})
    return rows


def write_tracking_file(
    path: Path,
    rows: List[dict],
    header: str,
    dry_run: bool,
    logger: Logger,
) -> None:
    """Atomically write the tracking file preserving original row order."""
    lines = [header, ""]
    for row in rows:
        status = row["status"]
        line = f"[{status}] | {row['folder']}"
        if row.get("job_id"):
            line += f" | job_id={row['job_id']}"
        lines.append(line)
    content = "\n".join(lines) + "\n"

    if dry_run:
        logger.log(f"[dry-run] would write {len(rows)} entries to {path.name}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    logger.log(f"Wrote {len(rows)} entries to {path.name}")


def read_needs_file(path: Path) -> List[str]:
    """Return the list of folder paths already tracked in a needs file."""
    paths: List[str] = []
    if not path.exists():
        return paths
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        if len(parts) >= 2:
            paths.append(parts[1])
    return paths


def append_to_needs_file(
    path: Path,
    header: str,
    relative_folder: str,
    dry_run: bool,
    logger: Logger,
) -> None:
    """Add a folder to a needs file if not already present (atomic write)."""
    existing = read_needs_file(path)
    if relative_folder in existing:
        logger.log(f"Already in {path.name}: {relative_folder}")
        return

    existing.append(relative_folder)
    lines = [header, ""]
    for entry in sorted(existing):
        tag = "[NEEDS_EBOOK]" if "ebook" in path.name else "[NEEDS_ABOOK]"
        lines.append(f"{tag} | {entry}")
    content = "\n".join(lines) + "\n"

    if dry_run:
        logger.log(f"[dry-run] would add to {path.name}: {relative_folder}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    logger.log(f"Added to {path.name}: {relative_folder}")


def classify_folder_for_targeted(folder: Path) -> Tuple[bool, bool, bool]:
    """Return (has_audio, has_epub, has_other_ebook) for direct files in folder."""
    try:
        direct_files = [f for f in folder.iterdir() if f.is_file()]
    except OSError:
        return False, False, False
    if not direct_files:
        return False, False, False

    has_audio = any(f.suffix.lower() in AUDIO_EXTENSIONS for f in direct_files)
    has_epub = any(f.suffix.lower() in EPUB_EXTENSIONS for f in direct_files)
    has_other_ebook = any(
        f.suffix.lower() in OTHER_EBOOK_EXTENSIONS for f in direct_files
    )
    return has_audio, has_epub, has_other_ebook


def find_matching_folders(
    root: Path,
    title: Optional[str],
    author: Optional[str],
) -> List[Path]:
    """Walk root and return folders matching title and/or author (case-insensitive)."""
    matches: List[Path] = []
    title_lower = title.lower() if title else None
    author_lower = author.lower() if author else None

    for dirpath, _dirnames, _filenames in os.walk(root):
        dir_path = Path(dirpath)
        name_lower = dir_path.name.lower()

        if title_lower and title_lower not in name_lower:
            continue
        if author_lower and author_lower not in name_lower:
            continue

        matches.append(dir_path)

    return sorted(matches)

    return sorted(matches)


def has_active_job(library_id: int, path: str, token: str, timeout: int) -> bool:
    """Check AudioSilo's job-list endpoint for an existing active job.

    Placeholder for Phase 4 — always returns False so submission proceeds.
    """
    return False


def submit_batch(
    rows: List[dict],
    indices: List[int],
    dry_run: bool,
    logger: Logger,
) -> Tuple[int, int, List[int]]:
    """Submit jobs for the given row indices.

    Returns (submitted, failed, uncertain_indices).
    """
    submitted = 0
    failed = 0
    uncertain: List[int] = []

    for idx in indices:
        row = rows[idx]
        folder = row["folder"]

        # Layer 1: local protection (already NEW-only here).
        if row["status"] != "NEW":
            continue

        # Layer 2: server-side duplicate protection.
        if has_active_job(AUDIOSILO_LIBRARY_ID, folder, AUDIOSILO_TOKEN, int(API_REQUEST_TIMEOUT)):
            logger.log(f"SKIP (active job on server): {folder}")
            continue

        print(f"\nSubmitting: {folder}")
        try:
            success, job_id, detail = api_post_job(
                library_id=AUDIOSILO_LIBRARY_ID,
                path=folder,
                token=AUDIOSILO_TOKEN,
                timeout=int(API_REQUEST_TIMEOUT),
                dry_run=dry_run,
                logger=logger,
            )
        except APITimeoutError as exc:
            logger.log(f"TIMEOUT (uncertain): {folder} -> {exc}")
            uncertain.append(idx)
            failed += 1
            time.sleep(int(BATCH_DELAY))
            continue
        except APIError as exc:
            logger.log(f"FAILED (stays NEW): {folder} -> {exc}")
            failed += 1
            time.sleep(int(BATCH_DELAY))
            continue

        if success:
            rows[idx]["status"] = "CREATED"
            if job_id:
                rows[idx]["job_id"] = job_id
            logger.log(f"CREATED: {folder} | job_id={job_id}")
            submitted += 1
        else:
            failed += 1

        if idx != indices[-1]:
            time.sleep(int(BATCH_DELAY))

    return submitted, failed, uncertain


def run(
    batch_size: Optional[int],
    dry_run: bool,
    title: Optional[str],
    author: Optional[str],
) -> int:
    logger = Logger(LOGS_DIR / "queuer.log")

    # Single-instance lock.
    MANAGER_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.log("Another queuer instance is already running. Exiting.")
        os.close(lock_fd)
        return 1

    try:
        return _queue(batch_size, dry_run, title, author, logger)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _queue(
    batch_size: Optional[int],
    dry_run: bool,
    title: Optional[str],
    author: Optional[str],
    logger: Logger,
) -> int:
    # --- Targeted submission by title / author ---------------------------
    if title is not None or author is not None:
        if not AUDIOBOOK_ROOT.exists():
            logger.log(f"ERROR: AUDIOBOOK_ROOT does not exist: {AUDIOBOOK_ROOT}")
            return 1

        match_desc = []
        if title:
            match_desc.append(f"title '{title}'")
        if author:
            match_desc.append(f"author '{author}'")
        match_str = " and ".join(match_desc)

        logger.log(f"Searching library for {match_str}...")
        matches = find_matching_folders(AUDIOBOOK_ROOT, title, author)

        if not matches:
            logger.log(f"No folders found matching {match_str} in {AUDIOBOOK_ROOT}")
            return 0

        logger.log(f"Found {len(matches)} folder(s) matching {match_str}")

        submitted_total = 0
        failed_total = 0

        for folder in matches:
            rel_path = folder.relative_to(AUDIOBOOK_ROOT).as_posix()
            has_audio, has_epub, _ = classify_folder_for_targeted(folder)

            # Skip if already completed (.audiosilo/readalong.json present)
            if (folder / ".audiosilo" / "readalong.json").is_file():
                logger.log(f"SKIP (already completed): {rel_path}")
                continue

            if has_audio and has_epub:
                print(f"\nSubmitting: {rel_path}")
                try:
                    success, job_id, detail = api_post_job(
                        library_id=AUDIOSILO_LIBRARY_ID,
                        path=rel_path,
                        token=AUDIOSILO_TOKEN,
                        timeout=int(API_REQUEST_TIMEOUT),
                        dry_run=dry_run,
                        logger=logger,
                    )
                except APITimeoutError as exc:
                    logger.log(f"TIMEOUT (uncertain): {rel_path} -> {exc}")
                    failed_total += 1
                    time.sleep(int(BATCH_DELAY))
                    continue
                except APIError as exc:
                    logger.log(f"FAILED: {rel_path} -> {exc}")
                    failed_total += 1
                    time.sleep(int(BATCH_DELAY))
                    continue

                if success:
                    logger.log(f"CREATED: {rel_path} | job_id={job_id}")
                    submitted_total += 1
                else:
                    failed_total += 1

                if folder != matches[-1]:
                    time.sleep(int(BATCH_DELAY))

            elif has_audio and not has_epub:
                logger.log(f"MISSING EPUB: {rel_path} -> added to needs_ebook.txt")
                append_to_needs_file(
                    NEEDS_EBOOK_FILE,
                    "# Audiobooks without matching ebooks",
                    rel_path,
                    dry_run,
                    logger,
                )
                failed_total += 1

            elif has_epub and not has_audio:
                logger.log(f"MISSING AUDIOBOOK: {rel_path} -> added to needs_abook.txt")
                append_to_needs_file(
                    NEEDS_ABOOK_FILE,
                    "# Ebooks without matching audiobooks",
                    rel_path,
                    dry_run,
                    logger,
                )
                failed_total += 1

            else:
                logger.log(f"NO CLASSIFIABLE FILES: {rel_path} -> skipped")

        logger.log("=" * 46)
        logger.log("            QUEUE COMPLETE")
        logger.log("=" * 46)
        logger.log(f"Matched:    {len(matches)}")
        logger.log(f"Submitted:  {submitted_total}")
        logger.log(f"Failed:     {failed_total}")
        return 0 if failed_total == 0 else 1

    # --- Batch submission (original behaviour) ---------------------------
    if not POTENTIAL_FILE.exists():
        logger.log(f"ERROR: tracking file not found: {POTENTIAL_FILE}")
        logger.log("Run scan_readalong_candidates.py first.")
        return 1

    rows = parse_tracking_file(POTENTIAL_FILE)
    eligible = [i for i, row in enumerate(rows) if row["status"] == "NEW"]

    if not eligible:
        logger.log("No NEW entries to queue.")
        return 0

    # Determine how many to submit.
    if batch_size is None:
        print(f"\n{len(eligible)} NEW title(s) eligible for submission.")
        print("Enter number of titles to queue (0 to exit):", end=" ")
        sys.stdout.flush()
        try:
            raw = input()
        except EOFError:
            return 0
        raw = raw.strip()
        if not raw:
            return 0
        try:
            batch_size = int(raw)
        except ValueError:
            print("Invalid input. Exiting.")
            return 0
        if batch_size < 0:
            print("Negative value not allowed. Exiting.")
            return 0
    else:
        batch_size = min(batch_size, len(eligible))
        logger.log(f"CLI batch size: {batch_size} (of {len(eligible)} NEW)")

    selected = eligible[:batch_size]

    # Display the selected titles and confirm.
    print(f"\nSelected {len(selected)} entr{'y' if len(selected) == 1 else 'ies'} to queue:")
    for idx in selected:
        print(f"  - {rows[idx]['folder']}")
    if not dry_run:
        print("\nConfirm submission? (y/N):", end=" ")
        sys.stdout.flush()
        try:
            confirm = input().strip().lower()
        except EOFError:
            confirm = "n"
        if confirm != "y":
            logger.log("Submission cancelled by user.")
            return 0

    submitted, failed, uncertain = submit_batch(rows, selected, dry_run, logger)

    # Persist status transitions atomically.
    if not dry_run:
        write_tracking_file(
            POTENTIAL_FILE,
            rows,
            POTENTIAL_HEADER := (
                "# Potential Read-Along Candidates\n"
                "# [NEW]       Not submitted\n"
                "# [CREATED]   Job accepted by AudioSilo\n"
                "# [COMPLETED] Job finished successfully\n"
                "# [IGNORED]   Manually excluded"
            ),
            dry_run=False,
            logger=logger,
        )

    logger.log("=" * 46)
    logger.log("            QUEUE COMPLETE")
    logger.log("=" * 46)
    logger.log(f"Selected:   {len(selected)}")
    logger.log(f"Submitted:  {submitted}")
    logger.log(f"Failed:     {failed}")
    if uncertain:
        logger.log(f"Uncertain (timeout, manual review): {len(uncertain)}")
        for idx in uncertain:
            logger.log(f"  - {rows[idx]['folder']}")

    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 2: Job Queuer (targeted)")
    parser.add_argument(
        "batch_size",
        nargs="?",
        type=int,
        help="Number of NEW titles to queue (omits interactive prompt)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without making API requests or modifying files",
    )
    parser.add_argument(
        "--title",
        type=str,
        help="Submit a specific NEW title whose folder name contains this text (case-insensitive)",
    )
    parser.add_argument(
        "--author",
        type=str,
        help="Submit NEW titles whose folder name contains this author name (case-insensitive)",
    )
    args = parser.parse_args()
    return run(
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        title=args.title,
        author=args.author,
    )


if __name__ == "__main__":
    sys.exit(main())
