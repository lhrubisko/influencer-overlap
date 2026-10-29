"""SQLite persistence.

Data model
----------
influencers      one row per Instagram username we track
snapshots        an explicit analysis snapshot (a collection "round")
collection_runs  one row per (snapshot, influencer): status, counts, cursor, timestamps
followers        follower IDs collected in a run; PRIMARY KEY dedupes per run

Every thread must open its own connection via ``connect()``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import config

# Run statuses
PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"          # pagination ended (Instagram returned no further cursor)
LIMIT_REACHED = "limit_reached"  # configured max_followers reached
CANCELLED = "cancelled"
RATE_LIMITED = "rate_limited"
AUTH_REQUIRED = "auth_required"  # login / checkpoint / 2FA needed
ERROR = "error"
INTERRUPTED = "interrupted"      # app/process stopped while running

FINAL_STATUSES = {COMPLETED, LIMIT_REACHED, CANCELLED, RATE_LIMITED, AUTH_REQUIRED, ERROR, INTERRUPTED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS influencers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    ig_user_id TEXT,
    full_name TEXT,
    is_private INTEGER,
    reported_follower_count INTEGER,
    profile_checked_at TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    notes TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    ig_user_id TEXT,
    reported_follower_count INTEGER,
    collected_count INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0,
    max_followers INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    stop_reason TEXT,
    pagination_ended INTEGER NOT NULL DEFAULT 0,
    limit_reached INTEGER NOT NULL DEFAULT 0,
    error_occurred INTEGER NOT NULL DEFAULT 0,
    next_cursor TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT,
    UNIQUE (snapshot_id, influencer_id)
);

CREATE TABLE IF NOT EXISTS followers (
    run_id INTEGER NOT NULL REFERENCES collection_runs(id) ON DELETE CASCADE,
    follower_id TEXT NOT NULL,
    username TEXT,
    PRIMARY KEY (run_id, follower_id)
) WITHOUT ROWID;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else config.DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- influencers

def add_influencers(conn: sqlite3.Connection, usernames: Iterable[str], is_demo: bool = False) -> tuple[list[str], list[str]]:
    """Insert usernames (already normalised). Returns (added, already_existing)."""
    added, existing = [], []
    with conn:
        for name in usernames:
            cur = conn.execute(
                "INSERT OR IGNORE INTO influencers (username, is_demo, created_at) VALUES (?, ?, ?)",
                (name, int(is_demo), now_iso()),
            )
            (added if cur.rowcount else existing).append(name)
    return added, existing


def list_influencers(conn: sqlite3.Connection, is_demo: bool | None = None) -> list[dict]:
    sql = "SELECT * FROM influencers"
    params: tuple = ()
    if is_demo is not None:
        sql += " WHERE is_demo = ?"
        params = (int(is_demo),)
    return [dict(r) for r in conn.execute(sql + " ORDER BY username", params)]


def get_influencer(conn: sqlite3.Connection, influencer_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM influencers WHERE id = ?", (influencer_id,)).fetchone()
    return dict(row) if row else None


def update_influencer_profile(
    conn: sqlite3.Connection,
    influencer_id: int,
    ig_user_id: str,
    full_name: str | None,
    is_private: bool | None,
    follower_count: int | None,
) -> None:
    with conn:
        conn.execute(
            """UPDATE influencers SET ig_user_id = ?, full_name = ?, is_private = ?,
                   reported_follower_count = ?, profile_checked_at = ? WHERE id = ?""",
            (str(ig_user_id), full_name, None if is_private is None else int(is_private),
             follower_count, now_iso(), influencer_id),
        )


def delete_influencer(conn: sqlite3.Connection, influencer_id: int) -> None:
    """Deletes the influencer and (via cascade) all of their runs and followers."""
    with conn:
        conn.execute("DELETE FROM influencers WHERE id = ?", (influencer_id,))


# ------------------------------------------------------------------ snapshots

def create_snapshot(conn: sqlite3.Connection, name: str, notes: str = "", is_demo: bool = False) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO snapshots (name, notes, is_demo, created_at) VALUES (?, ?, ?, ?)",
            (name.strip(), notes, int(is_demo), now_iso()),
        )
    return int(cur.lastrowid)


def list_snapshots(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC, id DESC")]


def get_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    return dict(row) if row else None


def snapshot_activity(conn: sqlite3.Connection) -> dict[int, dict]:
    """{snapshot_id: {runs, collected, last_update}} for snapshots that have collection runs."""
    rows = conn.execute(
        """SELECT snapshot_id, COUNT(*) AS runs, COALESCE(SUM(collected_count), 0) AS collected,
                  MAX(updated_at) AS last_update
           FROM collection_runs GROUP BY snapshot_id"""
    )
    return {r["snapshot_id"]: dict(r) for r in rows}


def default_snapshot_id(conn: sqlite3.Connection, is_demo: bool | None = None) -> int | None:
    """The snapshot with the most recent collection activity, else the newest snapshot."""
    snapshots = [s for s in list_snapshots(conn) if is_demo is None or bool(s["is_demo"]) == is_demo] \
        or list_snapshots(conn)
    if not snapshots:
        return None
    activity = snapshot_activity(conn)
    active = [s for s in snapshots if s["id"] in activity]
    if active:
        return max(active, key=lambda s: activity[s["id"]]["last_update"] or "")["id"]
    return snapshots[0]["id"]


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


# ---------------------------------------------------------------------- runs

def get_or_create_run(conn: sqlite3.Connection, snapshot_id: int, influencer_id: int) -> dict:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO collection_runs (snapshot_id, influencer_id, updated_at) VALUES (?, ?, ?)",
            (snapshot_id, influencer_id, now_iso()),
        )
    row = conn.execute(
        "SELECT * FROM collection_runs WHERE snapshot_id = ? AND influencer_id = ?",
        (snapshot_id, influencer_id),
    ).fetchone()
    return dict(row)


def get_run(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM collection_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection, snapshot_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, i.username, i.is_private AS influencer_is_private
           FROM collection_runs r JOIN influencers i ON i.id = r.influencer_id
           WHERE r.snapshot_id = ? ORDER BY i.username""",
        (snapshot_id,),
    )
    return [dict(r) for r in rows]


def start_run(
    conn: sqlite3.Connection,
    run_id: int,
    ig_user_id: str,
    reported_follower_count: int | None,
    max_followers: int,
    restart: bool = False,
) -> None:
    """Mark a run as running. ``restart`` drops the saved cursor (already collected IDs are kept)."""
    with conn:
        conn.execute(
            f"""UPDATE collection_runs SET
                   status = 'running', stop_reason = NULL,
                   ig_user_id = ?, reported_follower_count = ?, max_followers = ?,
                   pagination_ended = 0, limit_reached = 0, error_occurred = 0,
                   attempts = attempts + 1,
                   started_at = COALESCE(started_at, ?), finished_at = NULL, updated_at = ?
                   {", next_cursor = NULL" if restart else ""}
               WHERE id = ?""",
            (str(ig_user_id), reported_follower_count, max_followers, now_iso(), now_iso(), run_id),
        )


def save_page(
    conn: sqlite3.Connection,
    run_id: int,
    followers: Iterable[tuple[str, str | None]],
    next_cursor: str | None,
) -> int:
    """Store one page of followers + the cursor atomically. Returns the number of newly added IDs."""
    with conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO followers (run_id, follower_id, username) VALUES (?, ?, ?)",
            ((run_id, str(fid), uname) for fid, uname in followers),
        )
        added = conn.total_changes - before
        conn.execute(
            """UPDATE collection_runs SET
                   collected_count = (SELECT COUNT(*) FROM followers WHERE run_id = ?),
                   pages_fetched = pages_fetched + 1, next_cursor = ?, updated_at = ?
               WHERE id = ?""",
            (run_id, next_cursor, now_iso(), run_id),
        )
    return added


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    stop_reason: str,
    pagination_ended: bool = False,
    limit_reached: bool = False,
    error_occurred: bool = False,
) -> None:
    assert status in FINAL_STATUSES, status
    with conn:
        conn.execute(
            """UPDATE collection_runs SET status = ?, stop_reason = ?, pagination_ended = ?,
                   limit_reached = ?, error_occurred = ?, finished_at = ?, updated_at = ?,
                   next_cursor = CASE WHEN ? THEN NULL ELSE next_cursor END
               WHERE id = ?""",
            (status, stop_reason[:500], int(pagination_ended), int(limit_reached), int(error_occurred),
             now_iso(), now_iso(), int(pagination_ended), run_id),
        )


def mark_orphaned_runs_interrupted(conn: sqlite3.Connection, active_run_ids: Iterable[int] = ()) -> int:
    """Runs left in 'running' state with no live worker (e.g. app restarted) become 'interrupted'."""
    active = list(active_run_ids)
    exclude = f" AND id NOT IN ({','.join('?' * len(active))})" if active else ""
    with conn:
        cur = conn.execute(
            f"""UPDATE collection_runs SET status = 'interrupted', error_occurred = 1,
                    stop_reason = 'App stopped while collecting; partial data kept. Resume to continue.',
                    finished_at = ?, updated_at = ?
                WHERE status = 'running'{exclude}""",
            (now_iso(), now_iso(), *active),
        )
    return cur.rowcount


# ------------------------------------------------------------------ followers

TRUNCATION_THRESHOLD = 0.9  # pagination ended below this share of the reported count -> cut short


def is_truncated(collected: int, reported: int | None, pagination_ended: bool) -> bool:
    """Instagram ended pagination well below the reported follower count."""
    return bool(pagination_ended and reported and collected < TRUNCATION_THRESHOLD * reported)


def run_has_data(run: dict) -> bool:
    """A run counts as measured data if it collected IDs, or if it completed with an empty list.

    A run with 0 IDs that did not complete (error, cancelled before page 1, never run) is *missing*
    data and must not be treated as "zero overlap".
    """
    if run.get("collected_count", 0) > 0:
        return True
    return run.get("status") == COMPLETED and bool(run.get("pagination_ended"))


def load_follower_sets(conn: sqlite3.Connection, snapshot_id: int, influencer_ids: Iterable[int] | None = None) -> dict[str, set[str]]:
    """Return {influencer_username: set(follower_id)} for runs in the snapshot that have data."""
    runs = list_runs(conn, snapshot_id)
    if influencer_ids is not None:
        wanted = set(influencer_ids)
        runs = [r for r in runs if r["influencer_id"] in wanted]
    result: dict[str, set[str]] = {}
    for run in runs:
        if not run_has_data(run):
            continue
        ids = conn.execute("SELECT follower_id FROM followers WHERE run_id = ?", (run["id"],))
        result[run["username"]] = {row[0] for row in ids}
    return result
