# Read-Along Manager

A small utility for [AudioSilo Server](https://github.com/KodeStar/audiosilo-server) that scans a local audiobook library, finds read-along candidates (folders containing both an audiobook and an ebook), and queues alignment jobs against AudioSilo's bridge API.

## Why

AudioSilo can generate read-along alignments, but you still have to discover which books qualify and submit them one by one. This tool walks your library on the Raspberry Pi, builds a candidate list, and submits jobs in batches — with status tracking so nothing gets submitted twice.

## What

Two independent Python scripts plus a tiny config loader:

| Script | Stage | What it does |
|--------|-------|--------------|
| `scan_readalong_candidates.py` | 1 | Recursively scans `AUDIOBOOK_ROOT`, classifies folders (audiobook + ebook / audiobook only / ebook only), and writes three tracking files incrementally while preserving statuses. |
| `queue_readalongs.py` | 2 | Reads the candidate list, lets you pick a batch size, and submits `readalong.align` jobs to `POST /api/v1/admin/bridge/jobs`. Transitions `[NEW]` → `[CREATED]` on success, with delay between submissions and failure/timeout handling. |

## How

It does **not** talk to your audio files after scanning — it only reads `potential-read-alongs.txt` and sends job payloads (`kind`, `library_id`, `path`, `params`) to AudioSilo over HTTP. AudioSilo must be running and reachable at `AUDIOSILO_URL`; otherwise every submission will fail. The container mount path AudioSilo sees (`/library`) is separate from the host path the scanner walks (`/srv/ssd/Bookshelf/Audiobooks`).

## Usage steps

1. Copy `.env.example` to `.env` and fill in the required values (especially `AUDIOSILO_TOKEN`).
2. Make sure a working AudioSilo Server is reachable at `AUDIOSILO_URL`.
3. Run the scanner:
   ```bash
   python3 scan_readalong_candidates.py
   ```
   Review `potential-read-alongs.txt` and optionally mark entries `[IGNORED]`.
4. Run the queuer:
   ```bash
   # Interactive batch size
   python3 queue_readalongs.py
   # Or submit N titles directly
   python3 queue_readalongs.py 10
   # Preview without API calls
   python3 queue_readalongs.py --dry-run
   ```
5. Check `logs/` for per-run logs and inspect `potential-read-alongs.txt` for `[CREATED] | job_id=<id>` entries.

## Environment variables

All configuration lives in `.env` (git-ignored). Copy `.env.example` and adjust as needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIOSILO_URL` | *(required)* | Base URL of your AudioSilo Server, e.g. `https://your.audiosilo-server.com` |
| `AUDIOSILO_TOKEN` | *(required)* | Bearer token for AudioSilo admin API |
| `AUDIOSILO_LIBRARY_ID` | `1` | Library ID to submit jobs into |
| `AUDIOBOOK_ROOT` | *(required)* | Host path the scanner walks, e.g. `/srv/ssd/Bookshelf/Audiobooks` |
| `AUDIOSILO_CONTAINER_LIBRARY` | `/library` | Path AudioSilo sees inside its container (read-only mount) |
| `MANAGER_DIR` | `/home/pi/readalong-manager` | Directory where scripts and tracking files live |
| `LOGS_DIR` | `logs` | Log directory, relative to `MANAGER_DIR` |
| `POTENTIAL_FILE` | `potential-read-alongs.txt` | Candidate list, relative to `MANAGER_DIR` |
| `NEEDS_EBOOK_FILE` | `needs_ebook.txt` | Audiobooks missing ebooks, relative to `MANAGER_DIR` |
| `NEEDS_ABOOK_FILE` | `needs_abook.txt` | Ebooks missing audiobooks, relative to `MANAGER_DIR` |
| `BATCH_SIZE` | `10` | Default number of NEW titles to queue per run |
| `BATCH_DELAY` | `2` | Seconds to wait between job submissions |
| `API_REQUEST_TIMEOUT` | `30` | HTTP request timeout in seconds |

Required values (`AUDIOSILO_URL`, `AUDIOSILO_TOKEN`, `AUDIOBOOK_ROOT`) cause the scripts to fail fast with a clear error if missing.

## ⚠️ Vibe-coded disclaimer

This project was vibe-coded — written quickly with AI assistance, not rigorously audited. It works for the author's setup; review, test, and adapt before relying on it in production. Use at your own risk.

## License

MIT License — short and permissive; do whatever you want with it, just keep the license and copyright notice.

## Prerequisite

This utility will not work without a working AudioSilo Server ([KodeStar/audiosilo-server](https://github.com/KodeStar/audiosilo-server)) reachable at `AUDIOSILO_URL`. It is a client-side helper only.
