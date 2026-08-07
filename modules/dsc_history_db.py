"""
dsc_history_db.py — the DSC "prod database": a durable log of every game tested, so the
auto-sweep can (a) rotate — never re-test a game within a cooldown window (default 7 days)
per (brand, region) — and (b) keep an auditable record of what ran, when, and how it went.

SQLite, one file (dsc_history.db). Deliberately tiny: one table, a couple of queries. The
`record` flag lets a run execute WITHOUT persisting (the "do not record this test" switch) —
useful for ad-hoc re-tests that shouldn't burn a game's weekly slot or pollute the audit log.
"""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsc_history.db")
COOLDOWN_DAYS = 7   # a game tested for a (brand, region) is off-limits this many days

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  brand       TEXT NOT NULL,
  region      TEXT NOT NULL,
  provider    TEXT NOT NULL,
  game_id     TEXT NOT NULL,
  game_name   TEXT,
  tested_at   TEXT NOT NULL,          -- ISO-8601 local time
  launch      INTEGER,                -- 0/1
  bet_placed  INTEGER,                -- 0/1
  tlogs       TEXT,                   -- Pass | Fail | Pending | NA
  wager       REAL,
  payout      REAL,
  txid        TEXT,
  error       TEXT,
  run_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_rotation ON tests (brand, region, game_id, tested_at);
"""


def _conn(path=DB_PATH):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def init_db(path=DB_PATH):
    with closing(_conn(path)) as c, c:
        c.executescript(_SCHEMA)


def recently_tested(brand, region, days=COOLDOWN_DAYS, now=None, path=DB_PATH):
    """game_ids tested for this (brand, region) within the cooldown window — the rotation
    exclusion set. Uses a passed-in `now` so tests are deterministic."""
    init_db(path)
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).isoformat()
    with closing(_conn(path)) as c:
        rows = c.execute(
            "SELECT DISTINCT game_id FROM tests WHERE brand=? AND region=? AND tested_at>=?",
            (brand, region, cutoff)).fetchall()
    return {r["game_id"] for r in rows}


def record_test(brand, region, provider, game_id, game_name, outcome, run_id="",
                record=True, now=None, path=DB_PATH):
    """Persist one game's DSC result. `record=False` performs a no-op (the 'do not record'
    switch) so the game keeps its weekly slot and the audit log stays clean. `outcome` is the
    dict from slot_dsc.run_dsc (launch/bet_placed/tlogs/wager/payout/txid/errors)."""
    if not record:
        return False
    init_db(path)
    now = now or datetime.now()
    bet_placed = bool(outcome.get("bet_placed"))
    # `outcome` may be the run_dsc out-dict OR a records.jsonl row — read either shape. The
    # records store the effective wager under "wager"; tlogs is a bool (verified on the wire).
    tl = outcome.get("tlogs")
    if isinstance(tl, bool):
        # `tl` is the WIRE truth (money moved on the spin response) — that's captured by
        # bet_placed + wager. The Tlogs column is the TRANSACTION-HISTORY verdict, filled only by
        # the phase-2 validator, so a freshly-swept game is "Pending" until that runs — matching
        # what the Excel report writes (slot_dsc leaves Tlogs "Pending"). Keeps DB ↔ report in sync.
        tlogs = "Pending" if bet_placed else "NA"
    else:
        tlogs = tl or ("Pending" if bet_placed else "NA")
    wager = outcome.get("wager_effective")
    if wager is None:
        wager = outcome.get("wager")
    txid = outcome.get("round_id") or outcome.get("txid") or outcome.get("betId")
    with closing(_conn(path)) as c, c:
        c.execute(
            """INSERT INTO tests (brand,region,provider,game_id,game_name,tested_at,launch,
                                  bet_placed,tlogs,wager,payout,txid,error,run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (brand, region, provider, str(game_id), game_name, now.isoformat(),
             int(bool(outcome.get("launch"))), int(bet_placed), tlogs,
             wager, outcome.get("payout"), txid,
             "; ".join(outcome.get("errors") or []) or None, run_id))
    return True


def recent_rows(brand=None, region=None, limit=200, path=DB_PATH):
    """Audit view: most-recent tests, optionally filtered by brand/region."""
    init_db(path)
    q = "SELECT * FROM tests"
    conds, args = [], []
    if brand:  conds.append("brand=?");  args.append(brand)
    if region: conds.append("region=?"); args.append(region)
    if conds:  q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY tested_at DESC LIMIT ?"; args.append(limit)
    with closing(_conn(path)) as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


if __name__ == "__main__":
    # Self-check: rotation excludes only within the window, record=False is a no-op.
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "dsc_history_selftest.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    now = datetime(2026, 7, 21, 12, 0, 0)
    out = {"launch": True, "bet_placed": True, "attempted": True, "wager_effective": 0.1,
           "payout": 0.0, "round_id": "abc", "errors": []}
    record_test("betway", "ZA", "Red-Tiger", "7748", "Betway 777 Strike", out,
                now=now, path=tmp)
    # a game tested 8 days ago must NOT be excluded; today's must be
    record_test("betway", "ZA", "Habanero", "999", "Old Game", out,
                now=now - timedelta(days=8), path=tmp)
    excl = recently_tested("betway", "ZA", now=now, path=tmp)
    assert "7748" in excl, excl
    assert "999" not in excl, excl
    # record=False is a no-op
    assert record_test("betway", "ZA", "X", "555", "No Record", out, record=False,
                       now=now, path=tmp) is False
    assert "555" not in recently_tested("betway", "ZA", now=now, path=tmp)
    # region isolation
    assert recently_tested("betway", "GH", now=now, path=tmp) == set()
    print("dsc_history_db self-check OK")
    try:
        os.remove(tmp)
    except OSError:
        pass   # Windows may hold a brief lock on the sqlite file; harmless in a self-check
