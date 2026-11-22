"""Configuration loader for the Read-Along Manager.

All configuration is sourced from a ``.env`` file (falling back to real
environment variables).  No third-party dependencies are required.

Secrets / environment-specific values are *required* and the scripts fail fast
with a clear error when they are missing.  Everything else has a sensible
default and can be overridden in ``.env`` or via the environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Locate the .env file
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent


def _find_env_path() -> Path:
    """Return the path to the .env file."""
    override = os.environ.get("READALONG_ENV_FILE")
    if override:
        return Path(override)
    next_to_script = _HERE / ".env"
    if next_to_script.exists():
        return next_to_script
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    return next_to_script


_ENV_PATH = _find_env_path()


def _load_env(path: Path) -> dict:
    """Parse a .env file into a dict without overwriting existing env vars."""
    data: dict = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        data[key] = value
    return data


_ENV_DATA = _load_env(_ENV_PATH)


def get(key: str, default: Optional[str] = None, required: bool = False) -> str:
    """Return a config value (real env takes precedence over .env)."""
    value = os.environ.get(key)
    if value is None:
        value = _ENV_DATA.get(key, default)
    if value is None and required:
        raise RuntimeError(
            f"Required configuration '{key}' is missing. "
            f"Set it in {_ENV_PATH} or as an environment variable."
        )
    return value or ""


def get_int(key: str, default: int = 0, required: bool = False) -> int:
    raw = get(key, str(default) if default is not None else None, required=required)
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Config '{key}' must be an integer, got {raw!r}") from exc


def get_path(
    key: str,
    default: Optional[str] = None,
    required: bool = False,
    base: Optional[Path] = None,
) -> Path:
    """Return a config value as a :class:`pathlib.Path`.

    Relative paths are resolved against ``base`` (defaults to the script
    directory) so values like ``logs`` or ``potential-read-alongs.txt`` work
    out of the box.
    """
    raw = get(key, default, required=required)
    if raw == "":
        return Path(default) if default else Path()
    p = Path(raw)
    if not p.is_absolute() and base is not None:
        p = base / p
    return p


# ---------------------------------------------------------------------------
# Concrete settings (all sourced from .env / environment)
# ---------------------------------------------------------------------------
# --- Required (secrets / environment-specific) -----------------------------
AUDIOSILO_URL = get("AUDIOSILO_URL", required=True)
AUDIOSILO_TOKEN = get("AUDIOSILO_TOKEN", required=True)
AUDIOBOOK_ROOT = get_path("AUDIOBOOK_ROOT", required=True)

# --- Optional with defaults ------------------------------------------------
AUDIOSILO_LIBRARY_ID = get_int("AUDIOSILO_LIBRARY_ID", default=1)

# Manager application directory (on the Pi native filesystem)
MANAGER_DIR = get_path("MANAGER_DIR", default=str(_HERE))

# Tracking files (resolved relative to MANAGER_DIR)
POTENTIAL_FILE = get_path(
    "POTENTIAL_FILE", default="potential-read-alongs.txt", base=MANAGER_DIR
)
NEEDS_EBOOK_FILE = get_path(
    "NEEDS_EBOOK_FILE", default="needs_ebook.txt", base=MANAGER_DIR
)
NEEDS_ABOOK_FILE = get_path(
    "NEEDS_ABOOK_FILE", default="needs_abook.txt", base=MANAGER_DIR
)

# Logs directory (resolved relative to MANAGER_DIR)
LOGS_DIR = get_path("LOGS_DIR", default="logs", base=MANAGER_DIR)

# Container mount path AudioSilo sees (read-only)
AUDIOSILO_CONTAINER_LIBRARY = get("AUDIOSILO_CONTAINER_LIBRARY", default="/library")

# Batch / timing
BATCH_SIZE = get_int("BATCH_SIZE", default=10)
BATCH_DELAY = get_int("BATCH_DELAY", default=2)
API_REQUEST_TIMEOUT = get_int("API_REQUEST_TIMEOUT", default=30)

__all__ = [
    "AUDIOSILO_URL",
    "AUDIOSILO_TOKEN",
    "AUDIOSILO_LIBRARY_ID",
    "AUDIOSILO_CONTAINER_LIBRARY",
    "AUDIOBOOK_ROOT",
    "MANAGER_DIR",
    "LOGS_DIR",
    "POTENTIAL_FILE",
    "NEEDS_EBOOK_FILE",
    "NEEDS_ABOOK_FILE",
    "BATCH_SIZE",
    "BATCH_DELAY",
    "API_REQUEST_TIMEOUT",
    "_ENV_PATH",
]
