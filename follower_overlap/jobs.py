"""Background collection job so the Streamlit UI never blocks.

One job at a time (sequential processing). The UI polls ``JobManager.state()``; detailed
per-run numbers are read from SQLite, which the worker updates after every page.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import collector, storage
from .config import CollectionSettings


@dataclass
class JobState:
    running: bool = False
    kind: str = ""
    current: str = ""
    current_collected: int | None = None
    current_reported: int | None = None
    done: int = 0
    total: int = 0
    results: dict[str, str] = field(default_factory=dict)
    fatal: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=200))


class JobManager:
    def __init__(self, client_factory: Callable[[], collector.InstagramClient] = collector.load_session_client,
                 db_path=None):
        self._client_factory = client_factory
        self._db_path = db_path
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = JobState()

    # -------------------------------------------------------------- public
    def state(self) -> JobState:
        with self._lock:
            s = self._state
            return JobState(s.running, s.kind, s.current, s.current_collected, s.current_reported,
                            s.done, s.total, dict(s.results), s.fatal, deque(s.log, maxlen=200))

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def active_run_ids(self, conn) -> list[int]:
        if not self.is_running():
            return []
        return [r["id"] for r in conn.execute("SELECT id FROM collection_runs WHERE status = 'running'")]

    def cancel(self) -> None:
        self._cancel.set()
        self._log("cancellation requested – stopping after the current request")

    def start(self, snapshot_id: int, influencers: list[dict], settings: CollectionSettings,
              restart: bool = False, resolve_only: bool = False) -> bool:
        if self.is_running():
            return False
        self._cancel = threading.Event()
        with self._lock:
            self._state = JobState(running=True, kind="profile check" if resolve_only else "collection",
                                   total=len(influencers))
        self._thread = threading.Thread(
            target=self._run, args=(snapshot_id, influencers, settings, restart, resolve_only),
            name="follower-collector", daemon=True,
        )
        self._thread.start()
        return True

    # ------------------------------------------------------------ internals
    def _log(self, msg: str) -> None:
        with self._lock:
            self._state.log.appendleft(f"{datetime.now():%H:%M:%S}  {msg}")

    def _on_progress(self, p: collector.Progress) -> None:
        with self._lock:
            if p.username != self._state.current:
                if self._state.current:
                    self._state.done += 1
                self._state.current = p.username
                self._state.current_collected = None
            if p.collected is not None:
                self._state.current_collected = p.collected
                self._state.current_reported = p.reported
        self._log(f"@{p.username}: {p.message}")

    def _run(self, snapshot_id, influencers, settings, restart, resolve_only) -> None:
        conn = storage.connect(self._db_path)
        try:
            self._log("validating saved Instagram session …")
            try:
                client = self._client_factory()
            except Exception as exc:  # noqa: BLE001
                msg = str(exc) if isinstance(exc, collector.SessionError) else collector.describe_error(exc)
                with self._lock:
                    self._state.fatal = msg
                self._log(f"cannot start: {msg}")
                return
            results = collector.collect_many(client, conn, snapshot_id, influencers, settings, self._cancel,
                                             self._on_progress, restart=restart, resolve_only=resolve_only)
            with self._lock:
                self._state.results = results
                self._state.done = sum(1 for v in results.values() if v != "not started")
            self._log("job finished" if not self._cancel.is_set() else "job cancelled")
        except Exception as exc:  # noqa: BLE001 – never let the worker die silently
            with self._lock:
                self._state.fatal = collector.describe_error(exc)
            self._log(f"job crashed: {collector.describe_error(exc)}")
        finally:
            conn.close()
            with self._lock:
                self._state.running = False
                self._state.current = ""
