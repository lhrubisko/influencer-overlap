# Influencer Followers Overlap

[![tests](https://github.com/lhrubisko/influencer-overlap/actions/workflows/tests.yml/badge.svg)](https://github.com/lhrubisko/influencer-overlap/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B)](https://streamlit.io/)

**Measure how much the follower audiences of Instagram influencers overlap — and how much unique
audience each one really adds.**

A local, self-hosted tool for marketers and brands working with several creators. Collect follower
lists, then see pairwise overlap, a group's distinct audience, each influencer's exclusive followers,
and how many *new* people a candidate would add to your current lineup.

- 🔒 **Runs 100 % locally**: SQLite + Streamlit, no cloud services, no paid APIs
- 📊 **Overlap heatmap**: shared counts, directional percentages, Jaccard
- 🧮 **Group analysis**: distinct audience, exclusive followers, incremental reach of a candidate
- 🩺 **Honest data quality**: coverage, stop reasons and "missing ≠ zero" are shown explicitly,
  and nothing is extrapolated
- ⏯ **Resumable collection**: limits, delays, bounded backoff, cancel and resume from a cursor
- 🧪 **Demo mode**: try every feature with synthetic data, no Instagram account needed

> ⚠️ **Disclaimer.** Collection uses [instagrapi](https://github.com/subzeroid/instagrapi), an
> **unofficial** client for Instagram's private API. Automated access may violate Instagram's Terms
> of Use and can lead to checkpoints, temporary blocks or account restrictions. Follower data is
> personal data (GDPR and similar laws). You are responsible for how you use this tool. Read
> [Limitations](#limitations) before collecting real data.

---

## Contents

- [How it works](#how-it-works)
- [Installation](#installation)
- [Quick start (demo mode)](#quick-start-demo-mode)
- [Logging in to Instagram](#logging-in-to-instagram)
- [Collecting followers](#collecting-followers)
- [Reading the results](#reading-the-results)
- [Configuration](#configuration)
- [Safe collection guidelines](#safe-collection-guidelines)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## How it works

```
 Instagram ──(instagrapi, your account)──► collector ──► SQLite (snapshots, runs, follower IDs)
                                                              │
                                Streamlit UI ◄── analysis ◄───┘
```

1. You add influencer usernames and create a **snapshot** (one collection round, e.g. "2026-11").
2. The collector downloads each influencer's followers page by page and stores only the
   **follower's numeric Instagram ID** (the comparison key) and username.
3. The analysis compares the ID sets in the selected snapshot. Data from different snapshots is
   never mixed.

## Installation

**Requirements:** Python 3.12+ (tested with 3.14), Linux, macOS or Windows (WSL), and Git.

```bash
git clone https://github.com/lhrubisko/influencer-overlap.git
cd influencer-overlap

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt    # add -dev for tests: requirements-dev.txt
cp .env.example .env               # optional: defaults work out of the box
```

Start the app:

```bash
streamlit run app.py
```

Open <http://localhost:8501>. The server binds to **localhost only**
(`.streamlit/config.toml`), so it isn't reachable from your network.

<details>
<summary><b>PyCharm setup</b></summary>

1. *Settings → Project → Python Interpreter* → select `.venv/bin/python`.
2. *Run → Edit Configurations → + → Python*. Switch *Script path* to **Module** and set:
   - Module: `streamlit`
   - Parameters: `run app.py`
   - Working directory: the project root
3. Run `login.py` from PyCharm's **Terminal** tab, because it needs interactive input.

Running `app.py` directly (right-click → Run) does **not** work. Streamlit apps must be started with
`streamlit run`.
</details>

## Quick start (demo mode)

No Instagram account needed.

1. `streamlit run app.py`
2. Sidebar → **Demo data → Load / reset demo data**

This creates a snapshot marked 🧪 **DEMO** with five synthetic influencers whose overlaps are known
exactly (documented in `follower_overlap/demo.py`). It includes a profile with *measured zero*
overlap and one with *missing data*, so you can see how each case is displayed. Demo data stays in
its own snapshot and is never used as a fallback for failed live collection.

## Logging in to Instagram

The tool reads followers through an Instagram account that **you** provide. Instagram doesn't show
follower lists without a login.

**Use a dedicated secondary account, never your main or business account.** Don't link it to your
personal account (different e-mail and phone, not in the same Accounts Center), and use it in a
separate browser profile.

Passwords and session cookies are entered **only in the terminal**, never in the web UI. They are
never logged or written to the repository. The session is saved to `.secrets/ig_session.json` with
`600` permissions, and `.secrets/` is git-ignored.

### Option A: browser session (recommended)

Use this when password login fails. At the time of writing, instagrapi's password login often fails
with `ClientNeedsUpgradeError` (see [Troubleshooting](#troubleshooting)).

1. Log in to <https://www.instagram.com> in your browser with the secondary account.
2. Open DevTools (**F12**) → **Application** (Firefox: **Storage**) → **Cookies** →
   `https://www.instagram.com`.
3. Copy the **value** of the `sessionid` cookie. It starts with your numeric user ID, e.g.
   `1234567%3A…`.
4. Run:

   ```bash
   python login.py --sessionid
   ```

   and paste the value at the hidden prompt (terminal: **Ctrl+Shift+V**). Nothing is shown while
   you paste, so just press **Enter**.

> Treat `sessionid` like a password: anyone who has it is logged in as you. Never share it.

### Option B: username and password

```bash
python login.py
```

- You're asked for the username (or set `IG_USERNAME` in `.env`) and the password (hidden prompt).
- **2FA:** if Instagram asks for it, enter the code from your authenticator app or SMS. The script
  makes exactly one follow-up attempt.
- **Checkpoint / "confirm it's you":** the script stops and explains what to do. Confirm the login
  in the official Instagram app, **wait**, then run the script again. The tool never retries or
  tries to bypass verification.

### Session commands

```bash
python login.py --check      # validate the saved session
python login.py --force      # discard it and log in again with a password
python login.py --sessionid  # replace it with a browser session
python login.py --logout     # delete the saved session
```

The web UI only **loads** the saved session. If it expires, collection stops with a clear message
and you log in again from the terminal.

## Collecting followers

1. **Sidebar → New live snapshot**: name it, e.g. `2026-11 campaign`.
2. **Influencers** tab: add usernames, one per line. `name`, `@name` and
   `https://instagram.com/name/` all work. You can also upload a CSV: the `username` / `instagram` /
   `url` column is used if the file has a header, otherwise the first column (`,` `;` or tab delimited).
3. **Collection** tab:
   - set *Max followers per influencer*, *page size* and *delays* (see
     [safe guidelines](#safe-collection-guidelines))
   - pick the influencers (they are processed one at a time)
   - **🔎 Check profiles only** fetches the Instagram ID and reported follower count without
     collecting followers
   - **▶ Start / resume** starts the collection, or continues where it stopped
   - **↻ Restart from first page** ignores the saved cursor; IDs already collected are kept
   - **⏹ Cancel** stops after the current request
4. **Data quality** tab: check status, coverage and stop reasons before you trust the numbers.

Collection runs in a background thread. You can close the browser tab, but **keep the terminal
running** and don't let the computer sleep. On Linux:

```bash
systemd-inhibit --what=sleep:idle --why="Instagram collection" streamlit run app.py
```

### What happens behind the scenes

| Behaviour | Details |
|---|---|
| Minimal data | Stores only follower ID (as a string) and username. Never fetches per-follower profiles |
| Incremental | Each page is committed together with its cursor; duplicates are ignored |
| Resume | Uses Instagram's real pagination cursor (`next_max_id`, from `user_followers_v1_chunk`). Instagram may expire old cursors; then use *Restart from first page* |
| Rate limits | Bounded exponential backoff (default 2 retries: 120 s, 240 s), then the job **stops**. `feedback_required` stops immediately. Nothing retries forever |
| Checkpoints | Stop the job immediately. Interactive challenge prompts are disabled so the UI never freezes |
| Crashes | Partial data is kept. Runs left "running" are marked *interrupted* on the next start and can be resumed |
| Stall guard | 3 consecutive pages with no new IDs stop the run instead of looping |

### Run statuses

| Status | Meaning |
|---|---|
| ✅ pagination ended | Instagram returned no further page. This does **not** prove the list is complete |
| ⚠️ cut short by Instagram | Pagination ended below 90 % of the reported follower count |
| 🟨 limit reached | Your configured *max followers* was reached |
| 🛑 rate limited | Instagram asked to slow down and the retries were used up. Wait hours, then resume |
| 🔐 login/checkpoint needed | Session expired or verification required. Run `login.py` again |
| ⏹ cancelled / ⚠️ interrupted / ❌ error | Partial data kept; see the stop reason |

## Reading the results

> All figures are **"observed overlap in collected data"**. They describe only the follower IDs that
> were actually collected.

| Metric | Definition |
|---|---|
| Collected | Unique follower IDs collected for an influencer in the snapshot |
| Coverage | collected ÷ follower count reported by Instagram at collection time |
| Shared (A, B) | \|A ∩ B\| |
| % shared of A | shared ÷ collected(A): "what share of A's followers also follow B" |
| % shared of B | shared ÷ collected(B) |
| Jaccard | shared ÷ \|A ∪ B\| (symmetric) |
| Distinct collected | Size of the union of all selected influencers' follower sets |
| Exclusive (A) | A minus the union of all *other selected* influencers |
| Incremental (candidate) | candidate minus the union of the chosen *existing group* |
| Multi-way distribution | How many distinct followers follow exactly 1, 2, 3 … of the selected influencers |

**Heatmap modes:**
- **Counts**: shared followers. The diagonal shows each influencer's collected count.
- **Directional %**: cell (row A, column B) = shared ÷ collected(A). Not symmetric.
- **Jaccard**: symmetric similarity.

**Rules the tool follows:**
- **Missing ≠ zero.** An influencer without data is shown as *no data* (gray cell, blank values, a
  warning). A light-blue **0** is a measured zero.
- Group figures use real set unions. Pairwise intersections are never summed.
- Partial lists are **not** extrapolated to full audiences.

**CSV exports:** data quality, pairwise overlap, incremental audience, and a per-influencer summary
with group totals. Each is labelled with the snapshot name and data label.

**⬇ Export** (next to the snapshot title): a report for the selected influencers as **PDF**, **CSV** or
**XML**. Choose which parts it contains (per-influencer summary, pairwise overlap, data quality).
CSV with several parts downloads as a ZIP with one CSV per part. Missing data stays blank (XML: `missing="true"`),
never 0. The PDF uses the DejaVu Sans font when it is installed, otherwise diacritics are stripped.

## Configuration

All settings are optional and go in `.env` (copy from `.env.example`). Collection values can also be
changed per run in the UI.

| Variable | Default | Description |
|---|---|---|
| `IG_USERNAME` | – | Username for `login.py` (otherwise prompted) |
| `IG_PASSWORD` | – | Optional. **Prefer leaving it empty** and typing it at the hidden prompt |
| `IG_SESSION_FILE` | `.secrets/ig_session.json` | Where the session (auth cookies) is stored |
| `IG_PROXY` | – | Optional proxy for Instagram traffic, e.g. `http://user:pass@host:port` or `socks5://host:port` |
| `OVERLAP_DB_PATH` | `data/overlap.sqlite3` | SQLite database |
| `COLLECT_PAGE_SIZE` | `100` | Followers requested per API call (max 200) |
| `COLLECT_MAX_FOLLOWERS` | `2000` | Per-influencer cap; `0` = no limit |
| `COLLECT_DELAY_MIN` / `COLLECT_DELAY_MAX` | `8` / `15` | Random pause between pages (seconds) |
| `COLLECT_MAX_RETRIES` | `2` | Retries after rate-limit or network errors (max 5) |
| `COLLECT_BACKOFF_SECONDS` | `300` | First backoff; doubles with each retry |

If you use `IG_PROXY`, create the session through the same proxy. Never use free public proxies:
your session cookies would pass through someone else's server.

## Safe collection guidelines

Instagram doesn't publish its limits. These are conservative starting points, not guarantees:

| Setting | Recommended |
|---|---|
| Page size | `100` |
| Delay min / max | `8` / `15` s |
| Rate-limit retries | `2` |
| First backoff | `300` s |
| Daily volume (new account) | ~10–20k followers, increased gradually if no warnings appear |

Observed throughput with these settings is roughly **10–11k followers per hour**, so 10k followers
take about an hour. It varies with Instagram's response times.

- Several smaller batches are safer than one huge run.
- After any 🛑 or 🔐, pause for at least a day.
- Verified and business profiles often return only the first page of followers (see
  [Limitations](#limitations)).

## Limitations

- **Unofficial API.** instagrapi isn't supported by Meta and can break whenever Instagram changes.
  Login in particular breaks from time to time.
- **Account risk.** Automated collection may violate Instagram's Terms of Use. Expect checkpoints or
  blocks with large volumes. Use a dedicated account.
- **Incomplete lists.** Instagram often returns fewer followers than it reports (deactivated,
  restricted or hidden accounts, server-side caps). **Verified and business profiles** frequently
  return only a single page with no cursor. The tool flags these as *cut short by Instagram*.
- **Partial lists are not random samples.** Followers come in Instagram's own order (usually newest
  first). Overlap within the first N followers can differ a lot from overlap across the full
  audience, so the tool doesn't extrapolate.
- **Private profiles** only expose followers to accounts that follow them.
- **Follower overlap ≠ campaign reach.** Followers can be inactive, bots, or never see a post. Reach
  depends on the algorithm, timing and content.
- **Personal data.** Follower IDs and usernames are personal data. Keep the database local, collect
  only what you need, and delete old snapshots you no longer need.

## Troubleshooting

| Symptom | Solution |
|---|---|
| `ClientNeedsUpgradeError: Your version of Instagram is out of date` | A known instagrapi login issue ([#2807](https://github.com/subzeroid/instagrapi/issues/2807), [#2881](https://github.com/subzeroid/instagrapi/issues/2881)). Double-check the password first, because this error can hide a wrong one. Then use `python login.py --sessionid` |
| "No saved Instagram session" in the UI | Run `python login.py --sessionid` (or `python login.py`) |
| `login.py` seems frozen after the password or sessionid | Hidden prompts show nothing while you type. Logging in can take up to a minute |
| Checkpoint message | Confirm the login in the Instagram app, wait at least an hour, then try again. Don't loop logins |
| `TwoFactorRequired` after entering a code | The code expired or was wrong. Wait for a fresh one and run again |
| `BadPassword` with the correct password | Instagram sometimes reports this for suspicious logins. Log in once in the official app, then retry later, or use `--sessionid` |
| Only a handful of followers collected (*cut short*) | Common for verified and business profiles. Instagram doesn't return more |
| Status 🛑 *rate limited* / `feedback_required` | Stop for several hours, then **Start / resume** with longer delays and a lower limit |
| Resume fails with a cursor error | **↻ Restart from first page** (collected IDs are kept) |
| Status ⚠️ *interrupted* | The app stopped mid-run. **Start / resume** continues it |
| Empty heatmap / "Not enough collected data yet" | At least two selected influencers need collected data in the snapshot |
| Port 8501 in use | `streamlit run app.py --server.port 8502` |
| `ModuleNotFoundError` | Activate the venv: `source .venv/bin/activate` |
| Start completely fresh | Stop the app and delete `data/overlap.sqlite3` |

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

The test suite runs offline. It uses a fake Instagram client and a headless Streamlit `AppTest`, and
covers:
- the overlap maths (pairwise, directional, Jaccard, multi-way, exclusive, incremental)
- empty and missing data
- deduplication and snapshot isolation
- collector behaviour: pagination end, limits, truncation, bounded backoff, checkpoint stop,
  cancel, resume, stall guard
- the background job and a UI walkthrough of demo mode

### Project structure

```
app.py                        Streamlit UI
login.py                      terminal login (password + 2FA, or browser sessionid)
follower_overlap/
  config.py                   settings from environment / .env
  usernames.py                @name / URL normalisation, CSV import
  storage.py                  SQLite schema and queries (snapshots, runs, followers)
  analysis.py                 overlap maths (pure functions on sets of IDs)
  collector.py                instagrapi collection: limits, delays, backoff, resume
  jobs.py                     background worker so the UI never blocks
  demo.py                     synthetic demo data with known overlaps
  export.py                   PDF / CSV / XML report export
tests/                        pytest suite
.streamlit/config.toml        binds the server to localhost
```

### Data model

| Table | Purpose |
|---|---|
| `influencers` | Username, Instagram ID, latest reported follower count |
| `snapshots` | One collection round. Analyses never mix snapshots |
| `collection_runs` | One row per (snapshot, influencer): status, counts, cursor, timestamps, stop reason |
| `followers` | `(run_id, follower_id)` primary key, so IDs are deduplicated per run |

Contributions are welcome: open an issue first for larger changes, and make sure `pytest` passes.

## License

Released under the [MIT License](LICENSE).

This project isn't affiliated with, endorsed by, or connected to Instagram or Meta Platforms, Inc.
"Instagram" is a trademark of Meta Platforms, Inc.
