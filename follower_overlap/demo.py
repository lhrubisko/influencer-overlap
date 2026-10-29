"""Synthetic DEMO data with known overlaps. Not real Instagram data.

Follower IDs are ``demo-000001`` style strings built from integer ranges:

    demo_anna_rides      1..1000                (1000)  reported 1250 -> limit reached
    demo_bike_kids       601..1400              (800)   reported 800  -> completed
    demo_mama_outdoor    901..1200 + 1501..1800 (600)   reported 640  -> completed
    demo_family_trails   5001..5300             (300)   reported 310  -> completed, zero overlap
    demo_little_wheels   (none)                         reported 900  -> error before page 1 = MISSING data

Expected results (anna=A, bike_kids=B, mama_outdoor=C, family_trails=D):
    A∩B = 400, A∩C = 100, B∩C = 300, A∩B∩C = 100, D∩(any) = 0
    distinct(A,B,C,D) = 2000, sum collected = 2700
    exclusive: A 600, B 200, C 300, D 300
    followers of exactly k influencers: k=1 1400, k=2 500, k=3 100
"""

from __future__ import annotations

import sqlite3

from . import storage

DEMO_SNAPSHOT_NAME = "DEMO – synthetic data (not real Instagram)"


def _ids(*ranges: range) -> list[str]:
    return [f"demo-{n:06d}" for r in ranges for n in r]


DEMO_INFLUENCERS: dict[str, dict] = {
    "demo_anna_rides": {
        "ids": _ids(range(1, 1001)), "reported": 1250, "status": storage.LIMIT_REACHED,
        "reason": "Configured limit of 1000 followers reached (demo).", "limit": True, "ended": False,
    },
    "demo_bike_kids": {
        "ids": _ids(range(601, 1401)), "reported": 800, "status": storage.COMPLETED,
        "reason": "Pagination ended: no further cursor returned (demo).", "limit": False, "ended": True,
    },
    "demo_mama_outdoor": {
        "ids": _ids(range(901, 1201), range(1501, 1801)), "reported": 640, "status": storage.COMPLETED,
        "reason": "Pagination ended: no further cursor returned (demo).", "limit": False, "ended": True,
    },
    "demo_family_trails": {
        "ids": _ids(range(5001, 5301)), "reported": 310, "status": storage.COMPLETED,
        "reason": "Pagination ended: no further cursor returned (demo).", "limit": False, "ended": True,
    },
    "demo_little_wheels": {
        "ids": [], "reported": 900, "status": storage.ERROR,
        "reason": "Simulated error before the first page (demo) – no data collected.", "limit": False, "ended": False,
    },
}


def demo_follower_sets() -> dict[str, set[str]]:
    """Follower sets as the analysis layer sees them (influencers with data only)."""
    return {name: set(spec["ids"]) for name, spec in DEMO_INFLUENCERS.items() if spec["ids"]}


def load_demo(conn: sqlite3.Connection) -> int:
    """(Re)create the demo snapshot and demo influencers. Returns the demo snapshot id."""
    for snap in storage.list_snapshots(conn):
        if snap["is_demo"]:
            storage.delete_snapshot(conn, snap["id"])

    storage.add_influencers(conn, DEMO_INFLUENCERS.keys(), is_demo=True)
    by_name = {i["username"]: i for i in storage.list_influencers(conn, is_demo=True)}
    snapshot_id = storage.create_snapshot(
        conn, DEMO_SNAPSHOT_NAME, notes="Synthetic data for trying the UI. Not real Instagram data.", is_demo=True
    )

    for offset, (name, spec) in enumerate(DEMO_INFLUENCERS.items()):
        inf = by_name[name]
        ig_id = str(9_000_000_000 + offset)
        storage.update_influencer_profile(conn, inf["id"], ig_id, f"{name} (demo)", False, spec["reported"])
        run = storage.get_or_create_run(conn, snapshot_id, inf["id"])
        storage.start_run(conn, run["id"], ig_id, spec["reported"], max_followers=1000)
        ids = spec["ids"]
        for start in range(0, len(ids), 200):  # store in pages like the real collector
            page = ids[start:start + 200]
            storage.save_page(conn, run["id"], [(fid, f"user_{fid}") for fid in page], next_cursor=None)
        storage.finish_run(
            conn, run["id"], spec["status"], spec["reason"],
            pagination_ended=spec["ended"], limit_reached=spec["limit"],
            error_occurred=spec["status"] == storage.ERROR,
        )
    return snapshot_id
