import math

import pytest

from follower_overlap import analysis
from follower_overlap.demo import demo_follower_sets

A, B, C, D, E = "demo_anna_rides", "demo_bike_kids", "demo_mama_outdoor", "demo_family_trails", "demo_little_wheels"
ALL = [A, B, C, D, E]


@pytest.fixture
def sets():
    return demo_follower_sets()


def pair(df, a, b):
    row = df[(df.influencer_a == a) & (df.influencer_b == b)]
    assert len(row) == 1
    return row.iloc[0]


def test_pairwise_shared_directional_and_jaccard(sets):
    df = analysis.pairwise_overlap(sets, [A, B, C, D])
    ab = pair(df, A, B)
    assert (ab.collected_a, ab.collected_b, ab.shared, ab.union) == (1000, 800, 400, 1400)
    assert ab.pct_shared_of_a == pytest.approx(400 / 1000)
    assert ab.pct_shared_of_b == pytest.approx(400 / 800)
    assert ab.jaccard == pytest.approx(400 / 1400)
    assert pair(df, A, C).shared == 100
    assert pair(df, B, C).shared == 300
    assert len(df) == 6  # 4 choose 2


def test_measured_zero_overlap_is_zero_not_missing(sets):
    row = pair(analysis.pairwise_overlap(sets, [A, D]), A, D)
    assert row.shared == 0
    assert row.pct_shared_of_a == 0 and row.jaccard == 0
    assert row.data_status == "measured"


def test_missing_influencer_is_missing_not_zero(sets):
    row = pair(analysis.pairwise_overlap(sets, [A, E]), A, E)
    assert row.data_status.startswith("missing")
    assert row.shared is None or math.isnan(row.shared)
    m = analysis.overlap_matrix(sets, [A, E], "shared")
    assert math.isnan(m.loc[A, E]) and math.isnan(m.loc[E, E])
    assert m.loc[A, A] == 1000


def test_directional_matrix_is_row_normalised(sets):
    m = analysis.overlap_matrix(sets, [A, B], "pct_of_row")
    assert m.loc[A, B] == pytest.approx(0.4)   # shared / collected(A)
    assert m.loc[B, A] == pytest.approx(0.5)   # shared / collected(B)
    assert m.loc[A, A] == pytest.approx(1.0)
    j = analysis.overlap_matrix(sets, [A, B], "jaccard")
    assert j.loc[A, B] == j.loc[B, A] == pytest.approx(400 / 1400)


def test_group_summary_multiway(sets):
    g = analysis.group_summary(sets, ALL)
    assert g["total_distinct"] == 2000
    assert g["sum_collected"] == 2700
    assert g["duplicate_appearances"] == 700
    assert g["membership_distribution"] == {1: 1400, 2: 500, 3: 100}
    assert g["followers_in_2plus"] == 600
    assert g["influencers_missing_data"] == [E]


def test_group_union_is_not_sum_of_pairwise(sets):
    # A,B,C share 100 IDs three ways; summing pairwise intersections would double count them.
    g = analysis.group_summary(sets, [A, B, C])
    pairwise_sum = 400 + 100 + 300
    inclusion_exclusion = 1000 + 800 + 600 - pairwise_sum + 100
    assert g["total_distinct"] == inclusion_exclusion == 1700
    assert g["total_distinct"] != 1000 + 800 + 600 - pairwise_sum


def test_exclusive_audience(sets):
    df = analysis.exclusive_audience(sets, ALL).set_index("influencer")
    assert df.loc[A, "exclusive"] == 600
    assert df.loc[B, "exclusive"] == 200
    assert df.loc[C, "exclusive"] == 300
    assert df.loc[D, "exclusive"] == 300
    assert df.loc[D, "pct_exclusive_of_own"] == 1.0
    assert df.loc[A, "pct_of_group_distinct"] == pytest.approx(600 / 2000)
    assert df.loc[E, "data_status"] == "missing data"
    # exclusivity depends on the selected group only
    only_ab = analysis.exclusive_audience(sets, [A, B]).set_index("influencer")
    assert only_ab.loc[B, "exclusive"] == 400


def test_incremental_unique(sets):
    df = analysis.incremental_unique(sets, [A], [B, C, D, E]).set_index("candidate")
    assert df.loc[B, "new_unique"] == 400 and df.loc[B, "already_in_group"] == 400
    assert df.loc[C, "new_unique"] == 500  # 1001..1200 + 1501..1800
    assert df.loc[D, "new_unique"] == 300
    assert df.loc[B, "group_distinct_before"] == 1000 and df.loc[B, "group_distinct_after"] == 1400
    assert df.loc[C, "group_growth_pct"] == pytest.approx(0.5)
    assert df.loc[E, "data_status"] == "missing data"
    # base group with two members uses their union
    df2 = analysis.incremental_unique(sets, [A, B], [C]).set_index("candidate")
    assert df2.loc[C, "new_unique"] == 300 and df2.loc[C, "group_distinct_before"] == 1400


def test_empty_inputs_do_not_divide_by_zero():
    assert analysis.pairwise_overlap({}, []).empty
    g = analysis.group_summary({}, [])
    assert g["total_distinct"] == 0 and g["pct_distinct_in_2plus"] is None
    assert analysis.exclusive_audience({}, []).empty
    assert analysis.incremental_unique({}, [], []).empty
    # influencers measured with empty lists (completed, 0 followers)
    sets = {"x": set(), "y": set()}
    row = analysis.pairwise_overlap(sets, ["x", "y"]).iloc[0]
    assert row.shared == 0 and row.pct_shared_of_a is None and row.jaccard is None
    assert math.isnan(analysis.overlap_matrix(sets, ["x", "y"], "pct_of_row").loc["x", "y"])
    inc = analysis.incremental_unique(sets, ["x"], ["y"]).iloc[0]
    assert inc.new_unique == 0 and inc.group_growth_pct is None


def test_coverage():
    assert analysis.coverage(500, 1000) == 0.5
    assert analysis.coverage(10, 0) is None
    assert analysis.coverage(10, None) is None
