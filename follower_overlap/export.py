"""Report export (PDF / CSV / XML) of the overlap results for the selected influencers.

A report is a set of named tables plus metadata. CSV gets one file per table (a ZIP when there is
more than one), XML and PDF put everything in a single document. Missing values stay empty – they
are never written as 0.
"""

from __future__ import annotations

import io
import math
import numbers
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

FORMATS = {"PDF": "pdf", "CSV": "csv", "XML": "xml"}
SECTIONS = {
    "summary": "Per-influencer summary",
    "pairwise": "Pairwise overlap",
    "quality": "Data quality",
}
# Fractions shown as percentages in the PDF.
PERCENT_COLUMNS = {"coverage", "pct_exclusive_of_own", "pct_of_group_distinct",
                   "pct_shared_of_a", "pct_shared_of_b", "jaccard", "pct_distinct_in_2plus"}
# Bookkeeping columns left out of the PDF so the tables fit an A4 page (CSV / XML keep them).
PDF_SKIP_COLUMNS = {"started_utc", "finished_utc", "configured_limit", "resumable", "pagination_ended",
                    "limit_reached", "error", "data_status"}
_WIDE_COLUMNS = {"stop_reason": 4, "influencer": 2, "influencer_a": 2, "influencer_b": 2, "status": 2, "label": 3}
_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/Library/Fonts",
              "C:/Windows/Fonts")


@dataclass
class Report:
    title: str
    label: str
    notes: list[str]
    totals: dict[str, object]
    tables: dict[str, pd.DataFrame]  # section key -> table, in output order
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0))


def _is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA


def _text(value) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def file_name(report_stem: str, fmt: str, sections: list[str]) -> tuple[str, str]:
    """(file name, MIME type) for the chosen format."""
    if fmt == "csv" and len(sections) > 1:
        return f"{report_stem}.zip", "application/zip"
    if fmt == "csv":
        return f"{report_stem}_{sections[0]}.csv", "text/csv"
    return f"{report_stem}.{fmt}", {"pdf": "application/pdf", "xml": "application/xml"}[fmt]


def render(report: Report, fmt: str) -> bytes:
    return {"csv": to_csv, "xml": to_xml, "pdf": to_pdf}[fmt](report)


# ------------------------------------------------------------------- CSV

def to_csv(report: Report) -> bytes:
    """One CSV per table (UTF-8 with BOM so Excel shows diacritics); a ZIP if there are several."""
    files = {f"{key}.csv": df.to_csv(index=False).encode("utf-8-sig") for key, df in report.tables.items()}
    if len(files) == 1:
        return next(iter(files.values()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ------------------------------------------------------------------- XML

def to_xml(report: Report) -> bytes:
    root = ET.Element("overlap_report", title=report.title, label=report.label,
                      generated_utc=report.generated_at.isoformat())
    notes = ET.SubElement(root, "notes")
    for note in report.notes:
        ET.SubElement(notes, "note").text = note
    totals = ET.SubElement(root, "group_totals")
    for key, value in report.totals.items():
        ET.SubElement(totals, key).text = _text(value)
    for key, df in report.tables.items():
        section = ET.SubElement(root, key, title=SECTIONS.get(key, key))
        for record in df.to_dict("records"):
            row = ET.SubElement(section, "row")
            for col, value in record.items():
                el = ET.SubElement(row, col)
                if _is_missing(value):
                    el.set("missing", "true")
                else:
                    el.text = _text(value)
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ------------------------------------------------------------------- PDF

def _find_font(name: str) -> Path | None:
    for d in _FONT_DIRS:
        path = Path(d) / name
        if path.exists():
            return path
    return None


def _pdf_cell(value, column: str) -> str:
    if _is_missing(value):
        return "–"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if column in PERCENT_COLUMNS and isinstance(value, numbers.Real):
        return f"{value:.1%}"
    if isinstance(value, numbers.Real) and float(value).is_integer():
        return f"{int(value):,}"
    return str(value)


def to_pdf(report: Report) -> bytes:
    from fpdf import FPDF  # imported lazily: only needed for PDF export

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    regular, bold = _find_font("DejaVuSans.ttf"), _find_font("DejaVuSans-Bold.ttf")
    if regular and bold:
        pdf.add_font("Report", "", str(regular))
        pdf.add_font("Report", "B", str(bold))
        family, clean = "Report", (lambda s: s)
    else:  # core fonts are Latin-1 only: strip diacritics / unsupported symbols
        family = "Helvetica"
        clean = (lambda s: unicodedata.normalize("NFKD", s.replace("–", "-").replace("≠", "!=").replace("≥", ">="))
                 .encode("latin-1", "ignore").decode("latin-1"))

    pdf.add_page()
    pdf.set_font(family, "B", 16)
    pdf.cell(0, 9, clean(report.title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 9)
    pdf.cell(0, 5, clean(f"{report.label} · generated {report.generated_at:%Y-%m-%d %H:%M} UTC"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    for note in report.notes:
        pdf.multi_cell(0, 4.5, clean(note), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    if report.totals:
        pdf.set_font(family, "B", 12)
        pdf.cell(0, 7, "Group totals", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(family, "", 9)
        for key, value in report.totals.items():
            pdf.cell(70, 5, clean(key.replace("_", " ")))
            pdf.cell(0, 5, clean(_pdf_cell(value, key)), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    for key, df in report.tables.items():
        pdf.set_font(family, "B", 12)
        pdf.cell(0, 7, clean(SECTIONS.get(key, key)), new_x="LMARGIN", new_y="NEXT")
        if df.empty:
            pdf.set_font(family, "", 9)
            pdf.cell(0, 5, "No rows.", new_x="LMARGIN", new_y="NEXT")
            continue
        df = df[[c for c in df.columns if c not in PDF_SKIP_COLUMNS]]
        pdf.set_font(family, "", 7 if len(df.columns) > 9 else 8)
        with pdf.table(first_row_as_headings=True, repeat_headings=1, line_height=4.5, text_align="LEFT",
                       col_widths=tuple(_WIDE_COLUMNS.get(c, 1) for c in df.columns)) as table:
            table.row([clean(c.replace("_", " ")) for c in df.columns])
            for record in df.to_dict("records"):
                table.row([clean(_pdf_cell(v, c)) for c, v in record.items()])
        pdf.ln(4)
    return bytes(pdf.output())
