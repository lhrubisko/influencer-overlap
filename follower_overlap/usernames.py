"""Parsing and normalisation of Instagram usernames / profile URLs."""

from __future__ import annotations

import csv
import io
import re
from urllib.parse import urlparse

# Instagram usernames: letters, digits, '.', '_' ; max 30 chars.
_USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
_RESERVED_PATHS = {"p", "reel", "reels", "stories", "explore", "accounts", "direct", "tv"}
# CSV header names (lowercased) that mark the column holding the username / profile URL.
_CSV_COLUMNS = ("username", "user", "handle", "instagram", "ig", "account", "profile", "url", "link", "name")


def normalize_username(raw: str) -> str | None:
    """Return a lowercase username, or None if the input is not a valid username/URL.

    Accepts: ``name``, ``@name``, ``instagram.com/name``,
    ``https://www.instagram.com/name/?hl=en``.
    """
    text = (raw or "").strip().strip(",;")
    if not text:
        return None

    lowered = text.lower()
    if "instagram.com" in lowered or lowered.startswith(("http://", "https://")):
        if not lowered.startswith(("http://", "https://")):
            lowered = "https://" + lowered
        parsed = urlparse(lowered)
        host = parsed.netloc.split(":")[0]
        if not (host == "instagram.com" or host.endswith(".instagram.com")):
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if not parts or parts[0] in _RESERVED_PATHS:
            return None
        candidate = parts[0]
    else:
        candidate = lowered

    candidate = candidate.lstrip("@").strip()
    return candidate if _USERNAME_RE.match(candidate) else None


def parse_username_list(text: str) -> tuple[list[str], list[str]]:
    """Split multiline / comma-separated input into (valid unique usernames, rejected lines)."""
    valid: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\n,;\s]+", text or ""):
        if not token.strip():
            continue
        name = normalize_username(token)
        if name is None:
            rejected.append(token.strip())
        elif name not in seen:
            seen.add(name)
            valid.append(name)
    return valid, rejected


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1250"):  # cp1250: Excel CSV exports on Central European Windows
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_username_csv(data: bytes) -> tuple[list[str], list[str]]:
    """Read usernames from a CSV file into (valid unique usernames, rejected cells).

    If the first row is a header containing a known column name (``username``, ``instagram``,
    ``url`` …) that column is used; otherwise the first column of every row. Delimiter
    (``,`` ``;`` tab) is detected automatically.
    """
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return [], []

    column = 0
    header = [c.strip().lower() for c in rows[0]]
    for name in _CSV_COLUMNS:
        if name in header:
            column = header.index(name)
            rows = rows[1:]
            break

    cells = [r[column].strip() for r in rows if len(r) > column and r[column].strip()]
    return parse_username_list("\n".join(cells))
