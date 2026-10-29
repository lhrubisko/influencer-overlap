import io
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd

from follower_overlap import analysis, export

SETS = {"anna.k": {"1", "2", "3"}, "boris_ž": {"2", "3", "4"}}
NAMES = ["anna.k", "boris_ž", "no_data"]


def _report(sections=("summary", "pairwise")):
    tables = {"summary": analysis.exclusive_audience(SETS, NAMES), "pairwise": analysis.pairwise_overlap(SETS, NAMES)}
    return export.Report(title="Overlap – test", label="Observed overlap", notes=["Missing ≠ zero."],
                         totals={"distinct": 4, "pct_distinct_in_2plus": 0.5},
                         tables={k: tables[k] for k in sections})


def test_csv_single_section_is_plain_csv_and_keeps_missing_blank():
    data = export.render(_report(["summary"]), "csv")
    df = pd.read_csv(io.BytesIO(data), encoding="utf-8-sig")
    assert list(df["influencer"]) == NAMES
    assert pd.isna(df.set_index("influencer").loc["no_data", "exclusive"])
    assert export.file_name("r", "csv", ["summary"]) == ("r_summary.csv", "text/csv")


def test_csv_several_sections_is_zip():
    with zipfile.ZipFile(io.BytesIO(export.render(_report(), "csv"))) as zf:
        assert sorted(zf.namelist()) == ["pairwise.csv", "summary.csv"]
    assert export.file_name("r", "csv", ["summary", "pairwise"])[0] == "r.zip"


def test_xml_marks_missing_values():
    root = ET.fromstring(export.render(_report(), "xml"))
    rows = root.find("summary").findall("row")
    assert [r.findtext("influencer") for r in rows] == NAMES
    assert rows[2].find("exclusive").get("missing") == "true"
    assert rows[0].findtext("exclusive") == "1"
    assert len(root.find("pairwise").findall("row")) == 3


def test_pdf_renders_with_unicode():
    data = export.render(_report(), "pdf")
    assert data.startswith(b"%PDF")
