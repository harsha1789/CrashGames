import os
import re
import sys
import json
import glob
import queue
import threading
import subprocess
from flask import Flask, render_template, request, Response, jsonify, send_from_directory, abort
import time
from datetime import datetime

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "screenshots")
RECORDINGS_DIR = os.path.join(BASE_DIR, "recordings")
RUNS_DIR = os.path.join(BASE_DIR, "runs")
RUNS_INDEX = os.path.join(RUNS_DIR, "index.json")
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")
RESULTS_FILE = os.path.join(BASE_DIR, "test_results.json")

# Ensure dirs exist
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)


def _slug(s, maxlen=40):
    """Filesystem-safe slug for run-folder names."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (s or "").strip()).strip("-")
    return (s[:maxlen] or "game")

# Single-user global run state. A "run" is 1..N worker subprocesses (parallel DSC gives
# each selected account its own browser worker); their stdout lines are multiplexed into
# one queue that /stream drains, prefixed [W1]/[W2]/… when there's more than one worker.
WORKERS = []          # [{"proc": Popen, "label": "W1"}]
LOG_Q = None          # queue.Queue for the current run; None sentinel = stream end
CURRENT_RUN = {"game": None, "start_time": None, "status": "idle"}
# Single source of truth for the live log: every worker line is appended here and
# /stream serves each client from its own read position. That makes the stream
# broadcast-safe — any number of tabs get the FULL log (the old queue was
# consume-once: with two dashboard tabs open, each tab got random halves).
# Reset per run. RUN_DONE flips when the watcher has appended the final lines.
from collections import deque
LOG_HISTORY = deque(maxlen=20000)
RUN_DONE = {"v": True}


def _log_put(q, line):
    LOG_HISTORY.append(line)


def _workers_alive():
    return any(w["proc"].poll() is None for w in WORKERS)


def _stop_workers():
    for w in WORKERS:
        if w["proc"].poll() is None:
            w["proc"].terminate()
            w["proc"].wait()


def _pump_worker(proc, label, q, drop_payload):
    """Reader thread: one per worker. In batch mode the per-game REPORTPAYLOAD blocks
    are dropped — 300 inline JSON reports would swamp the log pane, and each game's
    results.json is already persisted in its run folder."""
    prefix = f"[{label}] " if label else ""
    in_payload = False
    for line in iter(proc.stdout.readline, ''):
        line = line.rstrip("\r\n")
        if drop_payload:
            s = line.strip()
            if s == "REPORTPAYLOAD===":
                in_payload = True
                continue
            if s == "===REPORTPAYLOAD":
                in_payload = False
                continue
            if in_payload:
                continue
        _log_put(q, prefix + line)
    proc.stdout.close()
    proc.wait()


def _watch_workers(workers, q, batch):
    """Waits for every worker, then closes the stream (and, for batches, appends a
    result summary read back from the shared report)."""
    for w in workers:
        w["proc"].wait()
    if batch and batch.get("report") and os.path.exists(batch["report"]):
        try:
            _log_put(q, "[BATCH] " + _report_summary(batch["report"]))
        except Exception as e:
            _log_put(q, f"[BATCH] summary unavailable: {e}")
        _log_put(q, f"[BATCH] Complete — report: {os.path.basename(batch['report'])}")
    # Auto-sweep: record each tested game into the prod DB so the weekly rotation holds
    # (unless the run was flagged do-not-record). Match picks to their result rows by name.
    if batch and batch.get("sweep"):
        _record_sweep(batch["sweep"], batch.get("report"), q)
    RUN_DONE["v"] = True
    q.put(None)


def _record_sweep(sweep, report_path, q):
    """Persist an auto-sweep's results to dsc_history.db (honours the do-not-record flag)."""
    try:
        from modules import dsc_history_db
        if not sweep.get("record"):
            _log_put(q, "[SWEEP] do-not-record: results NOT written to the prod database")
            return
        by_name = {}
        rec_path = (report_path or "").replace(".xlsx", "_records.jsonl")
        if rec_path and os.path.exists(rec_path):
            with open(rec_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        r = json.loads(line)
                        by_name[(r.get("game") or "").strip().lower()] = r
        n = 0
        for p in sweep.get("picks", []):
            out = by_name.get((p["name"] or "").strip().lower(), {})
            dsc_history_db.record_test(sweep["brand"], sweep["region"], p["provider"],
                                       p["id"], p["name"], out, run_id=sweep.get("run_id", ""),
                                       record=True)
            n += 1
        _log_put(q, f"[SWEEP] Recorded {n} result(s) to the prod database (dsc_history.db)")
    except Exception as e:
        _log_put(q, f"[SWEEP] DB record failed: {e}")


def _report_summary(report_path):
    """One-line Pass/Fail tally straight from the report sheet."""
    from openpyxl import load_workbook
    from modules import dsc_report as dr
    wb = load_workbook(report_path)
    ws, hdr, cols = dr._pick_sheet(wb)   # header may not be row 1 (title rows, dead tabs)

    def idx(canonical):
        c = dr._col(cols, canonical)
        return c - 1 if c else None

    name_i, launch_i = idx("Game Name"), idx("Launch")
    tally = {"Launch": [0, 0], "Bet Placed": [0, 0], "Tlogs": [0, 0]}
    pending = 0
    awaiting = 0   # Tlogs "Pending": spun, but transaction validation hasn't run yet
    for row in ws.iter_rows(min_row=hdr + 1, values_only=True):
        if name_i is None or name_i >= len(row) or not str(row[name_i] or "").strip():
            continue   # filler/blank rows aren't games
        if launch_i is None or launch_i >= len(row):
            continue
        if not str(row[launch_i] or "").strip():
            pending += 1
            continue
        for col, counts in tally.items():
            i = idx(col)
            v = str(row[i] or "").strip().lower() if i is not None and i < len(row) else ""
            if v == "pass":
                counts[0] += 1
            elif v == "fail":
                counts[1] += 1
            elif v == "pending" and col == "Tlogs":
                awaiting += 1
    wb.close()
    parts = [f"{col} {p} Pass / {f} Fail" for col, (p, f) in tally.items()]
    if awaiting:
        parts.append(f"{awaiting} awaiting Tlogs validation")
    if pending:
        parts.append(f"{pending} not run")
    return "Summary: " + " · ".join(parts)


def _start_workers(cmds, batch=None):
    """Spawn one subprocess per (label, cmd[, env]), wire reader threads into a fresh queue."""
    global WORKERS, LOG_Q
    _stop_workers()
    LOG_Q = queue.Queue()
    LOG_HISTORY.clear()
    RUN_DONE["v"] = False
    WORKERS = []
    drop_payload = batch is not None
    for item in cmds:
        label, cmd = item[0], item[1]
        env = item[2] if len(item) > 2 else None
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=BASE_DIR,
            env=env
        )
        WORKERS.append({"proc": proc, "label": label})
        threading.Thread(target=_pump_worker, args=(proc, label, LOG_Q, drop_payload),
                         daemon=True).start()
    threading.Thread(target=_watch_workers, args=(list(WORKERS), LOG_Q, batch),
                     daemon=True).start()


FLEET_STOP = threading.Event()   # set by /stop so a capped fleet stops launching queued waves


def _start_fleet(worker_specs, max_parallel, group_meta, cooldown=0):
    """Run a multi-region auto-sweep as a fleet of browser workers, at most `max_parallel`
    running at once (queued specs start as slots free — so 'all products' across 8 regions
    doesn't open 8 browsers at once). `worker_specs` = [{label, cmd, env}]; `group_meta` =
    one entry per (brand, region) with its shared report + picks, recorded to the prod DB when
    ALL workers finish (each honours its own do-not-record flag via _record_sweep).

    `cooldown` (seconds, default 0 — unused by the slot Auto Sweep, which spreads across
    DIFFERENT accounts and has no reason to wait): holds a finished worker's slot occupied for
    this long before the next pending spec may start in it. Added for the crash sweep
    (max_parallel=1, SAME account every launch) after the 2026-08-03 provider validation sweep
    showed frequent 'session held by another tab' / DISCONNECTED aborts on games launched
    immediately after a previous game's browser closed — our own launch-URL fetch is always
    fresh (no client-side token reuse), so this points at the casino backend's own wallet/game
    session lock for that account not having released yet, especially after a run that aborted
    without a clean in-game exit. A gap before requesting the next launch gives that lock time
    to expire instead of guaranteed-colliding with it."""
    global WORKERS, LOG_Q
    _stop_workers()
    FLEET_STOP.clear()
    LOG_Q = queue.Queue()
    LOG_HISTORY.clear()
    RUN_DONE["v"] = False
    WORKERS = []
    q = LOG_Q

    def scheduler():
        pending = list(worker_specs)
        running = []   # {"proc", "thread", "finished_at": float|None}
        while (pending or running) and not FLEET_STOP.is_set():
            occupied = sum(1 for r in running
                          if r["finished_at"] is None or time.time() - r["finished_at"] < cooldown)
            while pending and occupied < max_parallel and not FLEET_STOP.is_set():
                spec = pending.pop(0)
                try:
                    proc = subprocess.Popen(
                        spec["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1,
                        cwd=BASE_DIR, env=spec.get("env"))
                except Exception as e:
                    _log_put(q, f"[SWEEP] worker {spec['label']} failed to start: {e}")
                    continue
                w = {"proc": proc, "label": spec["label"]}
                WORKERS.append(w)
                th = threading.Thread(target=_pump_worker, args=(proc, spec["label"], q, True),
                                      daemon=True)
                th.start()
                running.append({"proc": proc, "thread": th, "finished_at": None,
                                "label": spec["label"]})
                occupied += 1
            time.sleep(0.5)
            for r in running:
                if r["finished_at"] is None and r["proc"].poll() is not None:
                    r["thread"].join(timeout=5)
                    r["finished_at"] = time.time()
                    if cooldown:
                        _log_put(q, f"[SWEEP] {r['label']} finished — "
                                    f"{cooldown:g}s cooldown before the next launch...")
            running = [r for r in running
                      if r["finished_at"] is None or time.time() - r["finished_at"] < cooldown]
        # Every worker finished (or the run was stopped): tally + record each region.
        for gm in group_meta:
            try:
                if gm.get("report") and os.path.exists(gm["report"]):
                    _log_put(q, f"[SWEEP] {gm['brand']} {gm['region']}: "
                                + _report_summary(gm["report"]))
            except Exception as e:
                _log_put(q, f"[SWEEP] {gm['brand']} {gm['region']} summary unavailable: {e}")
            _record_sweep(gm, gm.get("report"), q)
        RUN_DONE["v"] = True
        q.put(None)

    threading.Thread(target=scheduler, daemon=True).start()

# Cache of auth tokens so the typeahead doesn't re-authenticate on every keystroke.
# { username: (token, epoch_seconds) }; entries expire after TOKEN_TTL.
_TOKEN_CACHE = {}
TOKEN_TTL = 600  # 10 minutes


def _get_token(username, password, brand="betway", region="ZA"):
    """Return a cached token for the (account, brand, region), authenticating if needed."""
    key = (username, brand, region)
    hit = _TOKEN_CACHE.get(key)
    if hit and (time.time() - hit[1]) < TOKEN_TTL:
        return hit[0], None
    from modules.auth_handler import AuthHandler
    auth = AuthHandler().authenticate(username, password, brand=brand, region=region)
    if auth.get("success"):
        _TOKEN_CACHE[key] = (auth["token"], time.time())
        return auth["token"], None
    return None, auth.get("message", "auth failed")


def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        with open(ACCOUNTS_FILE, "r") as f:
            return json.load(f)
    return []


def save_accounts(accounts):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(accounts, f, indent=2)


# ─── Routes ──────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    return jsonify(load_accounts())


@app.route('/api/accounts', methods=['POST'])
def add_account():
    data = request.json
    accounts = load_accounts()
    # An account is unique per (brand, region, username) — same number can exist
    # across brands/regions as a separate login.
    brand = data.get("brand", "betway")
    region = data.get("region", "ZA")
    username = data.get("username", "")
    accounts = [a for a in accounts if not (
        a.get("username") == username
        and a.get("brand", "betway") == brand
        and a.get("region", "ZA") == region
    )]
    accounts.append({
        "label": data.get("label", ""),
        "username": username,
        "password": data.get("password", ""),
        "brand": brand,
        "region": region,
        "added": datetime.now().isoformat()
    })
    save_accounts(accounts)
    return jsonify({"status": "saved", "count": len(accounts)})


@app.route('/api/accounts/<username>', methods=['DELETE'])
def delete_account(username):
    brand = request.args.get("brand")
    region = request.args.get("region")
    accounts = load_accounts()

    def matches(a):
        if a.get("username") != username:
            return False
        if brand is not None and a.get("brand", "betway") != brand:
            return False
        if region is not None and a.get("region", "ZA") != region:
            return False
        return True

    accounts = [a for a in accounts if not matches(a)]
    save_accounts(accounts)
    return jsonify({"status": "deleted"})


@app.route('/api/results')
def get_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r") as f:
            return jsonify(json.load(f))
    return jsonify([])


@app.route('/api/games/search', methods=['POST'])
def games_search():
    """Live typeahead: authenticate with the selected account (token cached) and return matching
    games for the query. Body: {q, username, password, brand, region}."""
    data = request.json or {}
    q = (data.get('q') or '').strip()
    username = data.get('username', '')
    password = data.get('password', '')
    brand = (data.get('brand') or 'betway').lower()
    region = (data.get('region') or 'ZA').upper()
    if len(q) < 2:
        return jsonify({"games": []})
    if not username or not password:
        return jsonify({"games": [], "error": "select an account to search"})
    try:
        token, err = _get_token(username, password, brand=brand, region=region)
        if not token:
            return jsonify({"games": [], "error": f"auth failed: {err}"})
        from modules.game_handler import GameHandler
        games = GameHandler().search_games_list(q, token, region=region, brand=brand, limit=10)
        return jsonify({"games": games})
    except Exception as e:
        print(f"[games_search] EXCEPTION q={q!r}: {e}")
        return jsonify({"games": [], "error": str(e)})


@app.route('/api/recordings')
def get_recordings():
    files = sorted(glob.glob(os.path.join(RECORDINGS_DIR, "*.webm")), key=os.path.getmtime, reverse=True)
    result = []
    for f in files[:10]:
        name = os.path.basename(f)
        size_mb = round(os.path.getsize(f) / (1024 * 1024), 1)
        mtime = datetime.fromtimestamp(os.path.getmtime(f)).strftime("%b %d, %H:%M")
        result.append({"name": name, "size": f"{size_mb} MB", "date": mtime})
    return jsonify(result)


@app.route('/recordings/<path:filename>')
def serve_recording(filename):
    return send_from_directory(RECORDINGS_DIR, filename)


@app.route('/api/screenshot')
def latest_screenshot():
    pngs = glob.glob(os.path.join(SCREENSHOTS_DIR, "*.png"))
    if not pngs:
        return "", 204
    latest = max(pngs, key=os.path.getmtime)
    return send_from_directory(SCREENSHOTS_DIR, os.path.basename(latest))


@app.route('/screenshots/<path:filename>')
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOTS_DIR, filename)


@app.route('/runs/<run_id>/<path:filename>')
def serve_run_file(run_id, filename):
    """Serve any artifact (screenshots/, video/, logs/, results.json) from a run folder."""
    # Guard against path traversal in run_id.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        abort(404)
    return send_from_directory(os.path.join(RUNS_DIR, run_id), filename)


@app.route('/dsc-report/latest')
def dsc_report_latest():
    """Download the most recent DSC Excel report — batch (DSC_Report_*), auto-sweep (DSC_Sweep_*),
    or CLI sweep (DSC_Auto_*). Input sheets (*_input.xlsx) are not reports."""
    reports = [p for p in glob.glob(os.path.join(RUNS_DIR, "DSC_*.xlsx"))
               if not p.endswith("_input.xlsx")]
    if not reports:
        return "No DSC report yet — run a sanity check first.", 404
    latest = max(reports, key=os.path.getmtime)
    return send_from_directory(RUNS_DIR, os.path.basename(latest), as_attachment=True)


@app.route('/api/runs')
def list_runs():
    """Recent runs (newest first) — feeds the homepage history panel."""
    if os.path.exists(RUNS_INDEX):
        with open(RUNS_INDEX, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify([])


def _batch_report_path(run_id):
    """The Excel report belonging to a batch run: from its index entry when recorded,
    else derived from the run_id timestamp (runs/<YYYYMMDD_HHMMSS>_DSC ↔
    DSC_Report_<YYYY-MM-DD_HHMMSS>.xlsx)."""
    try:
        if os.path.exists(RUNS_INDEX):
            with open(RUNS_INDEX, "r", encoding="utf-8") as f:
                for e in json.load(f):
                    if e.get("run_id") == run_id and e.get("report"):
                        return os.path.join(RUNS_DIR, e["report"])
    except Exception:
        pass
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_(\d{6})_DSC$", run_id or "")
    if m:
        p = os.path.join(RUNS_DIR,
                         f"DSC_Report_{m.group(1)}-{m.group(2)}-{m.group(3)}_{m.group(4)}.xlsx")
        if os.path.exists(p):
            return p
    return None


def _report_rows(report_path):
    """The report sheet as JSON rows (blank/filler rows skipped) for the homepage
    validation table."""
    from openpyxl import load_workbook
    from modules import dsc_report as dr
    wb = load_workbook(report_path)
    ws, hdr, cols = dr._pick_sheet(wb)
    idxs = {c: dr._col(cols, c) for c in dr.DSC_COLUMNS}
    out = []
    for r in range(hdr + 1, ws.max_row + 1):
        name = ws.cell(row=r, column=idxs["Game Name"]).value if idxs["Game Name"] else None
        if not str(name or "").strip():
            continue
        out.append({c: (ws.cell(row=r, column=i).value if i else "") for c, i in idxs.items()})
    wb.close()
    return out


def _crash_sweep_rows(run_id):
    """Aggregate a crash sweep's per-game results.json files into one table. Unlike the slot
    batch/auto-sweep paths, a crash sweep has NO single shared report file — /launch-crash-sweep
    runs one crash_auto.py subprocess per game, each writing its OWN results.json into
    runs/<batch_id>/<NN_slug>/ (see app.py's launch_crash_sweep). So this reads those folders
    directly off disk instead of parsing an Excel report — works even mid-run (a game still in
    progress just has no results.json yet and is skipped this pass, not shown as failed)."""
    batch_dir = os.path.join(RUNS_DIR, run_id)
    rows = []
    for d in sorted(glob.glob(os.path.join(batch_dir, "*"))):
        folder = os.path.basename(d)
        rj = os.path.join(d, "results.json")
        if not os.path.isdir(d) or not os.path.exists(rj):
            continue
        name = re.sub(r"^\d+_", "", folder).replace("-", " ").strip() or folder
        try:
            with open(rj, encoding="utf-8") as f:
                data = json.load(f)
            summary = data.get("summary", {})
            rows.append({"folder": folder, "name": name,
                        "total": summary.get("total", 0), "passed": summary.get("passed", 0),
                        "failed": summary.get("failed", 0), "skipped": summary.get("skipped", 0)})
        except Exception as e:
            rows.append({"folder": folder, "name": name, "error": str(e)})
    return rows


CRASH_REPORT_COLUMNS = ["Sr. No.", "Provider", "Game Name", "Game Type",
                       "Launch", "Bet Placed", "Tlogs", "Remark", "Evidence"]


def _crash_report_rows(run_id):
    """Same per-game folders _crash_sweep_rows reads, mapped onto the DSC-style column set
    (see modules/dsc_report.py DSC_COLUMNS) plus Game Type — so crash results sit in the same
    shape as the slot sheet's, importable into the team's master tracker.

    Launch: Pass if 'Crash UI controls detected' (TEST 1) passed — i.e. the game genuinely
    loaded and rendered its UI. Deliberately NOT gated on TEST 2 (round-lifecycle observed)
    too: TEST 2 can fail on a perfectly-launched game just because the round we happened to
    watch ran long and our observation window timed out — that's a lifecycle-OBSERVATION gap,
    not evidence the game never launched, and conflating the two mis-marked real launches as
    "Did not launch" (confirmed 2026-07-28 against a real FlyX Party run).
    A run can also abort BEFORE TEST 1 ever runs (crash_auto.py's _abort_session — dead/expired
    session, or the phase drifted unreadable right after startup). That path writes a single
    result named "Crash game session is live" instead of the normal TEST 1-4 battery; handled
    explicitly below so its own specific reason surfaces instead of falling through to a vague
    generic message.
    Bet Placed / Tlogs: always "NA" here — /launch-crash-sweep is dry-run only by design (see
    its docstring), so no wager is ever placed to report a Bet Placed/Tlogs verdict for. This is
    NOT the same claim as "Skipped (non-slot)" — the game WAS tested, just never wagered on.
    """
    batch_dir = os.path.join(RUNS_DIR, run_id)
    rows = []
    for i, d in enumerate(sorted(glob.glob(os.path.join(batch_dir, "*"))), 1):
        folder = os.path.basename(d)
        rj = os.path.join(d, "results.json")
        if not os.path.isdir(d) or not os.path.exists(rj):
            continue
        pick = {}
        pj = os.path.join(d, "pick.json")
        if os.path.exists(pj):
            try:
                with open(pj, encoding="utf-8") as f:
                    pick = json.load(f)
            except Exception:
                pick = {}
        name = pick.get("name") or re.sub(r"^\d+_", "", folder).replace("-", " ").strip() or folder
        try:
            with open(rj, encoding="utf-8") as f:
                data = json.load(f)
            results = {r.get("name"): r for r in data.get("results", [])}
            abort = results.get("Crash game session is live")
            if abort is not None:
                launched = bool(abort.get("passed"))
                remark = abort.get("details") or "Session did not stay live long enough to test"
            else:
                t1 = results.get("Crash UI controls detected", {})
                launched = bool(t1.get("passed"))
                remark = "Dry-run only — no wagers placed" if launched else \
                    (t1.get("details") or "Controls not detected")
            rows.append({"Sr. No.": i, "Provider": pick.get("provider") or "",
                        "Game Name": name, "Game Type": "Crash Games",
                        "Launch": "Pass" if launched else "Fail",
                        "Bet Placed": "NA", "Tlogs": "NA", "Remark": remark,
                        "Evidence": f"{run_id}/{folder}"})
        except Exception as e:
            rows.append({"Sr. No.": i, "Provider": pick.get("provider") or "", "Game Name": name,
                        "Game Type": "Crash Games", "Launch": "Fail", "Bet Placed": "NA",
                        "Tlogs": "NA", "Remark": f"Unreadable results.json: {e}",
                        "Evidence": f"{run_id}/{folder}"})
    return rows


def _write_crash_report_xlsx(run_id, rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    path = os.path.join(RUNS_DIR, f"Crash_Report_{run_id}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "Crash"
    ws.append(CRASH_REPORT_COLUMNS)
    widths = {"Sr. No.": 8, "Provider": 22, "Game Name": 30, "Game Type": 14,
             "Launch": 10, "Bet Placed": 12, "Tlogs": 10, "Remark": 46, "Evidence": 34}
    for cell in ws[1]:
        cell.font = Font(bold=True)
        ws.column_dimensions[cell.column_letter].width = widths.get(cell.value, 14)
    for row in rows:
        ws.append([row.get(c, "") for c in CRASH_REPORT_COLUMNS])
    wb.save(path)
    return path


@app.route('/api/crash-report/<run_id>')
def api_crash_report(run_id):
    """Build (or rebuild) the Excel report for a crash sweep on demand, in the same column
    shape the slot DSC sheet uses (see _crash_report_rows). Regenerated fresh each call — cheap
    (a handful of small JSON files) and always reflects the latest results on disk, including a
    sweep that's still running. run_id='latest' is handled by api_crash_report_latest below —
    Flask matches the static rule first regardless of declaration order, so this dynamic route
    never actually sees the literal string 'latest'."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or "") or not run_id.endswith("_crash_sweep"):
        abort(404)
    rows = _crash_report_rows(run_id)
    if not rows:
        return jsonify({"error": "no saved results for this run yet"}), 404
    path = _write_crash_report_xlsx(run_id, rows)
    return send_from_directory(RUNS_DIR, os.path.basename(path), as_attachment=True)


@app.route('/api/crash-report/latest')
def api_crash_report_latest():
    """One-click download of the most recent crash sweep's report — the crash-vertical
    equivalent of /dsc-report/latest, for the persistent link in the Crash Sweep card (no need
    to open Recent Runs first, matching how slots expose 'Download DSC report')."""
    index = []
    if os.path.exists(RUNS_INDEX):
        with open(RUNS_INDEX, "r", encoding="utf-8") as fh:
            index = json.load(fh)
    latest = next((r for r in index if r.get("type") == "crash-sweep"), None)
    if not latest:
        return "No crash sweep run yet — run one first.", 404
    return api_crash_report(latest["run_id"])


@app.route('/api/report/<run_id>')
def api_report(run_id):
    """Persisted results for a past run, so the report survives page refreshes.
    Single runs: the run folder's results.json (same payload the live stream emits).
    Batch runs: the Excel report parsed into rows + a tally.
    Crash sweeps: no Excel report — aggregated on the fly from each game's own results.json."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        abort(404)
    # Batch AND auto-sweep runs are Excel-report-backed (many rows); render them as a table.
    if run_id.endswith("_DSC") or run_id.endswith("_DSCsweep"):
        rep = _batch_report_path(run_id)
        if not rep:
            return jsonify({"error": "no report found for this run"}), 404
        return jsonify({"batch": True, "run_id": run_id,
                        "report": os.path.basename(rep),
                        "summary": _report_summary(rep),
                        "rows": _report_rows(rep)})
    if run_id.endswith("_crash_sweep"):
        rows = _crash_sweep_rows(run_id)
        if not rows:
            return jsonify({"error": "no saved results for this run yet"}), 404
        return jsonify({"batch": True, "crash": True, "run_id": run_id, "rows": rows})
    rj = os.path.join(RUNS_DIR, run_id, "results.json")
    if os.path.exists(rj):
        with open(rj, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"error": "no saved results for this run"}), 404


@app.route('/dsc-report/file/<name>')
def dsc_report_file(name):
    """Download a SPECIFIC report (the /latest route only serves the newest). Covers batch
    (DSC_Report_*), auto-sweep (DSC_Sweep_*) and CLI-sweep (DSC_Auto_*) reports."""
    if not re.fullmatch(r"DSC_[A-Za-z0-9._-]+\.xlsx", name or "") or name.endswith("_input.xlsx"):
        abort(404)
    return send_from_directory(RUNS_DIR, name, as_attachment=True)


# ─── Tlogs validation: verify recorded spins against the site's transaction history ───
@app.route('/tlogs-reports')
def tlogs_reports():
    """Bet-records files (newest first) the validator can be pointed at."""
    out = []
    for p in sorted(glob.glob(os.path.join(RUNS_DIR, "*_records.jsonl")),
                    key=os.path.getmtime, reverse=True)[:20]:
        try:
            with open(p, encoding="utf-8") as f:
                n = sum(1 for line in f if line.strip())
        except OSError:
            n = 0
        out.append({"name": os.path.basename(p), "records": n,
                    "age_min": int((time.time() - os.path.getmtime(p)) / 60),
                    "validated": os.path.exists(p.replace("_records.jsonl", "_validation.json"))})
    return jsonify(out)


@app.route('/validate-tlogs', methods=['POST'])
def validate_tlogs():
    """Launch tlogs_validate.py as a worker; output streams through /stream like any run."""
    global CURRENT_RUN
    if _workers_alive():
        return jsonify({"status": "error", "message": "A run is already in progress"}), 409
    data = request.json or {}
    name = os.path.basename(data.get('records') or '')
    path = os.path.join(RUNS_DIR, name)
    if not name.endswith('_records.jsonl') or not os.path.exists(path):
        return jsonify({"status": "error", "message": "Records file not found"}), 400
    cmd = [sys.executable, "-u", "tlogs_validate.py", "--records", path]
    if data.get('headed'):
        cmd.append("--headed")
    CURRENT_RUN = {"game": f"Tlogs validation · {name}", "run_id": name,
                   "start_time": datetime.now().isoformat(), "status": "running"}
    _start_workers([("TLOGS", cmd)])
    return jsonify({"status": "started"})


@app.route('/tlogs-validation/latest')
def tlogs_validation_latest():
    """The most recent validation result (JSON written next to the records file)."""
    files = glob.glob(os.path.join(RUNS_DIR, "*_validation.json"))
    if not files:
        return jsonify({"error": "no validation yet"}), 404
    latest = max(files, key=os.path.getmtime)
    with open(latest, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route('/api/log-history')
def log_history():
    """The current/most recent run's streamed lines — lets the UI repopulate the
    log pane after a refresh instead of showing a blank terminal."""
    return jsonify({"lines": list(LOG_HISTORY),
                    "game": CURRENT_RUN.get("game"),
                    "status": CURRENT_RUN.get("status")})


@app.route('/api/status')
def status():
    global CURRENT_RUN
    if _workers_alive():
        CURRENT_RUN["status"] = "running"
    elif CURRENT_RUN["status"] == "running":
        CURRENT_RUN["status"] = "done"
    return jsonify(CURRENT_RUN)


@app.route('/launch', methods=['POST'])
def launch():
    global CURRENT_RUN
    data = request.json

    game_type = data.get('game_type', 'slot')
    game_name = data.get('game', '')
    username = data.get('username', '')
    password = data.get('password', '')
    is_mobile = data.get('mobile', False)
    default_bet = data.get('default_bet', '')
    min_bet = data.get('min_bet', '')
    brand = data.get('brand', 'betway')
    region = data.get('region', 'ZA')
    is_headless = data.get('headless', False)
    tests = data.get('tests')   # optional list of gated checks; None/absent = run all
    crash_url = data.get('url', '')

    # Each launch gets its own folder: runs/<timestamp>_<game>/ holding screenshots/, video/,
    # logs/, results.json — so runs never overwrite each other.
    started = datetime.now()

    if game_type == 'crash':
        # LIVE ONLY — the dry-run path was deliberately removed from this card (2026-07-29,
        # explicit request) so every run through it places a real wager, gated by
        # crash_auto.py --live + config_env.MAX_STAKE, PLUS the dashboard's own confirmation
        # checkbox. Stake is re-validated server-side here too — never trust a client-side
        # check alone for something that spends real money. Crash Sweep is UNAFFECTED — it
        # stays dry-run-only (see /launch-crash-sweep's own docstring on why).
        import config_env
        live_target = (data.get('target') or '').strip()
        try:
            live_bet = float(data.get('bet') or 0)
        except (TypeError, ValueError):
            live_bet = 0
        if live_bet <= 0:
            return jsonify({"status": "error", "message": "Enter a positive stake amount"}), 400
        if live_bet > config_env.MAX_STAKE:
            return jsonify({"status": "error",
                            "message": f"Stake {live_bet:g} exceeds the safety cap "
                                       f"{config_env.MAX_STAKE:g}"}), 400
        label = game_name or ("Crash URL" if crash_url else "Crash game")
        run_id = f"{started:%Y%m%d_%H%M%S}_crash_{_slug(label)}"
        run_dir = os.path.join(RUNS_DIR, run_id)
        os.makedirs(run_dir, exist_ok=True)
        cmd = [sys.executable, "-u", "crash_auto.py"]
        if crash_url:
            cmd.append(crash_url)
        elif game_name:
            if not username or not password:
                return jsonify({"status": "error",
                                "message": "Crash-by-name needs an account (username + password), "
                                           "or paste a direct launch URL"}), 400
            cmd += ["--game", game_name, "--username", username, "--password", password]
        else:
            return jsonify({"status": "error",
                            "message": "Provide a crash launch URL or a game name"}), 400
        cmd += ["--live", "--bet", str(live_bet)]
        if live_target:
            cmd += ["--target", live_target]
        cmd += ["--run-dir", run_dir, "--brand", brand, "--region", region]
        if is_mobile:
            cmd.append("--mobile")
        if is_headless:
            cmd.append("--headless")
        game_name = label   # label for CURRENT_RUN + the runs index below
    else:
        if not game_name or not username or not password:
            return jsonify({"status": "error", "message": "Missing fields"}), 400

        run_id = f"{started:%Y%m%d_%H%M%S}_{_slug(game_name)}"
        run_dir = os.path.join(RUNS_DIR, run_id)
        os.makedirs(run_dir, exist_ok=True)

        cmd = [sys.executable, "-u", "test_spin_button.py",
               "--game", game_name,
               "--username", username,
               "--password", password,
               "--brand", brand,
               "--region", region,
               "--run-dir", run_dir]
        if is_mobile:
            cmd.append("--mobile")
        if is_headless:
            cmd.append("--headless")
        if default_bet:
            cmd.extend(["--default-bet", default_bet])
        if min_bet:
            cmd.extend(["--min-bet", min_bet])
        # Pass --tests whenever the UI sent a selection list. Empty list => the user unchecked every
        # check (including core) => "none". Absent (None) => run everything.
        if isinstance(tests, list):
            cmd.extend(["--tests", ",".join(str(t) for t in tests) if tests else "none"])

    CURRENT_RUN = {
        "game": game_name,
        "run_id": run_id,
        "start_time": started.isoformat(),
        "status": "running"
    }
    # Prepend a record to the runs index (newest first).
    try:
        index = []
        if os.path.exists(RUNS_INDEX):
            with open(RUNS_INDEX, "r", encoding="utf-8") as f:
                index = json.load(f)
        # Crash's single-game card is LIVE-only now (2026-07-29) and writes real bet records
        # (see run_crash_tests' dsc_report.append_record call) — tag it distinctly so the
        # dashboard's Validate button can find it, without touching slot's "single" type at all.
        index.insert(0, {"run_id": run_id, "game": game_name, "brand": brand,
                         "region": region, "started_at": started.isoformat(),
                         "type": "crash-live" if game_type == "crash" else "single"})
        with open(RUNS_INDEX, "w", encoding="utf-8") as f:
            json.dump(index[:200], f, indent=2)
    except Exception as e:
        print(f"[runs] index write failed: {e}")

    _start_workers([("", cmd)])

    return jsonify({"status": "started"})


@app.route('/launch-batch', methods=['POST'])
def launch_batch():
    """Batch DSC sweep: an uploaded Excel of games, split round-robin across one browser
    worker per selected account. All workers fill ONE shared report (file-locked) seeded
    from the input sheet, so row order is preserved and a partial sweep is readable.
    Multipart form: excel (file), brand, region, accounts (JSON [{username,password}]),
    tests (JSON list, default ["dsc"])."""
    global CURRENT_RUN

    f = request.files.get('excel')
    if not f:
        return jsonify({"status": "error", "message": "No Excel file uploaded"}), 400
    brand = request.form.get('brand', 'betway')
    region = request.form.get('region', 'ZA')
    is_headless = request.form.get('headless') == 'true'
    try:
        accounts = json.loads(request.form.get('accounts') or '[]')
    except ValueError:
        accounts = []
    accounts = [a for a in accounts if a.get("username") and a.get("password")]
    if not accounts:
        return jsonify({"status": "error",
                        "message": "Select at least one worker account"}), 400
    try:
        tests = json.loads(request.form.get('tests') or '["dsc"]') or ["dsc"]
    except ValueError:
        tests = ["dsc"]

    started = datetime.now()
    batch_id = f"{started:%Y%m%d_%H%M%S}_DSC"
    batch_dir = os.path.join(RUNS_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)
    input_path = os.path.join(batch_dir, "input.xlsx")
    fname = (f.filename or "").lower()
    if fname.endswith(".csv"):
        # The dashboard file picker accepts .csv but openpyxl only reads xlsx — convert.
        import pandas as pd
        csv_path = os.path.join(batch_dir, "input.csv")
        f.save(csv_path)
        try:
            pd.read_csv(csv_path).to_excel(input_path, index=False)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Could not read the CSV: {e}"}), 400
    elif fname.endswith(".xls") and not fname.endswith(".xlsx"):
        return jsonify({"status": "error",
                        "message": "Legacy .xls isn't supported — save the sheet as .xlsx"}), 400
    else:
        f.save(input_path)

    from modules import dsc_report
    try:
        shards, total = dsc_report.shard_excel(input_path, batch_dir, len(accounts))
    except Exception as e:
        return jsonify({"status": "error", "message": f"Could not read the sheet: {e}"}), 400

    report = None
    if "dsc" in tests:
        # Fresh per-batch report (not the shared daily file): seeded from the FULL input
        # so row order survives sharding. Lives directly in runs/ so /dsc-report/latest
        # finds it. Workers' own ensure_report calls see it exists and leave it alone.
        report = os.path.join(RUNS_DIR, f"DSC_Report_{started:%Y-%m-%d_%H%M%S}.xlsx")
        dsc_report.ensure_report(report, seed_from=input_path)

    cmds = []
    for k, (shard, acc) in enumerate(zip(shards, accounts), 1):
        cmd = [sys.executable, "-u", "test_spin_button.py",
               "--excel", shard,
               "--username", acc["username"],
               "--password", acc["password"],
               "--brand", brand,
               "--region", region,
               "--run-dir", os.path.join(batch_dir, f"w{k}"),
               "--tests", ",".join(str(t) for t in tests)]
        if is_headless:
            cmd.append("--headless")
        if report:
            cmd.extend(["--dsc-report", report])
        # Cascade the worker windows (~36px steps) so every title bar stays clickable —
        # users resize/snap stacked windows to see them, and a RESIZE breaks coordinates.
        env = os.environ.copy()
        env["MELON_WINDOW_POS"] = f"{36 * (k - 1)},{36 * (k - 1)}"
        cmds.append((f"W{k}", cmd, env))

    batch = {"report": report, "total": total, "workers": len(cmds), "run_id": batch_id}
    CURRENT_RUN = {"game": f"Batch · {total} games", "run_id": batch_id,
                   "start_time": started.isoformat(), "status": "running", "batch": batch}
    try:
        index = []
        if os.path.exists(RUNS_INDEX):
            with open(RUNS_INDEX, "r", encoding="utf-8") as fh:
                index = json.load(fh)
        index.insert(0, {"run_id": batch_id, "game": f"Batch · {total} games",
                         "brand": brand, "region": region,
                         "started_at": started.isoformat(), "workers": len(cmds),
                         "type": "batch",
                         "report": os.path.basename(report) if report else None})
        with open(RUNS_INDEX, "w", encoding="utf-8") as fh:
            json.dump(index[:200], fh, indent=2)
    except Exception as e:
        print(f"[runs] index write failed: {e}")

    _start_workers(cmds, batch=batch)

    return jsonify({"status": "started", "total": total, "workers": len(cmds),
                    "run_id": batch_id,
                    "report": os.path.basename(report) if report else None})


def _sweep_targets(data):
    """Validate the {targets:[{brand,region,username,password,label?}]} a scope resolves to."""
    return [t for t in (data.get('targets') or [])
            if t.get('brand') and t.get('region') and t.get('username') and t.get('password')]


@app.route('/api/configured-pairs')
def api_configured_pairs():
    """The (brand, region) pairs that are actually testable — drives the scope→region resolution
    in the UI (which regions a scope covers, so it can auto-map saved accounts to them)."""
    from modules.utils import configured_pairs
    return jsonify({"pairs": [{"brand": b, "region": r} for (b, r) in configured_pairs()]})


@app.route('/api/sweep-plan', methods=['POST'])
def api_sweep_plan():
    """Preview the rotation plan grouped PER (brand, region) — NO spins, no money. Shows exactly
    which game each provider would get in each region so the plan is known before launch.
    JSON body: targets[{brand,region,username,password,label?}], limit, providers[]."""
    import dsc_sweep
    data = request.get_json(force=True) or {}
    targets = _sweep_targets(data)
    if not targets:
        return jsonify({"status": "error", "message": "No account resolved for the chosen scope — "
                        "add a saved account for each region you want to test"}), 400
    try:
        groups = dsc_sweep.plan_sweep(targets, data.get('providers') or None,
                                      data.get('limit') or None,
                                      one_per_region=bool(data.get('one_per_region')))
    except Exception as e:
        return jsonify({"status": "error", "message": f"Planning failed: {e}"}), 400
    out = [{"brand": g["brand"], "region": g["region"],
            "label": (g["accounts"][0].get("label") if g.get("accounts") else None),
            "account": (g["accounts"][0].get("username") if g.get("accounts") else None),
            "browsers": len(g.get("accounts", [])),
            "dropped": g.get("dropped", 0),
            "error": g.get("error"),
            "picks": [{"provider": p["provider"], "game": p["name"], "id": p["id"]}
                      for p in g.get("picks", [])]}
           for g in groups]
    total = sum(len(g["picks"]) for g in out)
    return jsonify({"groups": out, "total": total})


@app.route('/launch-sweep', methods=['POST'])
def launch_sweep():
    """Auto-DSC across one or many (brand, region)s. For each region: enumerate every provider,
    pick ONE untested game each (7-day rotation), then run the DSC — its games sharded across the
    region's selected accounts (one browser each). The whole fleet runs at most `parallel` browsers
    at a time. Results fill a per-region report and are recorded to the prod DB on completion
    (unless do_not_record). JSON body: targets[{brand,region,username,password,label?}], parallel,
    headless, do_not_record, limit, providers[]."""
    global CURRENT_RUN
    data = request.get_json(force=True) or {}
    is_headless = bool(data.get('headless'))
    do_not_record = bool(data.get('do_not_record'))
    limit = data.get('limit') or None
    only = data.get('providers') or None
    try:
        parallel = max(1, min(int(data.get('parallel') or 4), 8))
    except (TypeError, ValueError):
        parallel = 4
    targets = _sweep_targets(data)
    if not targets:
        return jsonify({"status": "error", "message": "No account resolved for the chosen scope"}), 400

    from modules import dsc_report
    import dsc_sweep

    try:
        groups = dsc_sweep.plan_sweep(targets, only, limit, max_parallel=parallel,
                                      one_per_region=bool(data.get('one_per_region')))
    except Exception as e:
        return jsonify({"status": "error", "message": f"Provider planning failed: {e}"}), 400
    planned = [g for g in groups if g.get("picks")]
    if not planned:
        errs = "; ".join(f"{g['brand']} {g['region']}: {g['error']}"
                         for g in groups if g.get("error"))
        filt = f" No provider matched your filter '{', '.join(only)}' — the site uses names like Red-Tiger, Pragmatic-Play." if only else ""
        return jsonify({"status": "error", "message":
                        "Nothing to test — every provider's games are in the 7-day rotation, "
                        "or no provider matched." + filt + (f" [{errs}]" if errs else "")}), 400

    started = datetime.now()
    ts = f"{started:%Y%m%d_%H%M%S}"
    batch_id = f"{ts}_DSCsweep"
    batch_dir = os.path.join(RUNS_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    worker_specs, group_meta = [], []
    total_games, widx = 0, 0
    for g in planned:
        b, r, picks, accts = g["brand"], g["region"], g["picks"], g["accounts"]
        gdir = os.path.join(batch_dir, f"{b}_{r}")
        os.makedirs(gdir, exist_ok=True)
        full_input = os.path.join(gdir, "input.xlsx")
        dsc_sweep._write_sheet(picks, full_input)
        report = os.path.join(RUNS_DIR, f"DSC_Sweep_{b}_{r}_{started:%Y-%m-%d_%H%M%S}.xlsx")
        dsc_report.ensure_report(report, seed_from=full_input)
        try:
            shards, n = dsc_report.shard_excel(full_input, gdir, len(accts))
        except Exception as e:
            _ = e; continue
        total_games += n
        for k, (shard, acc) in enumerate(zip(shards, accts), 1):
            widx += 1
            cmd = [sys.executable, "-u", "test_spin_button.py", "--excel", shard,
                   "--username", acc["username"], "--password", acc["password"],
                   "--brand", b, "--region", r,
                   "--run-dir", os.path.join(gdir, f"w{k}"),
                   "--tests", "dsc", "--dsc-report", report]
            if is_headless:
                cmd.append("--headless")
            env = os.environ.copy()
            step = 36 * ((widx - 1) % parallel)     # cascade within a wave; title bars stay clickable
            env["MELON_WINDOW_POS"] = f"{step},{step}"
            worker_specs.append({"label": f"{r}·W{k}", "cmd": cmd, "env": env})
        group_meta.append({"brand": b, "region": r, "report": report, "picks": picks,
                           "record": not do_not_record, "run_id": batch_id})

    n_regions = len(group_meta)
    scope_label = (f"{n_regions} regions" if n_regions > 1
                   else f"{group_meta[0]['brand']} {group_meta[0]['region']}")
    CURRENT_RUN = {"game": f"Auto-sweep · {total_games} games · {scope_label}", "run_id": batch_id,
                   "start_time": started.isoformat(), "status": "running",
                   "batch": {"report": group_meta[0]["report"] if group_meta else None,
                             "total": total_games, "workers": len(worker_specs),
                             "run_id": batch_id}}
    try:
        index = []
        if os.path.exists(RUNS_INDEX):
            with open(RUNS_INDEX, "r", encoding="utf-8") as fh:
                index = json.load(fh)
        index.insert(0, {"run_id": batch_id, "game": f"Auto-sweep · {total_games} games · {scope_label}",
                         "brand": group_meta[0]["brand"], "region": group_meta[0]["region"],
                         "started_at": started.isoformat(), "workers": len(worker_specs),
                         "type": "sweep", "report": os.path.basename(group_meta[0]["report"])})
        with open(RUNS_INDEX, "w", encoding="utf-8") as fh:
            json.dump(index[:200], fh, indent=2)
    except Exception as e:
        print(f"[runs] index write failed: {e}")

    _start_fleet(worker_specs, parallel, group_meta)
    return jsonify({"status": "started", "total": total_games, "workers": len(worker_specs),
                    "parallel": parallel, "regions": n_regions, "run_id": batch_id,
                    "record": not do_not_record,
                    "groups": [{"brand": g["brand"], "region": g["region"],
                                "games": len(g["picks"])} for g in group_meta]})


@app.route('/api/crash-sweep-plan', methods=['POST'])
def api_crash_sweep_plan():
    """Preview every crash title available for a (brand, region) — ONE live-catalog call
    (provider_sweep.list_crash_games reads the Categories API's curated 'crashgames' bucket
    directly, unlike the slot sweep which must walk every provider). No spins, no money.
    Crash is explicitly OUT of the DSC weekly-rotation compliance pass slots use (see
    SLOT_TEST_CHECKLIST.md — 'handled outside the slot UI pass'), so this is a plain
    discover-and-list, not a rotation pick, and nothing here touches dsc_history_db.
    JSON body: {username, password, brand, region}."""
    data = request.get_json(force=True) or {}
    username, password = data.get('username', ''), data.get('password', '')
    brand = (data.get('brand') or 'betway').lower()
    region = (data.get('region') or 'ZA').upper()
    if not username or not password:
        return jsonify({"status": "error", "message": "Select an account first"}), 400
    try:
        token, err = _get_token(username, password, brand=brand, region=region)
        if not token:
            return jsonify({"status": "error", "message": f"auth failed: {err}"}), 400
        import modules.provider_sweep as provider_sweep
        games = provider_sweep.list_crash_games(brand, region, token)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Discovery failed: {e}"}), 400
    return jsonify({"status": "ok", "games": games, "total": len(games)})


@app.route('/launch-crash-sweep', methods=['POST'])
def launch_crash_sweep():
    """Batch-run a set of discovered crash titles. Dry-run by default; live=true places a REAL
    wager on EVERY selected game, one after another (2026-07-29, explicit request — "whatever I
    select in discover games should be considered in live execution"). Stake is re-validated
    server-side against config_env.MAX_STAKE per game — the same cap the single-game Live card
    enforces, just applied N times here, and total exposure (games x stake) is surfaced in the
    response/label so it's never a silent multiply. Runs ONE AT A TIME regardless of dry-run or
    live — max_parallel is hard-capped to 1 here on purpose: every game in the batch
    authenticates with the SAME account, and a real concurrent second login on one account is
    exactly the 'session held by another tab' disconnect crash_auto.py's auto_handle_crash_startup
    already has to detect and abort on (see modules/auth_handler.py's sessionTrackingToken note).
    That risk is WORSE, not better, in live mode — a dropped session mid-bet is a real open
    position, not just a wasted observation — so the one-at-a-time cap is non-negotiable here.
    Spreading a crash sweep across MULTIPLE accounts in parallel (like the slot Auto Sweep does)
    is future work — not built here, so don't silently pretend a 'parallel' knob exists.
    JSON body: picks[{id,name,provider}], username, password, brand, region, headless,
    live, bet, target."""
    global CURRENT_RUN
    data = request.get_json(force=True) or {}
    picks = data.get('picks') or []
    username, password = data.get('username', ''), data.get('password', '')
    brand = (data.get('brand') or 'betway').lower()
    region = (data.get('region') or 'ZA').upper()
    is_headless = bool(data.get('headless'))
    if not username or not password:
        return jsonify({"status": "error", "message": "Select an account first"}), 400
    picks = [p for p in picks if (p or {}).get('name')]
    if not picks:
        return jsonify({"status": "error", "message": "No crash games selected"}), 400

    is_live = bool(data.get('live'))
    live_bet, live_target = 0, (data.get('target') or '').strip()
    if is_live:
        import config_env
        try:
            live_bet = float(data.get('bet') or 0)
        except (TypeError, ValueError):
            live_bet = 0
        if live_bet <= 0:
            return jsonify({"status": "error", "message": "Live mode needs a positive stake amount"}), 400
        if live_bet > config_env.MAX_STAKE:
            return jsonify({"status": "error",
                            "message": f"Stake {live_bet:g} exceeds the safety cap "
                                       f"{config_env.MAX_STAKE:g}"}), 400

    started = datetime.now()
    batch_id = f"{started:%Y%m%d_%H%M%S}_crash_sweep"
    batch_dir = os.path.join(RUNS_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    worker_specs = []
    for i, p in enumerate(picks, 1):
        name = p["name"]
        gdir = os.path.join(batch_dir, f"{i:02d}_{_slug(name)}")
        os.makedirs(gdir, exist_ok=True)
        # Persist the discovery-time pick (id/name/provider) alongside the run — crash_auto.py's
        # own results.json has no provider field, so the Excel exporter (_crash_report_rows)
        # needs this to fill that column without a second live catalog call.
        try:
            with open(os.path.join(gdir, "pick.json"), "w", encoding="utf-8") as fh:
                json.dump({"id": p.get("id"), "name": name, "provider": p.get("provider")}, fh)
        except Exception as e:
            print(f"[crash-sweep] pick.json write failed for {name}: {e}")
        cmd = [sys.executable, "-u", "crash_auto.py", "--game", name,
               "--username", username, "--password", password,
               "--brand", brand, "--region", region]
        if is_live:
            cmd += ["--live", "--bet", str(live_bet)]
            if live_target:
                cmd += ["--target", live_target]
        else:
            # Dry-run only (run_crash_tests_with_retry refuses retries under --live — see its
            # docstring): retry up to 3x with a fresh launch URL on a session-drop or failed
            # control detection, since the 2026-08-03 provider validation sweep proved the SAME
            # game can flip between a clean pass and a false-negative failure across attempts.
            cmd += ["--dry-run", "--retries", "3"]
        cmd += ["--run-dir", gdir]
        if is_headless:
            cmd.append("--headless")
        worker_specs.append({"label": _slug(name, 16), "cmd": cmd, "env": None})

    total_games = len(worker_specs)
    mode_label = f"LIVE (stake={live_bet:g} x {total_games} = {live_bet*total_games:g} exposure)" if is_live else "dry-run"
    CURRENT_RUN = {"game": f"Crash sweep · {total_games} games · {mode_label} · {brand} {region}",
                   "run_id": batch_id, "start_time": started.isoformat(), "status": "running",
                   "batch": {"report": None, "total": total_games, "workers": total_games,
                             "run_id": batch_id}}
    try:
        index = []
        if os.path.exists(RUNS_INDEX):
            with open(RUNS_INDEX, "r", encoding="utf-8") as fh:
                index = json.load(fh)
        index.insert(0, {"run_id": batch_id, "game": f"Crash sweep · {total_games} games · {mode_label}",
                         "brand": brand, "region": region,
                         "started_at": started.isoformat(), "workers": total_games,
                         "type": "crash-sweep", "report": None})
        with open(RUNS_INDEX, "w", encoding="utf-8") as fh:
            json.dump(index[:200], fh, indent=2)
    except Exception as e:
        print(f"[runs] index write failed: {e}")

    # max_parallel=1 (same-account risk, see docstring) + a cooldown gap between launches so the
    # casino backend's session lock for this account has time to release before the next game
    # requests a launch — see _start_fleet's cooldown docstring for the evidence behind this.
    _start_fleet(worker_specs, 1, [], cooldown=20)
    return jsonify({"status": "started", "total": total_games, "run_id": batch_id,
                    "live": is_live, "total_exposure": (live_bet * total_games) if is_live else 0})


@app.route('/api/history')
def api_history():
    """Prod-DB audit view for the History panel: most-recent tested games, optionally filtered."""
    from modules import dsc_history_db
    brand = request.args.get('brand') or None
    region = request.args.get('region') or None
    try:
        limit = min(int(request.args.get('limit', 100)), 500)
    except ValueError:
        limit = 100
    return jsonify({"rows": dsc_history_db.recent_rows(brand, region, limit)})


@app.route('/stream')
def stream():
    """Broadcast SSE: each client reads LOG_HISTORY from position 0, so a client that
    connects (or reconnects) mid-run still receives the whole log, and any number of
    tabs can watch the same run simultaneously."""
    def generate():
        while LOG_Q is None:
            time.sleep(0.3)
        pos = 0
        idle = 0.0
        while True:
            hist = list(LOG_HISTORY)
            if pos > len(hist):      # a new run cleared the log under us — hand off
                break
            if pos < len(hist):
                for line in hist[pos:]:
                    yield f"data: {line}\n\n"
                pos = len(hist)
                idle = 0.0
                continue
            if RUN_DONE["v"]:
                break
            time.sleep(0.25)
            idle += 0.25
            if idle >= 15:
                # SSE keep-alive comment so proxies/browsers don't drop an idle stream.
                yield ": ping\n\n"
                idle = 0.0
        yield "data: [MELON_STREAM_END]\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/stop', methods=['POST'])
def stop():
    global CURRENT_RUN
    FLEET_STOP.set()   # stop a capped fleet from launching further queued waves
    if _workers_alive():
        _stop_workers()
        CURRENT_RUN["status"] = "stopped"
        return jsonify({"status": "stopped"})
    return jsonify({"status": "idle"})


if __name__ == '__main__':
    app.run(debug=True, port=int(os.environ.get("MELON_PORT", 5000)), threaded=True)
