"""Audience-overlap calculations on collected follower-ID sets.

Inputs are ``{influencer: set(follower_id)}`` for influencers that HAVE data, plus the list of
selected influencer names. A selected name that is absent from the mapping is *missing data* and
yields ``NaN`` / ``None`` — never a measured zero.

All numbers describe the *collected* follower lists only ("observed overlap in collected data").
Group-level figures are computed from real set unions, never by summing pairwise intersections.
"""

from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Mapping

import pandas as pd

FollowerSets = Mapping[str, set[str]]

MATRIX_METRICS = {
    "shared": "Shared followers (count)",
    "pct_of_row": "Shared / collected(row influencer)",
    "jaccard": "Jaccard: shared / union",
}


def safe_div(numerator: float, denominator: float) -> float | None:
    """Division that returns None for a zero denominator instead of raising."""
    return None if not denominator else numerator / denominator


def _split(sets: FollowerSets, names: list[str]) -> tuple[list[str], list[str]]:
    present = [n for n in names if n in sets]
    missing = [n for n in names if n not in sets]
    return present, missing


def pairwise_overlap(sets: FollowerSets, names: list[str]) -> pd.DataFrame:
    """One row per unordered pair (A, B).

    pct_shared_of_a = shared / collected(A)   -> "what share of A's collected followers also follow B"
    pct_shared_of_b = shared / collected(B)
    jaccard         = shared / |A ∪ B|
    """
    rows = []
    for a, b in combinations(names, 2):
        if a not in sets or b not in sets:
            missing = [n for n in (a, b) if n not in sets]
            rows.append({
                "influencer_a": a, "influencer_b": b,
                "collected_a": len(sets[a]) if a in sets else None,
                "collected_b": len(sets[b]) if b in sets else None,
                "shared": None, "union": None,
                "pct_shared_of_a": None, "pct_shared_of_b": None, "jaccard": None,
                "data_status": "missing data: " + ", ".join(missing),
            })
            continue
        sa, sb = sets[a], sets[b]
        shared = len(sa & sb)
        union = len(sa | sb)
        rows.append({
            "influencer_a": a, "influencer_b": b,
            "collected_a": len(sa), "collected_b": len(sb),
            "shared": shared, "union": union,
            "pct_shared_of_a": safe_div(shared, len(sa)),
            "pct_shared_of_b": safe_div(shared, len(sb)),
            "jaccard": safe_div(shared, union),
            "data_status": "measured",
        })
    columns = ["influencer_a", "influencer_b", "collected_a", "collected_b", "shared", "union",
               "pct_shared_of_a", "pct_shared_of_b", "jaccard", "data_status"]
    return pd.DataFrame(rows, columns=columns)


def overlap_matrix(sets: FollowerSets, names: list[str], metric: str = "shared") -> pd.DataFrame:
    """Square matrix (rows/cols = names). NaN where either side has no data or denominator is 0.

    ``pct_of_row`` is directional: cell [A, B] = shared(A, B) / collected(A).
    """
    if metric not in MATRIX_METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    matrix = pd.DataFrame(float("nan"), index=names, columns=names, dtype="float64")
    for a in names:
        for b in names:
            if a not in sets or b not in sets:
                continue
            sa, sb = sets[a], sets[b]
            shared = len(sa & sb)
            if metric == "shared":
                value = shared
            elif metric == "pct_of_row":
                value = safe_div(shared, len(sa))
            else:
                value = safe_div(shared, len(sa | sb))
            if value is not None:
                matrix.loc[a, b] = value
    return matrix


def group_summary(sets: FollowerSets, names: list[str]) -> dict:
    """Group-level unique-audience figures for the selected influencers that have data."""
    present, missing = _split(sets, names)
    counts: Counter[str] = Counter()
    for n in present:
        counts.update(sets[n])
    total_distinct = len(counts)
    sum_collected = sum(len(sets[n]) for n in present)
    in_multiple = sum(1 for c in counts.values() if c >= 2)
    distribution = Counter(counts.values())  # k -> number of followers who follow exactly k influencers
    return {
        "influencers_with_data": present,
        "influencers_missing_data": missing,
        "sum_collected": sum_collected,
        "total_distinct": total_distinct,
        "duplicate_appearances": sum_collected - total_distinct,
        "followers_in_2plus": in_multiple,
        "pct_distinct_in_2plus": safe_div(in_multiple, total_distinct),
        "membership_distribution": dict(sorted(distribution.items())),
    }


def exclusive_audience(sets: FollowerSets, names: list[str]) -> pd.DataFrame:
    """Followers of A minus the union of all *other selected* influencers (that have data)."""
    present, missing = _split(sets, names)
    counts: Counter[str] = Counter()
    for n in present:
        counts.update(sets[n])
    total_distinct = len(counts)
    rows = []
    for n in names:
        if n not in sets:
            rows.append({"influencer": n, "collected": None, "exclusive": None,
                         "pct_exclusive_of_own": None, "pct_of_group_distinct": None,
                         "data_status": "missing data"})
            continue
        # an ID is exclusive to n iff n is the only selected influencer it appears for
        exclusive = sum(1 for fid in sets[n] if counts[fid] == 1)
        rows.append({
            "influencer": n,
            "collected": len(sets[n]),
            "exclusive": exclusive,
            "pct_exclusive_of_own": safe_div(exclusive, len(sets[n])),
            "pct_of_group_distinct": safe_div(exclusive, total_distinct),
            "data_status": "measured" if not missing else "measured (group has missing members)",
        })
    return pd.DataFrame(rows, columns=["influencer", "collected", "exclusive", "pct_exclusive_of_own",
                                       "pct_of_group_distinct", "data_status"])


def incremental_unique(sets: FollowerSets, base_group: list[str], candidates: list[str]) -> pd.DataFrame:
    """How many new distinct followers each candidate adds on top of ``base_group`` (evaluated individually)."""
    base_present, base_missing = _split(sets, base_group)
    base_union: set[str] = set().union(*(sets[n] for n in base_present)) if base_present else set()
    before = len(base_union)
    rows = []
    for c in candidates:
        if c in base_group:
            continue
        if c not in sets:
            rows.append({"candidate": c, "collected": None, "already_in_group": None, "new_unique": None,
                         "pct_new_of_candidate": None, "group_distinct_before": before,
                         "group_distinct_after": None, "group_growth_pct": None, "data_status": "missing data"})
            continue
        cand = sets[c]
        new = len(cand - base_union)
        rows.append({
            "candidate": c,
            "collected": len(cand),
            "already_in_group": len(cand) - new,
            "new_unique": new,
            "pct_new_of_candidate": safe_div(new, len(cand)),
            "group_distinct_before": before,
            "group_distinct_after": before + new,
            "group_growth_pct": safe_div(new, before),
            "data_status": "measured" if not base_missing else
                           "measured; base group missing: " + ", ".join(base_missing),
        })
    df = pd.DataFrame(rows, columns=["candidate", "collected", "already_in_group", "new_unique",
                                     "pct_new_of_candidate", "group_distinct_before", "group_distinct_after",
                                     "group_growth_pct", "data_status"])
    return df.sort_values("new_unique", ascending=False, na_position="last").reset_index(drop=True)


def coverage(collected: int | None, reported: int | None) -> float | None:
    """collected / reported follower count. None when either is unknown or reported is 0."""
    if collected is None or reported is None:
        return None
    return safe_div(collected, reported)
