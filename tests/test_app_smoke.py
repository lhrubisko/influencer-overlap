"""Headless UI smoke test: the whole app works in demo mode without Instagram credentials."""

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from follower_overlap import config


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ui.sqlite3")
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "no_session.json")
    st.cache_resource.clear()  # the DB connection is cached per process
    st.cache_data.clear()
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.run()
    assert not at.exception
    return at


def ok(at):
    assert not at.exception, at.exception
    return at


def test_demo_mode_end_to_end(app):
    at = app
    assert "load demo data" in at.markdown[0].value
    next(b for b in at.sidebar.button if b.label.startswith("Load / reset demo")).click()
    ok(at.run())

    assert any("DEMO DATA" in w.value for w in at.warning)
    assert any("demo_little_wheels" in w.value and "not zero" in w.value for w in at.warning)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Distinct collected followers"] == "2,000"
    assert metrics["Sum of collected lists"] == "2,700"

    for label in at.radio[0].options:  # counts / directional / jaccard heatmaps
        at.radio[0].set_value(label)
        ok(at.run())

    frames = [d.value for d in at.dataframe]
    pairwise = next(f for f in frames if "influencer_a" in f.columns)
    assert len(pairwise) == 10  # 5 choose 2, including pairs with missing data
    quality = next(f for f in frames if "coverage" in f.columns).set_index("influencer")
    assert quality.loc["demo_anna_rides", "coverage"] == pytest.approx(1000 / 1250)
    assert quality.loc["demo_little_wheels", "data"] == "missing"
    incremental = next(f for f in frames if "candidate" in f.columns).set_index("candidate")
    assert incremental.loc["demo_mama_outdoor", "new_unique"] == 500
    assert len(at.get("download_button")) >= 4
    assert any(b.proto.label == "⬇ Download PDF" for b in at.get("download_button"))
    assert any("Collection is disabled for the demo snapshot" in i.value for i in at.info)


def test_live_snapshot_and_adding_usernames(app):
    at = app
    at.sidebar.text_input[0].set_value("Live test")
    next(b for b in at.sidebar.button if b.label == "Create snapshot").click()
    ok(at.run())
    assert at.header[0].value == "Live test"
    assert any("No saved Instagram session" in w.value for w in at.warning)

    at.text_area[0].set_value("@Example.Brand\nhttps://www.instagram.com/some_creator/?hl=en\nbad!!")
    at.button(key="FormSubmitter:add_influencers-Add").click()
    ok(at.run())
    names = list(next(d.value for d in at.dataframe if "username" in d.value.columns)["username"])
    assert names == ["example.brand", "some_creator"]
    assert any("@example.brand" in m.value for m in at.success)
    assert any("bad!!" in e.value for e in at.error)
    # no demo influencers leak into a live snapshot
    assert all(not n.startswith("demo_") for n in at.multiselect[0].options)


def test_live_snapshot_without_data_explains_next_steps(app):
    at = app
    at.sidebar.text_input[0].set_value("Live empty")
    next(b for b in at.sidebar.button if b.label == "Create snapshot").click()
    ok(at.run())
    at.text_area[0].set_value("creator_one\ncreator_two")
    at.button(key="FormSubmitter:add_influencers-Add").click()
    ok(at.run())
    at.multiselect[0].set_value(["creator_one", "creator_two"])
    ok(at.run())
    infos = [i.value for i in at.info]
    assert sum("Not enough collected data yet" in v for v in infos) == 2  # overlap + unique tabs
    assert any("python login.py" in v for v in infos)
    assert not any("influencer_a" in d.value.columns for d in at.dataframe)  # no empty pairwise table
