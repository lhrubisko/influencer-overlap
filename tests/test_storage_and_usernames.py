import pytest

from follower_overlap import demo, storage
from follower_overlap.usernames import normalize_username, parse_username_csv, parse_username_list


@pytest.fixture
def conn(tmp_path):
    c = storage.connect(tmp_path / "t.sqlite3")
    yield c
    c.close()


def _run(conn, name="creator"):
    storage.add_influencers(conn, [name])
    inf = next(i for i in storage.list_influencers(conn) if i["username"] == name)
    snap = storage.create_snapshot(conn, "s")
    return snap, inf, storage.get_or_create_run(conn, snap, inf["id"])


@pytest.mark.parametrize("raw,expected", [
    ("example.brand", "example.brand"),
    ("@Example_Brand", "example_brand"),
    ("https://www.instagram.com/example.brand/", "example.brand"),
    ("instagram.com/example.brand?hl=sk", "example.brand"),
    ("https://instagram.com/p/ABC123/", None),
    ("https://example.com/brand", None),
    ("not a name!", None),
    ("", None),
])
def test_normalize_username(raw, expected):
    assert normalize_username(raw) == expected


def test_parse_list_dedupes_and_reports_rejects():
    valid, rejected = parse_username_list("@a_b\nhttps://instagram.com/a_b/\n c.d , bad!name")
    assert valid == ["a_b", "c.d"]
    assert rejected == ["bad!name"]


def test_parse_csv_uses_named_column_and_semicolons():
    data = "Name;Instagram;Notes\nAnna;@anna.k;x\nBoris;https://www.instagram.com/boris_b/;\nCyril;bad!name;\n"
    valid, rejected = parse_username_csv(data.encode("cp1250"))
    assert valid == ["anna.k", "boris_b"]
    assert rejected == ["bad!name"]


def test_parse_csv_without_header_uses_first_column():
    valid, rejected = parse_username_csv("\ufeffa_b,123\nc.d,456\na_b,789\n".encode())
    assert valid == ["a_b", "c.d"]
    assert rejected == []


def test_followers_deduplicated_per_run(conn):
    _, _, run = _run(conn)
    assert storage.save_page(conn, run["id"], [("1", "u1"), ("2", "u2"), ("2", "u2")], "cur1") == 2
    assert storage.save_page(conn, run["id"], [("2", "u2"), ("3", "u3")], "cur2") == 1
    r = storage.get_run(conn, run["id"])
    assert r["collected_count"] == 3 and r["pages_fetched"] == 2 and r["next_cursor"] == "cur2"


def test_ids_stored_as_strings(conn):
    _, _, run = _run(conn)
    storage.save_page(conn, run["id"], [(12345678901234567, "big")], None)
    (fid,) = conn.execute("SELECT follower_id FROM followers").fetchone()
    assert fid == "12345678901234567" and isinstance(fid, str)


def test_missing_vs_measured_empty(conn):
    snap, inf, run = _run(conn)
    storage.start_run(conn, run["id"], "99", 10, 100)
    storage.finish_run(conn, run["id"], storage.ERROR, "boom", error_occurred=True)
    assert storage.load_follower_sets(conn, snap) == {}  # error with no IDs -> missing
    storage.start_run(conn, run["id"], "99", 0, 100)
    storage.finish_run(conn, run["id"], storage.COMPLETED, "done", pagination_ended=True)
    assert storage.load_follower_sets(conn, snap) == {"creator": set()}  # measured empty


def test_partial_data_survives_error_and_counts(conn):
    snap, _, run = _run(conn)
    storage.start_run(conn, run["id"], "99", 10, 100)
    storage.save_page(conn, run["id"], [("1", None), ("2", None)], "c")
    storage.finish_run(conn, run["id"], storage.ERROR, "boom", error_occurred=True)
    assert storage.load_follower_sets(conn, snap) == {"creator": {"1", "2"}}
    assert storage.get_run(conn, run["id"])["next_cursor"] == "c"  # still resumable


def test_snapshots_are_isolated(conn):
    snap1, inf, run1 = _run(conn)
    snap2 = storage.create_snapshot(conn, "later")
    run2 = storage.get_or_create_run(conn, snap2, inf["id"])
    storage.save_page(conn, run1["id"], [("1", None)], None)
    storage.save_page(conn, run2["id"], [("2", None)], None)
    assert storage.load_follower_sets(conn, snap1) == {"creator": {"1"}}
    assert storage.load_follower_sets(conn, snap2) == {"creator": {"2"}}


def test_orphaned_running_runs_marked_interrupted(conn):
    _, _, run = _run(conn)
    storage.start_run(conn, run["id"], "99", 10, 100)
    assert storage.mark_orphaned_runs_interrupted(conn) == 1
    assert storage.get_run(conn, run["id"])["status"] == storage.INTERRUPTED


def test_demo_loads_with_expected_counts_and_is_flagged(conn):
    snap = demo.load_demo(conn)
    snap2 = demo.load_demo(conn)  # idempotent reset
    snaps = storage.list_snapshots(conn)
    assert len(snaps) == 1 and snaps[0]["is_demo"] == 1 and snap2 != snap
    sets = storage.load_follower_sets(conn, snap2)
    assert sets == demo.demo_follower_sets()
    assert "demo_little_wheels" not in sets
    assert all(i["is_demo"] for i in storage.list_influencers(conn))


def test_default_snapshot_is_the_one_with_latest_activity(conn):
    snap1, inf, run = _run(conn)
    storage.save_page(conn, run["id"], [("1", None)], None)
    storage.create_snapshot(conn, "newer but empty")
    assert storage.default_snapshot_id(conn, is_demo=False) == snap1
    assert storage.snapshot_activity(conn)[snap1]["collected"] == 1
