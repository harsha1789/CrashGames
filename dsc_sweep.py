"""
dsc_sweep.py — the auto-DSC orchestrator. THE meaning of DSC: for a (brand, region), go to
every provider on the site, test ONE game per provider, honour the weekly rotation, and record
what ran to the prod database.

Design: this is a thin wrapper around the ALREADY-PROVEN per-game DSC runner (test_spin_button.py
--tests dsc). It only decides WHICH games to test — provider enumeration + rotation-aware pick —
then hands a generated input sheet to the runner and records the results. Zero changes to the
runner, so the robust spin / min-bet / tlogs / report logic is reused verbatim.

Flow:
  auth -> providers -> pick one untested game each (skip games in the 7-day cooldown) -> write a
  temp input sheet -> run the DSC batch (subprocess) -> read its records -> record_test() each into
  dsc_history.db (unless --no-record).

  python dsc_sweep.py --brand betway --region ZA --username U --password P
                      [--limit N] [--providers Red-Tiger,Habanero] [--no-record] [--headed]
"""
import os
import re
import sys
import json
import argparse
import subprocess
from datetime import datetime

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from openpyxl import Workbook

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.auth_handler import AuthHandler
from modules import provider_sweep, dsc_history_db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(BASE_DIR, "runs")


def _norm_provider(s):
    """Fold a provider name to a match key: lowercase, strip everything but a-z0-9. So the API's
    'Red-Tiger', a user's 'red tiger', and 'RedTiger' all collapse to 'redtiger'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def plan_picks(brand, region, token, only_providers=None, limit=None):
    """Rotation-aware plan: for each provider (optionally filtered), pick one game not tested in
    the last cooldown window. A game already picked in THIS plan is also excluded, so two
    providers can't land on the same title. Returns [{provider, id, name, gameType}]."""
    providers = provider_sweep.get_providers(brand, region, token)
    if only_providers:
        # Forgiving match: normalize both sides (case / spaces / hyphens) and allow substrings, so
        # 'red tiger' finds 'Red-Tiger' and a bare 'pragmatic' finds 'Pragmatic-Play'.
        wants = [w for w in (_norm_provider(p) for p in only_providers) if w]
        providers = [p for p in providers
                     if any(w in _norm_provider(p) or _norm_provider(p) in w for w in wants)]
    exclude = set(dsc_history_db.recently_tested(brand, region))  # weekly cooldown
    picks = []
    for prov in providers:
        try:
            g = provider_sweep.pick_game(brand, region, token, prov, exclude_ids=exclude)
        except Exception as e:
            print(f"  [SWEEP] {prov}: game list failed ({str(e)[:60]}) — skipped")
            continue
        if not g:
            print(f"  [SWEEP] {prov}: nothing testable left this week — skipped")
            continue
        picks.append({"provider": prov, "id": str(g.get("id")),
                      "name": g.get("name"), "gameType": g.get("gameType") or ""})
        exclude.add(str(g.get("id")))
        if limit and len(picks) >= limit:
            break
    return picks


def _auth_msg(auth):
    """A clean one-line reason from an auth failure. AuthHandler stringifies the whole response
    into `message`, but keeps the parsed dict in `raw` — pull the human errorMessage from there
    (e.g. 'NumberOfFailedLoginAttemptsExceeded') instead of dumping the raw JSON at the user."""
    raw = auth.get("raw")
    if isinstance(raw, dict) and raw.get("errorMessage"):
        return str(raw["errorMessage"])[:90]
    m = auth.get("message")
    if isinstance(m, dict):
        m = m.get("errorMessage") or m
    return str(m)[:90]


def plan_sweep(targets, only_providers=None, limit=None, max_parallel=6, one_per_region=False):
    """Plan a MULTI-region sweep. `targets` = [{brand,region,username,password,label?}] (the UI
    sends every in-scope region + the account(s) it could use). Rows are grouped by (brand,region)
    so a region is planned ONCE and its games sharded across accounts later — not planned per-account
    (which would pick the same games twice).

    SMART account selection ("is this combination possible?"): each account is auth-checked and a
    LOCKED / dead one is dropped. `one_per_region=True` (multi-region scopes) keeps just the first
    account that authenticates → one browser per region. `one_per_region=False` (single-region
    scope) keeps ALL that authenticate → the user's chosen parallel browsers, minus any dead ones.

    Groups are planned concurrently (auth + provider enumeration is ~40 HTTP calls/region; serial
    across 8 regions would take minutes). Returns [{brand,region,accounts,picks,dropped,error?}]."""
    from collections import OrderedDict
    from concurrent.futures import ThreadPoolExecutor
    groups = OrderedDict()
    for t in targets:
        key = (t["brand"], t["region"])
        g = groups.setdefault(key, {"brand": t["brand"], "region": t["region"], "accounts": []})
        g["accounts"].append({"username": t["username"], "password": t["password"],
                              "label": t.get("label")})

    def plan_one(g):
        working, token, fails, last_err = [], None, 0, None
        for acc in g["accounts"]:
            auth = AuthHandler().authenticate(acc["username"], acc["password"],
                                              brand=g["brand"], region=g["region"])
            if auth.get("success"):
                working.append(acc)
                if token is None:
                    token = auth["token"]
                if one_per_region:
                    break            # one live account is enough for this region
            else:
                fails += 1
                last_err = _auth_msg(auth)
        if token is None:
            return {**g, "accounts": [], "picks": [], "dropped": fails,
                    "error": f"auth failed: {last_err}"}
        try:
            return {**g, "accounts": working, "dropped": fails,
                    "picks": plan_picks(g["brand"], g["region"], token, only_providers, limit)}
        except Exception as e:
            return {**g, "accounts": working, "dropped": fails, "picks": [], "error": str(e)[:140]}

    vals = list(groups.values())
    with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(vals)))) as ex:
        return list(ex.map(plan_one, vals))


def _write_sheet(picks, path):
    """Input sheet in the format the runner's parse_excel reads (Sr. No.|Provider|Game Name|Game Type)."""
    wb = Workbook(); ws = wb.active; ws.title = "DSC"
    ws.append(["Sr. No.", "Provider", "Game Name", "Game Type"])
    for i, p in enumerate(picks, 1):
        ws.append([i, p["provider"], p["name"], p["gameType"]])
    wb.save(path)


def run_sweep(brand, region, username, password, only_providers=None, limit=None,
              record=True, headed=False):
    auth = AuthHandler().authenticate(username, password, brand=brand, region=region)
    if not auth.get("success"):
        print(f"❌ Auth failed for {brand}/{region}: {auth.get('message')}")
        return 1
    token = auth["token"]

    print(f"[SWEEP] Planning {brand}/{region} …")
    picks = plan_picks(brand, region, token, only_providers, limit)
    if not picks:
        print("[SWEEP] No games to test (all providers covered this week, or none matched).")
        return 0
    print(f"[SWEEP] {len(picks)} game(s) to test (1 per provider):")
    for p in picks:
        print(f"    {p['provider']:24} -> {p['name']} (id {p['id']})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    sheet = os.path.join(RUNS, f"DSC_Auto_{ts}_input.xlsx")
    report = os.path.join(RUNS, f"DSC_Auto_{ts}.xlsx")
    run_dir = os.path.join(RUNS, f"{ts}_DSCauto")
    os.makedirs(RUNS, exist_ok=True)
    _write_sheet(picks, sheet)

    cmd = [sys.executable, "-u", os.path.join(BASE_DIR, "test_spin_button.py"),
           "--excel", sheet, "--brand", brand, "--region", region,
           "--username", username, "--password", password,
           "--tests", "dsc", "--dsc-report", report, "--run-dir", run_dir]
    if not headed:
        cmd.append("--headless")
    print(f"[SWEEP] Running DSC on {len(picks)} game(s)…")
    subprocess.run(cmd, cwd=BASE_DIR)

    # Record results into the prod DB (unless --no-record). Match each pick to its record row
    # by game name; a game with no record row (never launched) is still logged as attempted=False.
    records_path = report.replace(".xlsx", "_records.jsonl")
    by_name = {}
    if os.path.exists(records_path):
        for line in open(records_path, encoding="utf-8"):
            line = line.strip()
            if line:
                r = json.loads(line)
                by_name[(r.get("game") or "").strip().lower()] = r
    if not record:
        print(f"[SWEEP] --no-record: {len(picks)} result(s) NOT written to the prod DB")
    else:
        n = 0
        for p in picks:
            out = by_name.get((p["name"] or "").strip().lower(), {})
            dsc_history_db.record_test(brand, region, p["provider"], p["id"], p["name"],
                                       out, run_id=os.path.basename(run_dir), record=True)
            n += 1
        print(f"[SWEEP] Recorded {n} result(s) to {os.path.basename(dsc_history_db.DB_PATH)}")

    print(f"[SWEEP] Done. Report: {os.path.basename(report)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Auto-DSC: test one game per provider, with weekly rotation.")
    ap.add_argument("--brand", default="betway")
    ap.add_argument("--region", default="ZA")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--providers", default="", help="comma-separated subset (default: all providers)")
    ap.add_argument("--limit", type=int, default=None, help="max games to test this run")
    ap.add_argument("--no-record", action="store_true", help="run WITHOUT writing to the prod DB")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--plan-only", action="store_true", help="print the rotation plan and exit (no spins)")
    args = ap.parse_args()

    provs = [p.strip() for p in args.providers.split(",") if p.strip()] or None
    if args.plan_only:
        auth = AuthHandler().authenticate(args.username, args.password, brand=args.brand, region=args.region)
        if not auth.get("success"):
            print(f"❌ Auth failed: {auth.get('message')}"); sys.exit(1)
        picks = plan_picks(args.brand, args.region, auth["token"], provs, args.limit)
        print(f"[SWEEP] Plan for {args.brand}/{args.region} — {len(picks)} game(s):")
        for p in picks:
            print(f"    {p['provider']:24} -> {p['name']} (id {p['id']})")
        sys.exit(0)

    sys.exit(run_sweep(args.brand, args.region, args.username, args.password,
                       provs, args.limit, record=not args.no_record, headed=args.headed))
