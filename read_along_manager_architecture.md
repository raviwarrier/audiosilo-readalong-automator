# Read-Along Manager: Final Architecture

## 1. Overall Architecture

The system consists of two independent Python scripts, three tracking files, one configuration file, and a log directory stored on the Raspberry Pi's native filesystem (`/home/pi/readalong-manager/`), separate from the NTFS SSD housing the media library.

```text
Stage 1 · Raspberry Pi (Library Scanner)
  scan_readalong_candidates.py
    └── Recursively scans /srv/ssd/Bookshelf/Audiobooks
          ├── potential-read-alongs.txt  (Both audiobook and ebook found)
          ├── needs_ebook.txt            (Audiobook found, ebook missing)
          └── needs_abook.txt            (Ebook found, audiobook missing)

Stage 2 · Job Submission (Job Queuer)
  queue_readalongs.py
    └── Reads potential-read-alongs.txt, selects batch size, and submits jobs
          └── AudioSilo API: POST /api/v1/admin/bridge/jobs
                └── AudioSilo Bridge worker processes alignment via /library (read-only)
```

- **Decoupled Execution:** The scanner and queuer do not need to run together. You can scan the library today, review or edit the candidate list, and submit jobs days later.
- **Storage Separation:** Scripts, configuration, tracking files, and logs live on `/home/pi/readalong-manager/` (native Linux filesystem). The scanner reads `/srv/ssd/Bookshelf/Audiobooks` (NTFS SSD), and AudioSilo accesses that same library mount inside its container at `/library` with read-only permissions.

---

## 2. Host-to-Container Path Mapping

| Context | Path | Access Mode |
| :--- | :--- | :--- |
| **Host Audiobook Directory** (scanned by Pi) | `/srv/ssd/Bookshelf/Audiobooks` | Read-only during scan |
| **Container Library Directory** (AudioSilo) | `/library` | Read-only mount |
| **Manager Application Directory** (Pi native FS) | `/home/pi/readalong-manager/` | Read/Write |

### Path Verification Item Before Coding
Before implementing the queuer (`queue_readalongs.py`), verify whether `POST /api/v1/admin/bridge/jobs` expects the `path` field relative to `/library` including parent collection folders, for example:
```text
The Witcher Collection - Andrzej Sapkowski/Baptism of Fire (The Witcher Book 3) (1996) - Andrzej Sapkowski
```

---

## 3. Stage 1: Library Scanner (`scan_readalong_candidates.py`)

### A. Folder Identification
Folder identification relies strictly on file contents and folder hierarchy—never on publication years in folder names or fixed folder depths.

Example library hierarchy:
```text
Audiobooks/
├── A Brief History of Black Holes (2022) - Becky Smethurst/
│   ├── A Brief History of Black Holes - Becky Smethurst.m4b
│   └── A Brief History of Black Holes.epub
│
├── Atomic Habits - James Clear/
│   ├── Atomic Habits.mp3
│   └── Atomic Habits.pdf
│
└── The Witcher Collection - Andrzej Sapkowski/
    ├── Baptism of Fire (The Witcher Book 3) (1996) - Andrzej Sapkowski/
    │   ├── Baptism of Fire.m4b
    │   └── Baptism of Fire.epub
    │
    └── Blood of Elves (The Witcher Book 1) (2009) - Andrzej Sapkowski/
        ├── Blood of Elves.m4b
        └── Blood of Elves.epub
```

**Identification Rules:**
- A folder containing both audiobook files and ebook files is classified as a candidate (`potential-read-alongs.txt`).
- A folder containing only audiobook files goes into `needs_ebook.txt`.
- A folder containing only ebook files goes into `needs_abook.txt`.
- A folder containing neither is ignored.
- A parent folder that only contains subfolders is not treated as a book.
- **Direct-File Check Only:** Because the ebook and audiobook reside directly inside the same book folder, the scanner checks files directly inside each folder rather than recursively combining files from descendant subfolders. It still traverses the entire directory tree to discover individual book folders, preventing multi-book parent folders (such as `The Witcher Collection - Andrzej Sapkowski`) from being misidentified as a single book.

### B. Supported Extensions
Extension matching is case-insensitive. File count does not matter (one audiobook file or twenty MP3 files count as one audiobook collection).

| Type | Extensions |
| :--- | :--- |
| **Ebook** (valid for read-along) | `.epub` |
| **Ebook** (tracked as missing, needs conversion) | `.pdf`, `.mobi`, `.azw3` |
| **Audiobook** | `.m4b`, `.mp3`, `.opus` |

Classification rules:

| Audiobook | EPUB | Non-EPUB ebook | Classification | Tracking file |
| :--- | :--- | :--- | :--- | :--- |
| Yes | Yes | Any | `both` | `potential-read-alongs.txt` |
| Yes | No | Yes | `audio` | `needs_ebook.txt` |
| Yes | No | No | `audio` | `needs_ebook.txt` |
| No | Yes | Any | `ebook` | `needs_abook.txt` |
| No | No | Yes | `ebook` | `needs_abook.txt` |

Only folders containing an audiobook **and** an `.epub` are added to `potential-read-alongs.txt`. Folders with an audiobook and a non-EPUB ebook (`.pdf`, `.mobi`, `.azw3`) are treated as audiobook-only and added to `needs_ebook.txt`, since the worker requires an EPUB for alignment.

### C. Output Tracking Files

#### `potential-read-alongs.txt`
```text
# Potential Read-Along Candidates
# [NEW]       Not submitted
# [CREATED]   Job accepted by AudioSilo
# [COMPLETED] Job finished successfully
# [IGNORED]   Manually excluded

[NEW] | A Brief History of Black Holes (2022) - Becky Smethurst
[NEW] | Atomic Habits - James Clear
[CREATED] | The Witcher Collection - Andrzej Sapkowski/Baptism of Fire (The Witcher Book 3) (1996) - Andrzej Sapkowski | job_id=10
```

#### `needs_ebook.txt`
```text
# Audiobooks without matching ebooks

[NEEDS_EBOOK] | Author A/Book A
[NEEDS_EBOOK] | Author B/Book B
```

#### `needs_abook.txt`
```text
# Ebooks without matching audiobooks

[NEEDS_ABOOK] | Author C/Book C
[NEEDS_ABOOK] | Author D/Book D
```

### D. Rescanning Behaviour (Confirmed)
Subsequent runs perform an incremental update across all three lists:
- Preserve all existing `CREATED`, `COMPLETED`, and `IGNORED` statuses (including `job_id` metadata).
- Preserve existing `NEW` entries.
- Add newly discovered candidates without creating duplicate entries.
- Remove entries if their corresponding folders no longer exist on disk.
- Preserve existing entries in `needs_ebook.txt` and `needs_abook.txt` unless their folders disappear or their file classification changes (for example, when a missing ebook is added to a folder).

### E. Scanner Summary Output
```text
========================================
             SCAN COMPLETE
========================================

Folders examined:          5,420

Potential read-alongs:       820
Needs ebook:                 410
Needs audiobook:             125

New candidates added:        120
Existing candidates:         700
Existing statuses preserved: 700

Scan duration:               00:02:14
```

---

## 4. Stage 2: Job Queuer (`queue_readalongs.py`)

`queue_readalongs.py` reads `potential-read-alongs.txt` without re-inspecting the media files on disk and submits eligible jobs to AudioSilo.

### A. Processing States

| State | Meaning | Eligible for Submission? | How Transitioned |
| :--- | :--- | :--- | :--- |
| `NEW` | Not yet submitted | Yes | Added by scanner |
| `CREATED` | AudioSilo accepted the job | No | Set by queuer on confirmed HTTP success |
| `COMPLETED` | Job finished successfully | No | Manually edited in `potential-read-alongs.txt` |
| `IGNORED` | Manually excluded | No | Manually edited in `potential-read-alongs.txt` |

- `CREATED` indicates only that AudioSilo accepted the HTTP request and returned a job ID—not that GPU alignment has finished.
- Both `COMPLETED` and `IGNORED` entries remain in `potential-read-alongs.txt` across future scans.

### B. Interactive Batch Selection
Running without arguments:
```bash
python3 queue_readalongs.py
```
Displays status counts and prompts for batch size:
```text
========================================
       AUDIOSILO READ-ALONG QUEUER
========================================

Total candidates:       1,250
NEW:                      850
CREATED:                  300
COMPLETED:                 50
IGNORED:                   50

How many titles should be queued?
Enter number (0 to exit):
```
Entering a number (e.g., `10`) selects the first 10 `NEW` entries, displays the selected titles, and asks for explicit confirmation before submitting.

### C. Command-Line Batch Selection & Dry-Run
```bash
# Submit the first 10 NEW titles (with confirmation)
python3 queue_readalongs.py 10

# Preview without making API requests or modifying files
python3 queue_readalongs.py --dry-run
python3 queue_readalongs.py 10 --dry-run
```

### D. Job Submission Workflow
Endpoint:
```http
POST /api/v1/admin/bridge/jobs
```

Request payload:
```json
{
  "kind": "readalong.align",
  "library_id": 1,
  "path": "A Brief History of Black Holes (2022) - Becky Smethurst",
  "params": {}
}
```

Per-title execution sequence:
1. Read the next `NEW` title from `potential-read-alongs.txt`.
2. Check for an existing active job on the server (if supported by the job-list endpoint).
3. Submit the `POST` request.
4. Parse the server response and extract the returned `job_id`.
5. Update the entry status from `[NEW]` to `[CREATED]` with `| job_id=<id>` only after a confirmed successful response.
6. Wait `BATCH_DELAY` seconds (default: `2`) before submitting the next job.

**Failure & Timeout Handling:**
- If a request fails cleanly (e.g., HTTP error or connection refused), the entry remains `[NEW]`.
- If a connection times out after the request is sent, the result is uncertain: the script must **not** automatically resubmit the job and must log/report the entry for manual inspection.

---

## 5. Duplicate Protection

Two independent layers prevent duplicate GPU alignment workloads:

1. **Layer 1 (Local Protection):** `queue_readalongs.py` never submits entries marked `CREATED`, `COMPLETED`, or `IGNORED`.
2. **Layer 2 (Server-Side Protection):** Before submitting a `NEW` entry, the queuer checks AudioSilo's job-list endpoint (pending verification of response fields) to ensure no queued or processing job already exists for the same `library_id`, `path`, and `kind` (`readalong.align`).

*(Future consideration: Any deliberate regeneration of a `COMPLETED` read-along will require an explicit `--regenerate` flag so it can never happen accidentally.)*

---

## 6. Stage 3: Future Job Completion Tracking (Design Readiness)

Automatic completion tracking is excluded from v1, in accordance with the confirmed decision to manually update `CREATED` to `COMPLETED`. However, tracking entries store `| job_id=<id>` so a future monitoring pass can query AudioSilo's job status response (`Job ID`, `State`, `Progress`, `Result`, `Error`, `Created` and `Finished` timestamps) and automatically transition `[CREATED]` to `[COMPLETED]` or surface failed jobs.

---

## 7. File and Directory Organisation

Location on the Raspberry Pi's native filesystem:
```text
/home/pi/readalong-manager/
├── scan_readalong_candidates.py
├── queue_readalongs.py
├── potential-read-alongs.txt
├── needs_ebook.txt
├── needs_abook.txt
├── .env
├── .gitignore
├── README.md
└── logs/
```

### Configuration (`.env`)

Every configurable value — paths, URLs, API keys, secrets, ports, default batch
size, delay, timeouts, and file locations — lives in `.env`.  A committed
`.env.example` documents every variable with its default; real secrets (notably
`AUDIOSILO_TOKEN`) must be filled in locally and are never committed.

```env
# ---------------------------------------------------------------------
# AudioSilo API (REQUIRED - secrets / environment-specific)
# ---------------------------------------------------------------------
AUDIOSILO_URL=https://abooks.raviwarrier.net
AUDIOSILO_TOKEN=your_new_token_here
AUDIOSILO_LIBRARY_ID=1

# ---------------------------------------------------------------------
# Library paths
# ---------------------------------------------------------------------
# Host path scanned by the Pi (NTFS SSD) — REQUIRED
AUDIOBOOK_ROOT=/srv/ssd/Bookshelf/Audiobooks

# Path AudioSilo sees inside its container (read-only mount) — default /library
AUDIOSILO_CONTAINER_LIBRARY=/library

# ---------------------------------------------------------------------
# Manager application directory (Pi native filesystem)
# ---------------------------------------------------------------------
MANAGER_DIR=/home/pi/readalong-manager
LOGS_DIR=logs

# ---------------------------------------------------------------------
# Tracking files (resolved relative to MANAGER_DIR)
# ---------------------------------------------------------------------
POTENTIAL_FILE=potential-read-alongs.txt
NEEDS_EBOOK_FILE=needs_ebook.txt
NEEDS_ABOOK_FILE=needs_abook.txt

# ---------------------------------------------------------------------
# Batch / timing
# ---------------------------------------------------------------------
# Default number of NEW titles to queue per run — default 10
BATCH_SIZE=10
# Seconds to wait between job submissions — default 2
BATCH_DELAY=2
# HTTP request timeout in seconds — default 30
API_REQUEST_TIMEOUT=30
```

- `.env` is excluded via `.gitignore` and must not be committed to version
  control or included in unencrypted shared backups.
- `.env.example` is committed and shows all variables with defaults.
- Required values (`AUDIOSILO_URL`, `AUDIOSILO_TOKEN`, `AUDIOBOOK_ROOT`) cause
  the scripts to fail fast with a clear error if missing.
- All paths default relative to `MANAGER_DIR`; container and host paths are
  environment-specific and should match your actual deployment.

---

## 8. Safeguards and Quality-of-Life Features

| Feature | Purpose |
| :--- | :--- |
| **Dry-run mode** | Preview scanning and job submission without API calls or file modifications |
| **Interactive confirmation** | Prevent accidental batch submissions |
| **Atomic file updates** | Write to temporary file and rename to protect tracking lists from corruption |
| **Duplicate detection** | Prevent duplicate lines in tracking files and duplicate active jobs on the server |
| **Error & timeout handling** | Keep failed entries as `NEW`; flag uncertain timeouts for manual review without blind retry |
| **Single-instance lock** | Prevent concurrent runs of scanner or queuer from corrupting shared tracking files |
| **Persistent logging** | Record scan runs, API submissions, job IDs, and errors under `/home/pi/readalong-manager/logs/` |
| **Summary reports** | Display counts examined, added, removed, preserved, and submitted |

---

## 9. Confirmed Architecture Decisions

| Decision Area | Confirmed Behaviour |
| :--- | :--- |
| **Scanning strategy** | Incremental update; preserve all existing statuses (`NEW`, `CREATED`, `COMPLETED`, `IGNORED`) |
| **Missing folders** | Remove entries from tracking lists when their corresponding folders no longer exist |
| **Job completion** | Manually edit `[CREATED]` to `[COMPLETED]` in `potential-read-alongs.txt` (v1) |
| **Ignored titles** | Manually edit `[NEW]` to `[IGNORED]` in `potential-read-alongs.txt` (v1) |
| **Retention of terminal states** | Both `COMPLETED` and `IGNORED` entries remain in `potential-read-alongs.txt` across rescans |
| **Manager file location** | `/home/pi/readalong-manager/` on Pi native filesystem (media remains at `/srv/ssd/Bookshelf/Audiobooks`) |

---

## 10. Implementation Phases

1. **Phase 1: Library Scanner**
   - Build `scan_readalong_candidates.py` and generate `potential-read-alongs.txt`, `needs_ebook.txt`, and `needs_abook.txt`.
   - Verify against `/srv/ssd/Bookshelf/Audiobooks` that nested collections (such as `The Witcher Collection - Andrzej Sapkowski`) produce individual book candidates rather than a single collection candidate.
2. **Phase 2: Candidate-List Management**
   - Verify incremental rescans, status preservation (`NEW`, `CREATED`, `COMPLETED`, `IGNORED`), duplicate prevention, atomic writes, and removal of deleted folders.
3. **Phase 3: API Verification & Queuer Core**
   - Confirm the relative `path` convention expected by `POST /api/v1/admin/bridge/jobs` relative to `/library`.
   - Build `queue_readalongs.py` with `--dry-run`, interactive and CLI batch sizing, confirmation prompts, and `CREATED` state transitions.
4. **Phase 4: Duplicate Protection**
   - Integrate AudioSilo's job-list endpoint to block submission when an active job already exists for the same library, path, and `readalong.align` kind.
5. **Phase 5: End-to-End Testing**
   - Test with a small batch (2–3 books), verify returned job IDs and alignment outputs in AudioSilo, and scale up batch sizes.