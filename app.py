"""Influencer Followers Overlap – Streamlit UI.

Run:  streamlit run app.py      (binds to localhost via .streamlit/config.toml)
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from follower_overlap import analysis, collector, config, demo, export, storage
from follower_overlap.config import CollectionSettings
from follower_overlap.jobs import JobManager
from follower_overlap.usernames import parse_username_csv, parse_username_list

st.set_page_config(page_title="Influencer Followers Overlap", page_icon="📊", layout="wide")

OBSERVED_LABEL = "Observed overlap in collected data"
REACH_NOTE = ("Follower overlap ≠ campaign reach. Followers may be inactive, bots, or never see a post; "
              "reach depends on the algorithm, timing and content.")
STATUS_LABELS = {
    storage.PENDING: "⏳ pending", storage.RUNNING: "🔄 running", storage.COMPLETED: "✅ pagination ended",
    storage.LIMIT_REACHED: "🟨 limit reached", storage.CANCELLED: "⏹ cancelled",
    storage.RATE_LIMITED: "🛑 rate limited", storage.AUTH_REQUIRED: "🔐 login/checkpoint needed",
    storage.ERROR: "❌ error", storage.INTERRUPTED: "⚠️ interrupted",
}
# Sequential blue ramp (light -> dark) for magnitude.
HEAT_SCALE = [[0.0, "#cde2fb"], [0.25, "#86b6ef"], [0.5, "#3987e5"], [0.75, "#1c5cab"], [1.0, "#0d366b"]]
PCT = st.column_config.NumberColumn(format="percent")


# ------------------------------------------------------------------ resources

@st.cache_resource
def get_conn():
    return storage.connect()


@st.cache_resource
def get_jobs() -> JobManager:
    jobs = JobManager()
    storage.mark_orphaned_runs_interrupted(storage.connect())  # from a previous app process
    return jobs


@st.cache_data(show_spinner="Loading follower sets …")
def cached_sets(snapshot_id: int, fingerprint: tuple) -> dict[str, set[str]]:
    return storage.load_follower_sets(storage.connect(), snapshot_id)


conn = get_conn()
jobs = get_jobs()


def follower_sets(snapshot_id: int, runs: list[dict]) -> dict[str, set[str]]:
    fingerprint = tuple((r["id"], r["collected_count"], r["status"], r["updated_at"]) for r in runs)
    return cached_sets(snapshot_id, fingerprint)


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8")


# -------------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("📊 Followers Overlap")

    snapshots = storage.list_snapshots(conn)
    if not snapshots:
        st.info("No snapshots yet. Load demo data or create a live snapshot below.")
    activity = storage.snapshot_activity(conn)

    def snapshot_label(s: dict) -> str:
        a = activity.get(s["id"])
        size = f"{a['collected']:,} followers" if a else "empty"
        return f"{'🧪 ' if s['is_demo'] else ''}{s['name']}  (#{s['id']}, {size})"

    labels = {s["id"]: snapshot_label(s) for s in snapshots}
    ids = list(labels)
    # Selection survives page reloads and app restarts via the URL (?snapshot=<id>).
    try:
        url_id = int(st.query_params.get("snapshot", ""))
    except ValueError:
        url_id = None
    default_id = next((i for i in (st.session_state.get("snapshot_id"), url_id) if i in ids), None) \
        or storage.default_snapshot_id(conn, is_demo=False)
    choice = st.selectbox("Analysis snapshot", list(labels.values()),
                          index=ids.index(default_id) if default_id in ids else 0) if ids else None
    snapshot_id = ids[list(labels.values()).index(choice)] if choice else None
    st.session_state["snapshot_id"] = snapshot_id
    if snapshot_id is not None:
        st.query_params["snapshot"] = str(snapshot_id)
    snapshot = storage.get_snapshot(conn, snapshot_id) if snapshot_id else None

    with st.expander("New live snapshot"):
        name = st.text_input("Name", value=f"Live {date.today().isoformat()}")
        notes = st.text_input("Notes (optional)")
        duplicate = name.strip() in {s["name"] for s in snapshots}
        if duplicate:
            st.caption("⚠️ A snapshot with this name already exists – choose a different name.")
        if st.button("Create snapshot", disabled=not name.strip() or duplicate):
            st.session_state["snapshot_id"] = storage.create_snapshot(conn, name, notes)
            st.rerun()

    with st.expander("Demo data"):
        st.caption("Synthetic influencers with known overlaps. Clearly marked 🧪 DEMO; never mixed into live snapshots.")
        if st.button("Load / reset demo data", disabled=jobs.is_running()):
            st.session_state["snapshot_id"] = demo.load_demo(conn)
            st.cache_data.clear()
            st.rerun()

    if snapshot and not snapshot["is_demo"]:
        with st.expander("Delete this snapshot"):
            st.caption("Deletes all collection runs and follower IDs in this snapshot.")
            if st.checkbox("I understand") and st.button("Delete snapshot", disabled=jobs.is_running()):
                storage.delete_snapshot(conn, snapshot_id)
                st.session_state.pop("snapshot_id", None)
                st.rerun()

    st.divider()
    st.caption(f"Instagram session: {collector.session_status()}")
    st.caption(f"Database: `{config.DB_PATH.relative_to(config.PROJECT_ROOT) if config.DB_PATH.is_relative_to(config.PROJECT_ROOT) else config.DB_PATH}`")

if snapshot is None:
    st.header("Influencer Followers Overlap")
    st.write("Create a snapshot or load demo data from the sidebar to begin.")
    st.stop()

is_demo = bool(snapshot["is_demo"])
if is_demo:
    st.warning("🧪 **DEMO DATA** – synthetic followers, not real Instagram accounts. "
               "Numbers are constructed to have known overlaps.")

runs = storage.list_runs(conn, snapshot_id)
runs_by_name = {r["username"]: r for r in runs}
pool = storage.list_influencers(conn, is_demo=is_demo)
pool_names = [i["username"] for i in pool]

head_title, head_export = st.columns([5, 1], vertical_alignment="bottom")
head_title.header(f"{'🧪 ' if is_demo else ''}{snapshot['name']}")
if snapshot.get("notes"):
    st.caption(snapshot["notes"])

selected = st.multiselect(
    "Influencers to compare (this snapshot)",
    pool_names,
    default=[n for n in pool_names if n in runs_by_name],
    help="Only influencers with collected data in this snapshot contribute numbers; others are shown as missing.",
)

sets = follower_sets(snapshot_id, runs)
missing = [n for n in selected if n not in sets]
with_data = [n for n in selected if n in sets]

tab_inf, tab_collect, tab_quality, tab_overlap, tab_unique, tab_about = st.tabs(
    ["Influencers", "Collection", "Data quality", "Pairwise overlap", "Unique audience", "Limitations"])


# --------------------------------------------------------------- influencers

with tab_inf:
    if is_demo:
        st.info("Demo snapshot: the demo influencers are fixed. Switch to a live snapshot to manage real usernames.")
    else:
        with st.form("add_influencers", clear_on_submit=True):
            text = st.text_area("Add usernames – one per line; `@name` and instagram.com URLs accepted",
                                placeholder="@your.brand\nhttps://www.instagram.com/some_creator/\nanother_creator")
            upload = st.file_uploader("…or upload a CSV", type=["csv", "txt"],
                                      help="Uses the `username` / `instagram` / `url` column if the file has a "
                                           "header, otherwise the first column. `,` `;` and tab delimiters work.")
            if st.form_submit_button("Add"):
                valid, rejected = parse_username_list(text)
                if upload is not None:
                    csv_valid, csv_rejected = parse_username_csv(upload.getvalue())
                    valid += [v for v in csv_valid if v not in valid]
                    rejected += csv_rejected
                added, existing = storage.add_influencers(conn, valid)
                st.session_state["add_result"] = (added, existing, rejected)
                st.rerun()  # refresh the influencer pickers; messages are shown below after the rerun
        added, existing, rejected = st.session_state.pop("add_result", ([], [], []))
        if added:
            st.success(f"Added: {', '.join('@' + a for a in added)}")
        if existing:
            st.info(f"Already present: {', '.join('@' + e for e in existing)}")
        if rejected:
            st.error(f"Not recognised as Instagram usernames: {', '.join(rejected)}")

    if pool:
        df = pd.DataFrame(pool)[["username", "ig_user_id", "full_name", "is_private",
                                 "reported_follower_count", "profile_checked_at"]]
        df["is_private"] = df["is_private"].map({1: "yes", 0: "no"})
        st.dataframe(df, hide_index=True, width="stretch", column_config={
            "ig_user_id": "Instagram ID", "reported_follower_count": "Reported followers (latest check)",
            "profile_checked_at": "Profile checked (UTC)"})
        if not is_demo:
            with st.expander("Remove influencer"):
                victim = st.selectbox("Influencer", pool_names, key="remove_inf")
                st.caption("Also deletes their collected followers in every snapshot.")
                if st.checkbox("Confirm removal") and st.button("Remove", disabled=jobs.is_running()):
                    storage.delete_influencer(conn, next(i["id"] for i in pool if i["username"] == victim))
                    st.rerun()
    else:
        st.write("No influencers yet.")


# ---------------------------------------------------------------- collection

def render_progress() -> None:
    state = jobs.state()
    if state.running:
        st.markdown(f"**{state.kind.capitalize()} running** – profile {min(state.done + 1, state.total)} "
                    f"of {state.total}: @{state.current or '…'}")
        cap = st.session_state.get("job_limit") or state.current_reported
        if state.current_collected is not None:
            frac = min(state.current_collected / cap, 1.0) if cap else 0.0
            st.progress(frac, text=f"{state.current_collected:,} unique followers collected"
                                   + (f" / target {cap:,}" if cap else ""))
        if st.button("⏹ Cancel collection", type="secondary"):
            jobs.cancel()
    elif state.total:
        if state.fatal:
            st.error(state.fatal)
        if state.results:
            st.write("Last job results:", {f"@{k}": STATUS_LABELS.get(v, v) for k, v in state.results.items()})
    if state.log:
        with st.expander("Activity log", expanded=state.running):
            st.code("\n".join(state.log), language=None)
    # When the job finishes, rerun the whole app so the other tabs pick up the new data.
    if st.session_state.get("job_was_running") and not state.running:
        st.session_state["job_was_running"] = False
        st.rerun(scope="app")
    st.session_state["job_was_running"] = state.running


with tab_collect:
    if is_demo:
        st.info("Collection is disabled for the demo snapshot. Create a live snapshot in the sidebar. "
                "Demo data is never used as a fallback for failed live collection.")
    else:
        if not config.SESSION_FILE.exists():
            st.warning("No saved Instagram session. In a terminal run `python login.py` "
                       "(see README). The password is never entered in this web UI.")
        d = config.DEFAULT_COLLECTION
        c1, c2, c3 = st.columns(3)
        max_followers = c1.number_input("Max followers per influencer (0 = no limit)", 0, 1_000_000, d.max_followers, 500)
        page_size = c1.number_input("Page size (followers per request, ≤200)", 10, 200, d.page_size, 10)
        delay_min = c2.number_input("Delay between pages – min (s)", 0.0, 600.0, d.delay_min, 1.0)
        delay_max = c2.number_input("Delay between pages – max (s)", 0.0, 600.0, d.delay_max, 1.0)
        max_retries = c3.number_input("Rate-limit retries (bounded)", 0, 5, d.max_retries)
        backoff = c3.number_input("First backoff (s, doubles per retry)", 1.0, 3600.0, d.backoff_seconds, 30.0)
        settings = CollectionSettings(int(page_size), int(max_followers), float(delay_min), float(delay_max),
                                      int(max_retries), float(backoff)).validated()
        if settings.max_followers == 0:
            st.caption("⚠️ No limit: large accounts take hours and are more likely to hit rate limits.")

        targets = st.multiselect("Influencers to collect (processed one after another)", pool_names,
                                 default=[n for n in selected if n in pool_names])
        target_rows = [i for i in pool if i["username"] in targets]
        busy = jobs.is_running()
        b1, b2, b3 = st.columns(3)
        start = b1.button("▶ Start / resume", type="primary", disabled=busy or not targets,
                          help="Continues from the saved cursor when a previous run was interrupted.")
        restart = b2.button("↻ Restart from first page", disabled=busy or not targets,
                            help="Ignores the saved cursor. Already collected IDs are kept (duplicates ignored).")
        check = b3.button("🔎 Check profiles only", disabled=busy or not targets,
                          help="Fetch Instagram ID and reported follower count without collecting followers.")
        if start or restart or check:
            st.session_state["job_limit"] = settings.max_followers or None
            jobs.start(snapshot_id, target_rows, settings, restart=restart, resolve_only=check)
            st.session_state["job_was_running"] = True
            st.rerun()

    @st.fragment(run_every=2 if jobs.is_running() else None)
    def progress_fragment():
        render_progress()

    progress_fragment()


# -------------------------------------------------------------- data quality

def quality_table(names: list[str]) -> pd.DataFrame:
    rows = []
    for n in names:
        r = runs_by_name.get(n)
        if r is None:
            rows.append({"influencer": n, "status": "not collected in this snapshot", "data": "missing"})
            continue
        rows.append({
            "influencer": n,
            "status": ("⚠️ cut short by Instagram"
                       if storage.is_truncated(r["collected_count"], r["reported_follower_count"],
                                               bool(r["pagination_ended"]))
                       else STATUS_LABELS.get(r["status"], r["status"])),
            "data": "measured" if storage.run_has_data(r) else "missing",
            "reported_followers": r["reported_follower_count"],
            "collected_unique": r["collected_count"],
            "coverage": analysis.coverage(r["collected_count"], r["reported_follower_count"]),
            "pagination_ended": bool(r["pagination_ended"]),
            "limit_reached": bool(r["limit_reached"]),
            "error": bool(r["error_occurred"]),
            "configured_limit": r["max_followers"],
            "pages": r["pages_fetched"],
            "resumable": bool(r["next_cursor"]),
            "started_utc": r["started_at"],
            "finished_utc": r["finished_at"],
            "stop_reason": r["stop_reason"],
        })
    return pd.DataFrame(rows)


def summary_table(names: list[str]) -> pd.DataFrame:
    q = quality_table(names)
    summary = q[["influencer", "status", "data"] + [c for c in ("reported_followers", "collected_unique", "coverage")
                                                    if c in q.columns]]
    edf = analysis.exclusive_audience(sets, names)
    summary = summary.merge(edf[["influencer", "exclusive", "pct_exclusive_of_own", "pct_of_group_distinct"]],
                            on="influencer", how="left")
    summary.insert(0, "snapshot", snapshot["name"])
    summary["label"] = data_label()
    summary["group_distinct_collected"] = analysis.group_summary(sets, names)["total_distinct"]
    return summary


def data_label() -> str:
    return ("DEMO DATA – " if is_demo else "") + OBSERVED_LABEL


def build_report(names: list[str], sections: list[str]) -> export.Report:
    """Everything the export contains; runs when the download button is clicked."""
    tables = {"summary": lambda: summary_table(names).drop(columns=["snapshot", "label"]),
              "pairwise": lambda: analysis.pairwise_overlap(sets, names),
              "quality": lambda: quality_table(names)}
    g = analysis.group_summary(sets, names)
    notes = ["All figures are computed only from follower IDs actually collected in this snapshot. "
             "They are not extrapolated to full audiences.", REACH_NOTE]
    if g["influencers_missing_data"]:
        notes.append("No data (blank, not zero) for: " + ", ".join(f"@{m}" for m in g["influencers_missing_data"]))
    return export.Report(
        title=f"{'DEMO – ' if is_demo else ''}Influencer overlap – {snapshot['name']}",
        label=data_label(),
        notes=notes,
        totals={"snapshot": snapshot["name"], "influencers_selected": len(names),
                "influencers_with_data": len(g["influencers_with_data"]),
                "distinct_collected_followers": g["total_distinct"], "sum_of_collected_lists": g["sum_collected"],
                "duplicate_appearances": g["duplicate_appearances"],
                "distinct_followers_in_2plus_lists": g["followers_in_2plus"],
                "pct_distinct_in_2plus": g["pct_distinct_in_2plus"]},
        tables={key: tables[key]() for key in export.SECTIONS if key in sections},
    )


with head_export.popover("⬇ Export", width="stretch"):
    fmt_label = st.segmented_control("Format", list(export.FORMATS), default="PDF", required=True,
                                   key="export_format")
    sections = st.pills("Content", list(export.SECTIONS), format_func=export.SECTIONS.get,
                        selection_mode="multi", default=list(export.SECTIONS), key="export_sections")
    st.caption(f"{len(selected)} selected influencers ({len(with_data)} with data)."
               + (" CSV with several parts downloads as a ZIP." if fmt_label == "CSV" and len(sections) > 1 else ""))
    if fmt_label and sections and selected:
        fmt = export.FORMATS[fmt_label]
        fname, mime = export.file_name(f"overlap_snapshot{snapshot_id}", fmt, sections)
        st.download_button(f"⬇ Download {fmt_label}", lambda: export.render(build_report(selected, sections), fmt),
                           fname, mime, on_click="ignore", type="primary", width="stretch")
    else:
        st.caption("Choose a format, at least one part and at least one influencer.")


with tab_quality:
    st.caption("Coverage = collected unique followers / follower count reported by Instagram at collection time. "
               "'Pagination ended' only means Instagram returned no further page – it does **not** prove the "
               "list is complete (Instagram can hide or cap follower lists).")
    qdf = quality_table(selected or list(runs_by_name))
    if qdf.empty:
        st.write("Nothing selected.")
    else:
        st.dataframe(qdf, hide_index=True, width="stretch", column_config={"coverage": PCT})
        st.download_button("⬇ Data quality CSV", csv_bytes(qdf), f"data_quality_snapshot{snapshot_id}.csv", "text/csv")


# ------------------------------------------------------------------- overlap

def no_data_help(needed: int) -> None:
    """Explain why there is nothing to analyse yet and what to do next."""
    st.info(
        f"**Not enough collected data yet** – {len(with_data)} of {len(selected)} selected influencers have "
        f"followers collected in this snapshot; at least {needed} needed.\n\n"
        + ("This is a demo snapshot – reload demo data from the sidebar." if is_demo else
           ("1. In a terminal run `python login.py` (no saved Instagram session found).\n"
            if not config.SESSION_FILE.exists() else "")
           + "2. Open the **Collection** tab, choose the influencers and click **▶ Start / resume**.\n"
             "3. Check the **Data quality** tab, then come back here.")
    )


def data_banner() -> None:
    st.markdown(f"#### {OBSERVED_LABEL}")
    st.caption("All figures are computed only from follower IDs actually collected in this snapshot. "
               "They are not extrapolated to full audiences. " + REACH_NOTE)
    if missing:
        st.warning("No data (shown as blank / 'missing', not zero) for: " + ", ".join(f"@{m}" for m in missing))


def heatmap(names: list[str], metric: str) -> go.Figure:
    matrix = analysis.overlap_matrix(sets, names, metric)
    shared = analysis.overlap_matrix(sets, names, "shared")
    z = matrix.copy()
    text, hover = [], []
    for a in names:
        trow, hrow = [], []
        for b in names:
            if a not in sets or b not in sets:
                trow.append("no data")
                hrow.append(f"{a} × {b}: missing data")
                continue
            s = int(shared.loc[a, b])
            if a == b:
                z.loc[a, b] = float("nan")  # keep the scale for off-diagonal cells
                trow.append(f"{len(sets[a]):,} collected")
                hrow.append(f"{a}: {len(sets[a]):,} collected followers")
                continue
            v = matrix.loc[a, b]
            trow.append(f"{s:,}" if metric == "shared" else ("n/a" if pd.isna(v) else f"{v:.1%}"))
            hrow.append(
                f"<b>{a}</b> × <b>{b}</b><br>shared: {s:,}<br>"
                f"% of {a}'s collected ({len(sets[a]):,}): {analysis.safe_div(s, len(sets[a])) or 0:.1%}<br>"
                f"% of {b}'s collected ({len(sets[b]):,}): {analysis.safe_div(s, len(sets[b])) or 0:.1%}<br>"
                f"Jaccard: {analysis.safe_div(s, len(sets[a] | sets[b])) or 0:.1%}")
        text.append(trow)
        hover.append(hrow)
    fig = go.Figure(go.Heatmap(
        z=z.values, x=names, y=names, text=text, texttemplate="%{text}", hovertext=hover,
        hovertemplate="%{hovertext}<extra></extra>", colorscale=HEAT_SCALE, xgap=2, ygap=2,
        colorbar=dict(title=analysis.MATRIX_METRICS[metric], tickformat=None if metric == "shared" else ".0%"),
    ))
    fig.update_layout(height=120 + 60 * len(names), margin=dict(l=10, r=10, t=30, b=10),
                      yaxis=dict(autorange="reversed", title="row influencer (A)", showgrid=False, zeroline=False),
                      xaxis=dict(title="column influencer (B)", side="top", showgrid=False, zeroline=False),
                      # uncoloured cells (diagonal, missing data) show this neutral gray, distinct from a measured 0
                      plot_bgcolor="rgba(128,128,128,0.18)")
    return fig


with tab_overlap:
    data_banner()
    if len(selected) < 2:
        st.info("Select at least two influencers above.")
    elif len(with_data) < 2:
        no_data_help(2)
    else:
        metric_label = st.radio("Heatmap shows", list(analysis.MATRIX_METRICS.values()), horizontal=True)
        metric = next(k for k, v in analysis.MATRIX_METRICS.items() if v == metric_label)
        if metric == "pct_of_row":
            st.caption("Directional: cell (row A, column B) = shared(A, B) / collected(A) – "
                       "the share of A's collected followers who also follow B. Not symmetric.")
        elif metric == "jaccard":
            st.caption("Jaccard = shared / (collected A ∪ collected B). Symmetric.")
        else:
            st.caption("Shared follower IDs. Diagonal shows each influencer's collected count (not coloured).")
        st.plotly_chart(heatmap(selected, metric), width="stretch")

        pdf = analysis.pairwise_overlap(sets, selected)
        st.markdown("**Pairwise table** – denominators: `pct_shared_of_a` ÷ collected_a, "
                    "`pct_shared_of_b` ÷ collected_b, `jaccard` ÷ union")
        st.dataframe(pdf, hide_index=True, width="stretch", column_config={
            "pct_shared_of_a": PCT, "pct_shared_of_b": PCT, "jaccard": PCT})
        st.download_button("⬇ Pairwise CSV", csv_bytes(pdf), f"pairwise_overlap_snapshot{snapshot_id}.csv", "text/csv")


# ------------------------------------------------------------ unique audience

with tab_unique:
    data_banner()
    if not selected:
        st.info("Select influencers above.")
    elif not with_data:
        no_data_help(1)
    else:
        g = analysis.group_summary(sets, selected)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Distinct collected followers", f"{g['total_distinct']:,}",
                  help="Size of the union of all selected influencers' collected follower IDs.")
        m2.metric("Sum of collected lists", f"{g['sum_collected']:,}", help="Counts a person once per influencer.")
        m3.metric("Duplicate appearances", f"{g['duplicate_appearances']:,}", help="Sum − distinct.")
        pct = g["pct_distinct_in_2plus"]
        m4.metric("Distinct followers in ≥2 lists", f"{g['followers_in_2plus']:,}",
                  delta=None if pct is None else f"{pct:.1%} of distinct", delta_color="off")

        dist = pd.DataFrame([{"follows_k_selected_influencers": k, "distinct_followers": v,
                              "share_of_distinct": analysis.safe_div(v, g["total_distinct"])}
                             for k, v in g["membership_distribution"].items()])
        st.markdown("**Multi-way overlap** – how many of the selected influencers each distinct follower follows")
        st.dataframe(dist, hide_index=True, column_config={"share_of_distinct": PCT})

        st.markdown("**Exclusive audience** – followers of A minus the union of all *other selected* influencers")
        edf = analysis.exclusive_audience(sets, selected)
        st.dataframe(edf, hide_index=True, width="stretch",
                     column_config={"pct_exclusive_of_own": st.column_config.NumberColumn(
                         "exclusive ÷ own collected", format="percent"),
                         "pct_of_group_distinct": st.column_config.NumberColumn(
                             "exclusive ÷ group distinct", format="percent")})

        st.markdown("**Incremental unique audience** – what a candidate adds to an existing group")
        base = st.multiselect("Existing group", selected, default=selected[:1], key="base_group")
        cands = [n for n in selected if n not in base]
        idf = analysis.incremental_unique(sets, base, cands)
        if not base:
            st.info("Choose at least one influencer for the existing group.")
        elif idf.empty:
            st.info("All selected influencers are in the group – nothing to compare.")
        else:
            st.dataframe(idf, hide_index=True, width="stretch", column_config={
                "pct_new_of_candidate": st.column_config.NumberColumn("new ÷ candidate collected", format="percent"),
                "group_growth_pct": st.column_config.NumberColumn("new ÷ group distinct before", format="percent")})
            st.download_button("⬇ Incremental CSV", csv_bytes(idf),
                               f"incremental_snapshot{snapshot_id}.csv", "text/csv")

        st.download_button("⬇ Summary CSV (per influencer + group totals)", csv_bytes(summary_table(selected)),
                           f"summary_snapshot{snapshot_id}.csv", "text/csv")


# --------------------------------------------------------------- limitations

with tab_about:
    st.markdown(f"""
**How to read these numbers**

- Everything is *{OBSERVED_LABEL.lower()}*. With partial lists (coverage < 100 %), true overlap can be
  higher or lower; the tool deliberately does **not** extrapolate. Instagram's follower list order is not a
  random sample, so a partial list is not statistically representative.
- **Missing data ≠ zero.** An influencer with no collected data shows as blank / "missing", never as 0 % overlap.
  A 0 shown for a measured pair means no shared IDs were found among the collected followers.
- Group totals use real set unions; pairwise intersections are never summed.
- {REACH_NOTE}

**Data source limitations**

- Collection uses instagrapi, an *unofficial* client for Instagram's private mobile API. It can break when
  Instagram changes, and automated access may violate Instagram's Terms of Use and lead to checkpoints,
  temporary blocks or account restrictions. Use a dedicated account, small limits and long delays.
- Instagram may return fewer followers than the reported count (hidden/deactivated/restricted accounts,
  server-side caps). Private profiles only expose followers to accounts that follow them.
- Snapshots keep collection rounds apart; data from different dates is never silently mixed.
""")
