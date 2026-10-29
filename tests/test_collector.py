"""Collector logic against a fake instagrapi client (no network)."""

import threading
from types import SimpleNamespace

import pytest
from instagrapi import exceptions as igx

from follower_overlap import collector, storage
from follower_overlap.config import CollectionSettings

FAST = CollectionSettings(page_size=2, max_followers=0, delay_min=0, delay_max=0, max_retries=2, backoff_seconds=0.01)


class FakeClient:
    """Pages of follower IDs; cursor = index of next page as a string."""

    def __init__(self, pages, follower_count=100, errors=None):
        self.pages = pages
        self.follower_count = follower_count
        self.errors = list(errors or [])  # exceptions raised on successive follower calls
        self.calls = []

    def user_info_by_username(self, username):
        return SimpleNamespace(pk=4242, follower_count=self.follower_count, is_private=False, full_name="X")

    def user_followers_v1_chunk(self, user_id, max_amount=0, max_id=""):
        self.calls.append((user_id, max_amount, max_id))
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        idx = int(max_id) if max_id else 0
        users = [SimpleNamespace(pk=int(p), username=f"u{p}") for p in self.pages[idx]]
        nxt = str(idx + 1) if idx + 1 < len(self.pages) else ""
        return users, nxt


@pytest.fixture
def env(tmp_path):
    conn = storage.connect(tmp_path / "c.sqlite3")
    storage.add_influencers(conn, ["creator"])
    inf = storage.list_influencers(conn)[0]
    snap = storage.create_snapshot(conn, "live")
    yield conn, snap, inf
    conn.close()


def run_of(conn, snap, inf):
    return storage.get_or_create_run(conn, snap, inf["id"])


def test_collects_all_pages_dedupes_and_records_profile(env):
    conn, snap, inf = env
    client = FakeClient([["1", "2"], ["2", "3"], ["4"]], follower_count=4)
    status = collector.collect_one(client, conn, snap, inf, FAST, threading.Event())
    assert status == storage.COMPLETED
    run = run_of(conn, snap, inf)
    assert run["collected_count"] == 4 and run["pagination_ended"] == 1 and run["next_cursor"] is None
    assert run["ig_user_id"] == "4242" and run["reported_follower_count"] == 4
    assert run["started_at"] and run["finished_at"]
    assert "does not guarantee" in run["stop_reason"]
    assert storage.load_follower_sets(conn, snap)["creator"] == {"1", "2", "3", "4"}
    assert storage.get_influencer(conn, inf["id"])["ig_user_id"] == "4242"


def test_limit_reached(env):
    conn, snap, inf = env
    client = FakeClient([["1", "2"], ["3", "4"], ["5", "6"]])
    settings = CollectionSettings(2, 3, 0, 0, 0, 0.01)
    assert collector.collect_one(client, conn, snap, inf, settings, threading.Event()) == storage.LIMIT_REACHED
    run = run_of(conn, snap, inf)
    assert run["limit_reached"] == 1 and run["pagination_ended"] == 0
    assert client.calls[1][1] == 1  # second request only asks for the remaining 1


def test_rate_limit_bounded_backoff_then_stop_and_resume(env):
    conn, snap, inf = env
    errors = [None] + [igx.PleaseWaitFewMinutes("wait")] * 3  # page 1 ok, then 3 rate limits
    client = FakeClient([["1", "2"], ["3"]], errors=errors)
    with pytest.raises(collector.StopJob) as exc:
        collector.collect_one(client, conn, snap, inf, FAST, threading.Event())
    assert exc.value.status == storage.RATE_LIMITED
    assert len(client.calls) == 4  # 1 ok + 1 attempt + 2 bounded retries
    run = run_of(conn, snap, inf)
    assert run["status"] == storage.RATE_LIMITED and run["collected_count"] == 2 and run["next_cursor"] == "1"

    # resume continues from the saved cursor, not page 0
    client.errors = []
    assert collector.collect_one(client, conn, snap, inf, FAST, threading.Event()) == storage.COMPLETED
    assert client.calls[-1][2] == "1"
    assert run_of(conn, snap, inf)["collected_count"] == 3


def test_transient_error_recovers_within_retry_budget(env):
    conn, snap, inf = env
    client = FakeClient([["1"]], errors=[igx.ClientConnectionError("net")])
    assert collector.collect_one(client, conn, snap, inf, FAST, threading.Event()) == storage.COMPLETED


def test_challenge_stops_job_without_retry(env):
    conn, snap, inf = env
    client = FakeClient([["1"], ["2"]], errors=[None, igx.ChallengeRequired("challenge_required")])
    with pytest.raises(collector.StopJob) as exc:
        collector.collect_one(client, conn, snap, inf, FAST, threading.Event())
    assert exc.value.status == storage.AUTH_REQUIRED and "verification" in exc.value.message
    assert len(client.calls) == 2
    run = run_of(conn, snap, inf)
    assert run["status"] == storage.AUTH_REQUIRED and run["collected_count"] == 1


def test_cancel_keeps_partial_data(env):
    conn, snap, inf = env
    cancel = threading.Event()
    client = FakeClient([["1"], ["2"], ["3"]])

    def progress(p):
        if p.collected == 1:
            cancel.set()

    with pytest.raises(collector.Cancelled):
        collector.collect_one(client, conn, snap, inf, FAST, cancel, progress)
    run = run_of(conn, snap, inf)
    assert run["status"] == storage.CANCELLED and run["collected_count"] == 1 and run["next_cursor"] == "1"


def test_unexpected_error_is_recorded_and_partial_kept(env):
    conn, snap, inf = env
    client = FakeClient([["1"], ["2"]], errors=[None, igx.PrivateError("Not authorized to view user")])
    assert collector.collect_one(client, conn, snap, inf, FAST, threading.Event()) == storage.ERROR
    run = run_of(conn, snap, inf)
    assert run["error_occurred"] == 1 and run["collected_count"] == 1 and "PrivateError" in run["stop_reason"]


def test_stalled_cursor_guard(env):
    conn, snap, inf = env
    client = FakeClient([["1"]] * 10)  # every page repeats the same follower
    assert collector.collect_one(client, conn, snap, inf, FAST, threading.Event()) == storage.ERROR
    assert len(client.calls) == 1 + collector.MAX_STALLED_PAGES


def test_profile_lookup_failure_recorded_on_run(env):
    conn, snap, inf = env

    class Missing(FakeClient):
        def user_info_by_username(self, username):
            raise igx.UserNotFound("nope")

    assert collector.collect_one(Missing([]), conn, snap, inf, FAST, threading.Event()) == storage.ERROR
    assert "Profile lookup failed" in run_of(conn, snap, inf)["stop_reason"]


def test_collect_many_stops_whole_job_on_auth_error(env):
    conn, snap, inf = env
    storage.add_influencers(conn, ["second"])
    infs = storage.list_influencers(conn)
    client = FakeClient([["1"]], errors=[igx.LoginRequired("login_required")])
    results = collector.collect_many(client, conn, snap, infs, FAST, threading.Event())
    assert results == {"creator": storage.AUTH_REQUIRED, "second": "not started"}


def test_new_client_never_prompts_for_challenge():
    client = collector.new_client()
    assert client.with_challenge_flow is False
    with pytest.raises(igx.ChallengeRequired):
        client.challenge_code_handler("user", "email")


def _wait_done(jobs, timeout=5):
    jobs._thread.join(timeout)
    assert not jobs.is_running()


def test_job_manager_runs_in_background_and_can_cancel(tmp_path):
    from follower_overlap.jobs import JobManager

    db = tmp_path / "j.sqlite3"
    conn = storage.connect(db)
    storage.add_influencers(conn, ["creator"])
    infs = storage.list_influencers(conn)
    snap = storage.create_snapshot(conn, "live")
    slow = CollectionSettings(2, 0, 30, 30, 0, 1)  # 30 s delay between pages -> cancel must interrupt it
    jobs = JobManager(client_factory=lambda: FakeClient([["1"], ["2"]]), db_path=db)
    assert jobs.start(snap, infs, slow)
    assert not jobs.start(snap, infs, slow)  # one job at a time
    for _ in range(100):
        if storage.list_runs(conn, snap) and storage.list_runs(conn, snap)[0]["collected_count"] == 1:
            break
        threading.Event().wait(0.02)
    jobs.cancel()
    _wait_done(jobs)
    run = storage.list_runs(conn, snap)[0]
    assert run["status"] == storage.CANCELLED and run["collected_count"] == 1
    assert jobs.state().results == {"creator": storage.CANCELLED}


def test_job_manager_reports_missing_session(tmp_path):
    from follower_overlap.jobs import JobManager

    def no_session():
        raise collector.SessionError("No saved session")

    jobs = JobManager(client_factory=no_session, db_path=tmp_path / "j.sqlite3")
    jobs.start(1, [], FAST)
    _wait_done(jobs)
    assert "No saved session" in jobs.state().fatal


def test_pagination_ending_far_below_reported_is_flagged_truncated(env):
    conn, snap, inf = env
    client = FakeClient([["1", "2"]], follower_count=116_398)  # one page, no cursor
    assert collector.collect_one(client, conn, snap, inf, FAST, threading.Event()) == storage.COMPLETED
    run = run_of(conn, snap, inf)
    assert "cut short by Instagram" in run["stop_reason"]
    assert storage.is_truncated(run["collected_count"], run["reported_follower_count"], True)
    assert not storage.is_truncated(95, 100, True) and not storage.is_truncated(5, 100, False)
