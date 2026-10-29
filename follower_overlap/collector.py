"""Instagram follower collection via instagrapi (unofficial private API).

Verified against instagrapi 3.2.2 (installed source):
  Client.user_info_by_username(username, use_cache=True) -> User (pk, follower_count, is_private, ...)
  Client.user_followers_v1_chunk(user_id, max_amount=0, max_id="", order=None)
        -> (list[UserShort], next_max_id)       # UserShort has .pk / .username only
  Client.load_settings(path) / dump_settings(path) / account_info()
  Client.login(username, password, relogin=False, verification_code="")  # raises TwoFactorRequired

Resume: ``next_max_id`` is persisted after every stored page and passed back as ``max_id``.
Instagram may expire or reject old cursors; then use "restart" (already collected IDs are kept,
duplicates are ignored).

The collection logic only depends on two client methods, so tests can use a fake client.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from instagrapi import Client
from instagrapi import exceptions as igx

from . import config, storage
from .config import CollectionSettings

log = logging.getLogger(__name__)

# Keep library loggers quiet: request-level debug output can include headers.
for _name in ("instagrapi", "private_request", "public_request"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Pause between HTTP requests *inside* one instagrapi chunk call (only used if Instagram returns
# fewer users than requested and instagrapi fetches another page internally).
INTRA_CHUNK_DELAY = [1, 3]
# Stop if this many consecutive pages add no new IDs (cursor not advancing).
MAX_STALLED_PAGES = 3

RETRYABLE_ERRORS = (
    igx.PleaseWaitFewMinutes,
    igx.RateLimitError,
    igx.ClientThrottledError,
    igx.ClientConnectionError,
    igx.ClientRequestTimeout,
)
RATE_LIMIT_ERRORS = (igx.PleaseWaitFewMinutes, igx.RateLimitError, igx.ClientThrottledError, igx.FeedbackRequired)
AUTH_ERRORS = (
    igx.ChallengeError,          # includes ChallengeRequired and all checkpoint forms
    igx.LoginRequired,
    igx.ClientLoginRequired,
    igx.TwoFactorRequired,
    igx.BadCredentials,
    igx.BadPassword,
    igx.ReloginAttemptExceeded,
    igx.ClientUnauthorizedError,
    igx.AccountSuspended,
    igx.CaptchaChallengeRequired,
)

CHECKPOINT_HELP = (
    "Instagram requires verification (checkpoint/challenge). Stop here: open the Instagram app "
    "or instagram.com with this account, complete the verification, wait a while, then run "
    "`python login.py` again. The tool does not retry or bypass verification."
)
LOGIN_HELP = "The saved Instagram session is missing or no longer valid. Run `python login.py` in a terminal."


class InstagramClient(Protocol):
    def user_info_by_username(self, username: str): ...
    def user_followers_v1_chunk(self, user_id: str, max_amount: int = 0, max_id: str = ""): ...


class StopJob(Exception):
    """Abort the whole job (not just the current profile): auth problems, persistent rate limiting."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Cancelled(Exception):
    pass


class SessionError(Exception):
    pass


def describe_error(exc: BaseException) -> str:
    """Short, non-sensitive error description (no headers, cookies or tokens)."""
    text = str(exc).strip().replace("\n", " ")
    return f"{type(exc).__name__}: {text[:300]}" if text else type(exc).__name__


def auth_message(exc: BaseException) -> str:
    if isinstance(exc, (igx.ChallengeError, igx.CaptchaChallengeRequired, igx.AccountSuspended)):
        return f"{CHECKPOINT_HELP} ({type(exc).__name__})"
    if isinstance(exc, igx.TwoFactorRequired):
        return f"Two-factor verification required. Run `python login.py` interactively. ({type(exc).__name__})"
    return f"{LOGIN_HELP} ({type(exc).__name__})"


# ------------------------------------------------------------------- session

def _refuse_challenge(*_args, **_kwargs):
    raise igx.ChallengeRequired(CHECKPOINT_HELP)


def new_client() -> Client:
    """Client that never prompts on stdin for challenges (would freeze the UI)."""
    client = Client(proxy=config.IG_PROXY) if config.IG_PROXY else Client()
    client.with_challenge_flow = False
    client.challenge_code_handler = _refuse_challenge
    client.change_password_handler = _refuse_challenge
    client.delay_range = INTRA_CHUNK_DELAY
    return client


def save_session(client: Client, session_file: Path) -> None:
    """Write session settings with owner-only permissions (contains auth cookies)."""
    session_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(session_file.parent, 0o700)
    tmp = session_file.with_suffix(".tmp")
    client.dump_settings(tmp)
    os.chmod(tmp, 0o600)
    tmp.replace(session_file)


def load_session_client(session_file: Path | None = None) -> Client:
    """Load the saved session and validate it with one lightweight request. Never logs in with a password."""
    path = session_file or config.SESSION_FILE
    if not path.exists():
        raise SessionError(f"No saved session at {path}. {LOGIN_HELP}")
    client = new_client()
    client.load_settings(path)
    try:
        account = client.account_info()
    except AUTH_ERRORS as exc:
        raise SessionError(auth_message(exc)) from exc
    client.username = account.username
    return client


def session_status(session_file: Path | None = None) -> str:
    path = session_file or config.SESSION_FILE
    return "saved session file present (validated when collection starts)" if path.exists() else "no saved session"


# ---------------------------------------------------------------- collection

@dataclass
class Progress:
    username: str
    collected: int | None  # None = message-only update
    reported: int | None
    pages: int | None
    message: str


def _wait(cancel: threading.Event, seconds: float) -> None:
    if cancel.wait(max(seconds, 0)):
        raise Cancelled()


def _call(fn: Callable, settings: CollectionSettings, cancel: threading.Event, note: Callable[[str], None]):
    """Run ``fn`` with bounded exponential backoff on rate limits / transient network errors."""
    attempt = 0
    while True:
        if cancel.is_set():
            raise Cancelled()
        try:
            return fn()
        except AUTH_ERRORS as exc:
            raise StopJob(storage.AUTH_REQUIRED, auth_message(exc)) from exc
        except igx.FeedbackRequired as exc:  # action block: retrying makes it worse
            raise StopJob(storage.RATE_LIMITED, "Instagram blocked the action (feedback_required). "
                                                f"Stop for several hours. {describe_error(exc)}") from exc
        except RETRYABLE_ERRORS as exc:
            if attempt >= settings.max_retries:
                if isinstance(exc, RATE_LIMIT_ERRORS):
                    raise StopJob(storage.RATE_LIMITED, f"Rate limited after {attempt} retries; stopped. "
                                                        f"Wait (hours, not minutes) and resume. {describe_error(exc)}") from exc
                raise
            wait = settings.backoff_seconds * (2 ** attempt)
            attempt += 1
            note(f"{type(exc).__name__}; backing off {wait:.0f}s (retry {attempt}/{settings.max_retries})")
            _wait(cancel, wait)


def collect_one(
    client: InstagramClient,
    conn: sqlite3.Connection,
    snapshot_id: int,
    influencer: dict,
    settings: CollectionSettings,
    cancel: threading.Event,
    on_progress: Callable[[Progress], None] = lambda p: None,
    restart: bool = False,
    resolve_only: bool = False,
) -> str:
    """Collect followers for one influencer into the snapshot. Returns the final run status.

    Raises StopJob for problems that should stop the whole job. All pages already saved stay saved.
    """
    username = influencer["username"]

    def note(msg: str) -> None:
        on_progress(Progress(username, None, None, None, msg))

    run = None if resolve_only else storage.get_or_create_run(conn, snapshot_id, influencer["id"])

    # 1. resolve profile (stable ID + reported follower count at collection time)
    try:
        user = _call(lambda: client.user_info_by_username(username), settings, cancel, note)
    except Cancelled:
        raise
    except StopJob as exc:
        if run and run["status"] != storage.RUNNING:
            storage.finish_run(conn, run["id"], exc.status, exc.message, error_occurred=True)
        raise
    except Exception as exc:  # noqa: BLE001 – e.g. UserNotFound
        if run is None:
            raise
        storage.finish_run(conn, run["id"], storage.ERROR, f"Profile lookup failed: {describe_error(exc)}",
                           error_occurred=True)
        return storage.ERROR
    ig_id = str(user.pk)
    reported = getattr(user, "follower_count", None)
    storage.update_influencer_profile(conn, influencer["id"], ig_id, getattr(user, "full_name", None),
                                      getattr(user, "is_private", None), reported)
    if resolve_only:
        return "resolved"

    run_id = run["id"]
    cursor = "" if restart else (run["next_cursor"] or "")
    storage.start_run(conn, run_id, ig_id, reported, settings.max_followers, restart=restart)
    collected = run["collected_count"]
    pages = run["pages_fetched"]
    stalled = 0
    on_progress(Progress(username, collected, reported, pages,
                         "resuming from saved cursor" if cursor else "starting from first page"))

    try:
        if getattr(user, "is_private", False):
            note("profile is private – followers are only visible if the logged-in account follows it")
        while True:
            if settings.max_followers and collected >= settings.max_followers:
                storage.finish_run(conn, run_id, storage.LIMIT_REACHED,
                                   f"Configured limit of {settings.max_followers} followers reached.",
                                   limit_reached=True)
                return storage.LIMIT_REACHED
            want = settings.page_size
            if settings.max_followers:
                want = min(want, settings.max_followers - collected)

            page_cursor = cursor
            users, next_cursor = _call(
                lambda: client.user_followers_v1_chunk(ig_id, max_amount=want, max_id=page_cursor),
                settings, cancel, note,
            )
            added = storage.save_page(conn, run_id, ((str(u.pk), u.username) for u in users), next_cursor or None)
            collected = storage.get_run(conn, run_id)["collected_count"]
            pages += 1
            on_progress(Progress(username, collected, reported, pages, f"page {pages}: +{added} new"))

            if not next_cursor:
                reason = ("Pagination ended: Instagram returned no further cursor. "
                          "This does not guarantee the list is complete.")
                if storage.is_truncated(collected, reported, pagination_ended=True):
                    reason = (f"Instagram stopped returning followers after {collected:,} of {reported:,} reported "
                              f"({collected / reported:.2%}). The list was cut short by Instagram – "
                              "results for this profile are not representative.")
                storage.finish_run(conn, run_id, storage.COMPLETED, reason, pagination_ended=True)
                return storage.COMPLETED
            stalled = stalled + 1 if added == 0 else 0
            if stalled >= MAX_STALLED_PAGES:
                storage.finish_run(conn, run_id, storage.ERROR,
                                   f"{MAX_STALLED_PAGES} consecutive pages added no new followers; stopped "
                                   "to avoid looping. Partial data kept.", error_occurred=True)
                return storage.ERROR
            cursor = next_cursor
            _wait(cancel, random.uniform(settings.delay_min, settings.delay_max))
    except Cancelled:
        storage.finish_run(conn, run_id, storage.CANCELLED, "Cancelled by user. Partial data kept; resume possible.")
        raise
    except StopJob as exc:
        storage.finish_run(conn, run_id, exc.status, exc.message, error_occurred=True)
        raise
    except igx.WrongCursorError as exc:
        storage.finish_run(conn, run_id, storage.ERROR,
                           f"Instagram rejected the saved cursor ({describe_error(exc)}). Use 'Restart from "
                           "first page' – collected IDs are kept and duplicates ignored.", error_occurred=True)
        return storage.ERROR
    except Exception as exc:  # noqa: BLE001 – record any failure, keep partial data
        storage.finish_run(conn, run_id, storage.ERROR, describe_error(exc), error_occurred=True)
        log.warning("collection failed for %s: %s", username, describe_error(exc))
        return storage.ERROR


def collect_many(
    client: InstagramClient,
    conn: sqlite3.Connection,
    snapshot_id: int,
    influencers: list[dict],
    settings: CollectionSettings,
    cancel: threading.Event,
    on_progress: Callable[[Progress], None] = lambda p: None,
    restart: bool = False,
    resolve_only: bool = False,
) -> dict[str, str]:
    """Process influencers sequentially. Stops the whole job on auth problems / rate limits / cancel."""
    results: dict[str, str] = {}
    for index, influencer in enumerate(influencers):
        try:
            results[influencer["username"]] = collect_one(
                client, conn, snapshot_id, influencer, settings, cancel, on_progress, restart, resolve_only)
        except Cancelled:
            results[influencer["username"]] = storage.CANCELLED
            break
        except StopJob as exc:
            results[influencer["username"]] = exc.status
            for rest in influencers[index + 1:]:
                results[rest["username"]] = "not started"
            on_progress(Progress(influencer["username"], None, None, None, f"Job stopped: {exc.message}"))
            break
        except Exception as exc:  # noqa: BLE001 – e.g. profile lookup failed (not found)
            results[influencer["username"]] = f"error: {describe_error(exc)}"
            on_progress(Progress(influencer["username"], None, None, None, f"error: {describe_error(exc)}"))
            continue
        if index < len(influencers) - 1 and not cancel.is_set():
            try:
                _wait(cancel, random.uniform(settings.delay_min, settings.delay_max))
            except Cancelled:
                break
    return results
