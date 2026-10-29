"""Configuration loaded from environment variables / a local .env file.

Secrets (IG_PASSWORD) are read here only for the interactive login script and
are never stored in Settings objects that get displayed or logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default)).expanduser()
    return value if value.is_absolute() else PROJECT_ROOT / value


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class CollectionSettings:
    page_size: int = 100            # followers requested per API page (instagrapi max 200)
    max_followers: int = 2000       # stop after this many unique followers per influencer; 0 = no limit
    delay_min: float = 8.0          # random pause between pages (seconds)
    delay_max: float = 15.0
    max_retries: int = 2            # bounded retries after a rate-limit response
    backoff_seconds: float = 300.0  # first backoff; doubles per retry

    def validated(self) -> "CollectionSettings":
        page = min(max(self.page_size, 10), 200)
        dmin = max(self.delay_min, 0.0)
        dmax = max(self.delay_max, dmin)
        return CollectionSettings(
            page_size=page,
            max_followers=max(self.max_followers, 0),
            delay_min=dmin,
            delay_max=dmax,
            max_retries=min(max(self.max_retries, 0), 5),
            backoff_seconds=max(self.backoff_seconds, 1.0),
        )


DB_PATH = _path("OVERLAP_DB_PATH", "data/overlap.sqlite3")
SESSION_FILE = _path("IG_SESSION_FILE", ".secrets/ig_session.json")
IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PROXY = os.getenv("IG_PROXY", "").strip() or None

DEFAULT_COLLECTION = CollectionSettings(
    page_size=_int("COLLECT_PAGE_SIZE", 100),
    max_followers=_int("COLLECT_MAX_FOLLOWERS", 2000),
    delay_min=_float("COLLECT_DELAY_MIN", 8.0),
    delay_max=_float("COLLECT_DELAY_MAX", 15.0),
    max_retries=_int("COLLECT_MAX_RETRIES", 2),
    backoff_seconds=_float("COLLECT_BACKOFF_SECONDS", 300.0),
).validated()
