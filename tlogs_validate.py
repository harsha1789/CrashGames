"""
tlogs_validate.py — Phase-2 Tlogs: verify Melon's recorded spins against the REAL
transaction history on the Betway site.
================================================================================
Melon's DSC sweep writes one JSON line per spin into runs/DSC_Report_*_records.jsonl
(schema: RECORDS.md). Betway reflects bets in ~5 minutes, so shortly after the sweep
this script:

  1. groups the records by worker account,
  2. logs into the BRAND SITE with each account (Playwright, headed or headless),
  3. opens Transaction History and scrapes every entry back to the sweep window
     (paginating as needed),
  4. reconciles each recorded spin — wager amount, minute-window time, the running
     balance printed in parentheses (the clincher), and the payout pair — and captures
     the wallet-side transaction GUID that only the site shows,
  5. writes <records>_validation.json next to the records file and emits a
     VALIDATIONPAYLOAD=== … ===VALIDATIONPAYLOAD block for the dashboard.

Every site-specific detail lives in SITES[(brand, region)] — ZA is implemented; new
regions are one selector-pack away.

CLI:
  python tlogs_validate.py --records runs/DSC_Report_2026-07-10_131539_records.jsonl
                           [--brand betway] [--region ZA] [--headed] [--window-min 8]
"""
import os
import re
import sys
import json
import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from playwright.async_api import async_playwright

# Direct console runs on Windows default to cp1252, which can't print the icons the
# dashboard pipe (utf-8) handles fine — normalize so both contexts work.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules.utils import add_country_code, region_config, auth_headers  # noqa: E402
from modules.auth_handler import AuthHandler  # noqa: E402
from curl_cffi import requests as _creq  # noqa: E402  (Cloudflare-passing HTTP, like the sweep's auth)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─── per-(brand, region) site pack ────────────────────────────────────────────
# Selectors come from the live ZA site (captured 2026-07-10). The history menu anchor's id
# literally contains spaces, hence the attribute selector.
SITES = {
    ("betway", "ZA"): {
        "site": "https://www.betway.co.za",
        "history_url": "https://www.betway.co.za/?account=transaction-history",
        "user_sel": "#header-username",
        "pass_sel": "#header-password",
        "login_btn_sel": "#login-btn",
        "hamburger_sel": "#header-hamburger-btn",
        "history_menu_sel": '[id="Transaction History-hamburger-menu-btn"]',
        "next_page_sel": "#transaction-history-next",   # sticky footer: prev · "page N" · next
    },
}


def _site_pack(brand, region):
    pack = SITES.get(((brand or "betway").lower(), (region or "ZA").upper()))
    if not pack:
        raise SystemExit(f"[TLOGS] {brand}/{region} has no site pack yet — add selectors to "
                         f"SITES in tlogs_validate.py (ZA is the template).")
    return pack


# ─── history-row parsing (pure, testable) ─────────────────────────────────────
_ROW_JS = """
() => {
  const out = [];
  for (const row of document.querySelectorAll('div[class*="grid-cols-"]')) {
    const cells = [...row.children].filter(c => c.tagName === 'DIV');
    if (cells.length < 4) continue;
    const t = c => (c.textContent || '').trim().replace(/\\s+/g, ' ');
    out.push({date: t(cells[0]), type: t(cells[1]), amount: t(cells[2]), txid: t(cells[3])});
  }
  return out;
}
"""

_AMOUNT_RE = re.compile(r"R\s*(-?[\d,]+(?:\.\d+)?)\s*\(([\d,]+(?:\.\d+)?)\)")
_DATE_RE = re.compile(r"^(Today|Yesterday|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s*\|\s*(\d{1,2}):(\d{2})$")


def parse_row(raw, now=None):
    """One scraped row {date,type,amount,txid} -> a typed entry, or None for non-rows
    (header, malformed). Times are the site's client-rendered local time — same machine,
    same timezone as the recorder. ZA dates are dd/mm/yyyy."""
    m = _AMOUNT_RE.search(raw.get("amount") or "")
    d = _DATE_RE.match((raw.get("date") or "").strip())
    if not m or not d:
        return None
    now = now or datetime.now()
    day_txt, hh, mm = d.group(1), int(d.group(2)), int(d.group(3))
    if day_txt == "Today":
        day = now.date()
    elif day_txt == "Yesterday":
        day = (now - timedelta(days=1)).date()
    else:
        dd, mo, yy = re.split(r"[./-]", day_txt)
        yy = int(yy) + (2000 if int(yy) < 100 else 0)
        day = datetime(int(yy), int(mo), int(dd)).date()
    when = datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm)

    type_txt = (raw.get("type") or "").strip()
    kind = "Payout" if type_txt.endswith("Payout") else ("Wager" if type_txt.endswith("Wager") else type_txt)
    provider = re.sub(r"\s*(Wager|Payout)$", "", type_txt).strip()
    return {"time": when, "kind": kind, "provider": provider,
            "amount": float(m.group(1).replace(",", "")),
            "running": float(m.group(2).replace(",", "")),
            "txid": (raw.get("txid") or "").strip()}


def _parse_record_time(rec):
    """Best reference time for a record, as NAIVE LOCAL time: server_time (UTC, exact,
    RagingRiver/Bugatti formats) beats spin_at (client clock at the click)."""
    st = rec.get("server_time")
    if st:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
            try:
                return (datetime.strptime(st, fmt).replace(tzinfo=timezone.utc)
                        .astimezone().replace(tzinfo=None))
            except ValueError:
                continue
    sa = rec.get("spin_at")
    if sa:
        try:
            return datetime.fromisoformat(sa).astimezone().replace(tzinfo=None)
        except ValueError:
            pass
    return None


# ─── reconciliation (pure, testable) ──────────────────────────────────────────
EPS = 0.011


def reconcile(records, entries, window_min=8.0):
    """Match every record against the scraped history entries (one account's worth).
    Matching: kind=Wager + |amount|==wager + time within ±window; the running balance
    (history's parenthetical == our balance_after_bet, fallback balance_before-wager)
    picks between same-amount candidates and upgrades the verdict. Each history entry
    is consumed at most once."""
    window = timedelta(minutes=window_min)
    wagers = [e for e in entries if e["kind"] == "Wager"]
    payouts = [e for e in entries if e["kind"] == "Payout"]
    used = set()
    results = []

    for rec in sorted(records, key=lambda r: _parse_record_time(r) or datetime.min):
        ref = _parse_record_time(rec)
        wager = rec.get("wager")
        res = {"game": rec.get("game"), "account": rec.get("account"),
               "srNo": rec.get("srNo"), "provider": rec.get("provider"),
               "wager": wager, "payout": rec.get("payout"),
               "ref_time": ref.strftime("%Y-%m-%d %H:%M:%S") if ref else None,
               "round_id": rec.get("round_id"),
               "status": None, "txid": None, "entry_time": None, "entry_provider": None,
               "running_balance": None, "balance_ok": None, "payout_ok": None,
               "shot": None, "notes": []}

        if not rec.get("bet_placed"):
            # We claimed no bet — history must agree. A matching wager here means our
            # verdict under-reported real money movement: loudest possible flag.
            ghost = None
            if ref and wager:
                ghost = next((e for i, e in enumerate(wagers) if i not in used
                              and abs(abs(e["amount"]) - wager) <= EPS
                              and abs(e["time"] - ref) <= window), None)
            if ghost:
                res["status"] = "VIOLATION_unrecorded_bet"
                res["txid"] = ghost["txid"]
                res["notes"].append("record says bet NOT placed, but history shows a matching wager")
            else:
                res["status"] = "absent_ok"
                res["notes"].append(f"no bet expected ({rec.get('not_attempted_reason') or 'not placed'}) — none found")
            results.append(res)
            continue

        if ref is None or wager is None:
            res["status"] = "unmatchable"
            res["notes"].append("record lacks a usable time or wager amount")
            results.append(res)
            continue

        # Candidate amounts: the balance-delta wager first, but ALSO the response wager
        # when they disagree — on a SHARED wallet a concurrent worker's spin lands
        # between our balance reads and inflates the delta (2026-07-13: China Pots'
        # real 0.50 wager reported as 0.60 because a parallel Gold Blitz autoplay
        # round deducted 0.10 in between; history knows only 0.50).
        amounts = [wager]
        wr = rec.get("wager_response")
        if wr is not None and abs(wr - wager) > EPS:
            amounts.append(wr)
        cands = [(i, e) for i, e in enumerate(wagers) if i not in used
                 and any(abs(abs(e["amount"]) - a) <= EPS for a in amounts)
                 and abs(e["time"] - ref) <= window]
        expected_running = rec.get("balance_after_bet")
        if expected_running is None and rec.get("balance_before") is not None:
            expected_running = round(rec["balance_before"] - wager, 2)

        best = None
        if cands:
            # Some brands (JPC) don't return a running balance — running is None there. Only do
            # the balance cross-check when the entries actually carry one; otherwise leave
            # balance_ok=None (amount+time match alone) rather than falsely failing it.
            has_running = any(e["running"] is not None for _, e in cands)
            with_balance = [(i, e) for i, e in cands
                            if expected_running is not None and e["running"] is not None
                            and abs(e["running"] - expected_running) <= EPS]
            pool = with_balance or cands
            best = min(pool, key=lambda ie: abs(ie[1]["time"] - ref))
            res["balance_ok"] = (bool(with_balance)
                                 if (expected_running is not None and has_running) else None)

        if best is None:
            res["status"] = "missing"
            res["notes"].append(f"no history wager of {wager:g} within ±{window_min:g} min of {res['ref_time']}")
        else:
            i, e = best
            used.add(i)
            res.update({"txid": e["txid"], "entry_time": e["time"].strftime("%Y-%m-%d %H:%M"),
                        "entry_provider": e["provider"], "running_balance": e["running"],
                        "entry_amount": abs(e["amount"])})
            res["status"] = "verified" if res["balance_ok"] in (True, None) else "verified_amount_time_only"
            if abs(abs(e["amount"]) - wager) > EPS:
                res["notes"].append(
                    f"matched on the response wager {abs(e['amount']):g} — the balance-delta "
                    f"{wager:g} was contaminated by concurrent activity on the shared wallet")
            if res["balance_ok"] is False:
                res["notes"].append(f"running balance {e['running']:g} != expected {expected_running:g}"
                                    " (other activity on the account between spins?)")
            # Payout pair: wager+payout share the wallet GUID on this platform.
            if (rec.get("payout") or 0) > EPS:
                pe = next((p for p in payouts if p["txid"] == e["txid"]), None) \
                    or next((p for p in payouts if abs(p["amount"] - rec["payout"]) <= EPS
                             and abs(p["time"] - ref) <= window), None)
                res["payout_ok"] = pe is not None
                if pe is None:
                    res["notes"].append(f"payout {rec['payout']:g} has no matching Payout entry")
        results.append(res)

    # Reverse pass: casino wagers in the sweep window that matched NO record.
    refs = [t for t in (_parse_record_time(r) for r in records) if t]
    unexpected = []
    if refs:
        lo, hi = min(refs) - timedelta(minutes=5), max(refs) + window
        for i, e in enumerate(wagers):
            if i not in used and e["provider"] and lo <= e["time"] <= hi:
                unexpected.append({"time": e["time"].strftime("%Y-%m-%d %H:%M"),
                                   "provider": e["provider"], "amount": e["amount"],
                                   "running": e["running"], "txid": e["txid"]})
    return results, unexpected


# ─── site driving ─────────────────────────────────────────────────────────────
async def _goto(page, url, tries=3):
    """Navigation with retries — corp VPN links drop connections now and then."""
    for i in range(1, tries + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return True
        except Exception as e:
            print(f"  [TLOGS] nav retry {i}/{tries} ({e.__class__.__name__})")
            await asyncio.sleep(3 * i)
    return False


async def _clear_promo_overlays(page):
    """Rotating promo modals (2026-07-15: 'R300k every matchday') render on a full-viewport
    fixed z-50 backdrop that intercepts every click and keypress — Escape does not close it,
    and inputs stay fillable behind it, so login 'fills but never sticks'. Remove the
    backdrop node(s) outright; the site header itself is z-40 and unaffected."""
    try:
        n = await page.evaluate(
            """() => { const els = [...document.querySelectorAll('div[class*="fixed"][class*="z-50"]')];
                       els.forEach(e => e.remove()); return els.length; }""")
        if n:
            print(f"  [TLOGS] removed {n} promo overlay(s)")
    except Exception:
        pass


async def _login(page, pack, username, password, region, ss_dir=None):
    """Header login; Betway accepts a few username spellings — try each until the
    login inputs disappear (the success signal). On failure, screenshot each stuck
    attempt into ss_dir so the report shows WHY (error banner, captcha, changed UI)."""
    variants, seen = [], set()
    for v in (username, add_country_code(username, region), "0" + username.lstrip("0")):
        if v and v not in seen:
            variants.append(v); seen.add(v)
    for attempt, name in enumerate(variants, 1):
        if not await _goto(page, pack["site"]):
            continue
        try:
            await page.wait_for_selector(pack["user_sel"], timeout=15000)
        except Exception:
            await page.keyboard.press("Escape")   # cookie/promo overlay best-effort
            await page.wait_for_selector(pack["user_sel"], timeout=10000)
        await _clear_promo_overlays(page)
        await page.fill(pack["user_sel"], name)
        await page.fill(pack["pass_sel"], password)
        # The promo modal mounts on a DELAY (seconds after load), so it can appear between
        # overlay-clearing and the submit and intercept a real click or Enter. A DOM-level
        # click can't be intercepted by any overlay.
        try:
            await page.evaluate("sel => document.querySelector(sel).click()",
                                pack["login_btn_sel"])
        except Exception:
            await page.press(pack["pass_sel"], "Enter")   # button missing — old path
        try:
            await page.wait_for_selector(pack["user_sel"], state="hidden", timeout=20000)
            print(f"  [TLOGS] logged in as {name}")
            return True
        except Exception:
            print(f"  [TLOGS] login attempt {attempt} as '{name}' did not stick")
            if ss_dir:
                try:
                    os.makedirs(ss_dir, exist_ok=True)
                    shot = os.path.join(ss_dir, f"login_fail_{username}_a{attempt}.png")
                    await page.screenshot(path=shot)
                    print(f"  [TLOGS] evidence: {shot}")
                except Exception:
                    pass
    return False


async def _open_history(page, pack):
    """Direct URL first (the menu anchor's own href); hamburger route as fallback."""
    if not await _goto(page, pack["history_url"]):
        return False
    await _clear_promo_overlays(page)   # pagination/menu clicks are blocked the same way
    if await _rows_present(page):
        return True
    try:
        await page.click(pack["hamburger_sel"], timeout=8000)
        await page.click(pack["history_menu_sel"], timeout=8000)
    except Exception:
        pass
    return await _rows_present(page)


async def _rows_present(page, timeout=25000):
    try:
        await page.wait_for_function(
            "() => [...document.querySelectorAll('div[class*=\"grid-cols-\"]')]"
            ".some(r => /Wager|Payout|Deposit/.test(r.textContent))", timeout=timeout)
        return True
    except Exception:
        return False


async def scrape_history(page, pack, oldest_needed, max_pages=30):
    """Scrape entries newest-first, paginating (#transaction-history-next) until we're past
    the sweep window, the rows stop changing, or the cap. Dedupe on full row identity."""
    entries, seen = [], set()
    now = datetime.now()
    for pageno in range(1, max_pages + 1):
        raw = await page.evaluate(_ROW_JS)
        batch = [e for e in (parse_row(r, now) for r in raw) if e]
        fresh = 0
        for e in batch:
            key = (e["txid"], e["kind"], e["amount"], e["running"], e["time"].isoformat())
            if key not in seen:
                seen.add(key); entries.append(e); fresh += 1
        oldest = min((e["time"] for e in batch), default=None)
        print(f"  [TLOGS] page {pageno}: {len(batch)} rows ({fresh} new), oldest {oldest}")
        if not batch or (pageno > 1 and fresh == 0):
            break
        if oldest and oldest < oldest_needed:
            break
        nxt = await page.query_selector(pack["next_page_sel"])
        if nxt is None:
            print("  [TLOGS] no next-page control — end of history")
            break
        await nxt.click()
        await asyncio.sleep(2.0)
    return entries


# ─── evidence capture: highlight each matched row ON the site and screenshot it ──
_slug_re = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(s, maxlen=32):
    return (_slug_re.sub("-", str(s or "").strip()).strip("-") or "row")[:maxlen]


_HL_JS = """
(args) => {
  const rows = [...document.querySelectorAll('div[class*="grid-cols-"]')];
  const row = rows.find(r => {
    const c = [...r.children].filter(x => x.tagName === 'DIV');
    return c.length >= 4 && c[3].textContent.trim() === args.txid;
  });
  if (!row) return false;
  row.style.outline = '3px solid ' + args.color;
  row.style.outlineOffset = '2px';
  row.style.boxShadow = '0 0 0 6px ' + args.color + '33';
  row.style.position = 'relative';
  row.style.zIndex = '9998';
  const tag = document.createElement('div');
  tag.className = 'melon-tlogs-tag';
  tag.textContent = args.label;
  tag.style.cssText = 'position:absolute;top:-28px;left:0;background:' + args.color +
    ';color:#fff;font:700 13px sans-serif;padding:4px 12px;border-radius:6px;' +
    'z-index:9999;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.35)';
  row.appendChild(tag);
  row.scrollIntoView({block: 'center'});
  return true;
}
"""

_HL_CLEAR_JS = """
() => {
  document.querySelectorAll('.melon-tlogs-tag').forEach(t => t.remove());
  for (const row of document.querySelectorAll('div[class*="grid-cols-"]')) {
    row.style.outline = ''; row.style.outlineOffset = '';
    row.style.boxShadow = ''; row.style.zIndex = '';
  }
  document.querySelectorAll('.melon-tlogs-banner').forEach(b => b.remove());
}
"""

_BANNER_JS = """
(text) => {
  const b = document.createElement('div');
  b.className = 'melon-tlogs-banner';
  b.textContent = text;
  b.style.cssText = 'position:fixed;top:14px;left:50%;transform:translateX(-50%);' +
    'background:#dc2626;color:#fff;font:700 14px sans-serif;padding:10px 20px;' +
    'border-radius:10px;z-index:99999;box-shadow:0 4px 16px rgba(0,0,0,.4)';
  document.body.appendChild(b);
}
"""

_STATUS_COLOR = {"verified": "#059669", "verified_amount_time_only": "#d97706"}


async def _shoot(page, shots_dir, fname):
    path = os.path.join(shots_dir, fname)
    try:
        await page.screenshot(path=path)
        return fname
    except Exception as e:
        print(f"  [TLOGS] screenshot failed ({fname}): {e}")
        return None


async def capture_evidence(page, pack, results, unexpected, shots_dir, oldest_needed,
                           account, max_pages=30):
    """Walk the history pages again and, for every reconciled record, highlight its row
    on the LIVE site (colored outline + label with game/wager/status) and screenshot the
    page — account header, dates, running balance, txid all in frame. Missing records
    get a red banner shot of the page that covers their time window: proof of absence."""
    want = {r["txid"]: r for r in results if r.get("txid") and not r.get("shot")}
    missing = [r for r in results if r["status"] == "missing" and r.get("ref_time")]
    unex = {u["txid"]: u for u in unexpected if u.get("txid")}
    if not want and not missing and not unex:
        return
    if not await _open_history(page, pack):   # back to page 1 (scrape left us at the end)
        print("  [TLOGS] evidence pass: history did not re-render — no screenshots")
        return
    os.makedirs(shots_dir, exist_ok=True)
    shot_n = 0
    now = datetime.now()
    for pageno in range(1, max_pages + 1):
        raw = await page.evaluate(_ROW_JS)
        batch = [e for e in (parse_row(r, now) for r in raw) if e]
        page_tx = {e["txid"] for e in batch}

        for txid in [t for t in page_tx if t in want]:
            r = want.pop(txid)
            color = _STATUS_COLOR.get(r["status"], "#dc2626")
            amt = r.get("entry_amount") if r.get("entry_amount") is not None else r["wager"]
            label = f"{r['game']} · R {amt:g} · {r['status']} · {account}"
            if await page.evaluate(_HL_JS, {"txid": txid, "color": color, "label": label}):
                shot_n += 1
                r["shot"] = await _shoot(page, shots_dir,
                                         f"{shot_n:02d}_{_slug(r['game'])}_{r['status']}.png")
                if r["shot"]:
                    print(f"  [TLOGS] 📸 {r['game']}: row highlighted -> {r['shot']}")
            await page.evaluate(_HL_CLEAR_JS)

        for txid in [t for t in page_tx if t in unex]:
            u = unex.pop(txid)
            label = f"UNEXPECTED wager · {u['provider']} · R {u['amount']:g}"
            if await page.evaluate(_HL_JS, {"txid": txid, "color": "#dc2626", "label": label}):
                shot_n += 1
                u["shot"] = await _shoot(page, shots_dir, f"{shot_n:02d}_unexpected.png")
            await page.evaluate(_HL_CLEAR_JS)

        # Proof-of-absence: this page covers the missing record's expected time window.
        times = [e["time"] for e in batch]
        if times:
            lo, hi = min(times), max(times)
            for r in [m for m in missing if m.get("shot") is None]:
                try:
                    ref = datetime.strptime(r["ref_time"], "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    missing.remove(r)
                    continue
                if lo <= ref <= hi or (pageno == 1 and ref >= hi):
                    await page.evaluate(
                        _BANNER_JS,
                        f"MISSING: no wager of R {r['wager']:g} for {r['game']} "
                        f"around {ref:%H:%M} in this history")
                    shot_n += 1
                    r["shot"] = await _shoot(page, shots_dir,
                                             f"{shot_n:02d}_{_slug(r['game'])}_missing.png")
                    await page.evaluate(_HL_CLEAR_JS)
                    if r["shot"]:
                        print(f"  [TLOGS] 📸 {r['game']}: absence proof -> {r['shot']}")

        if not want and not unex and all(m.get("shot") for m in missing):
            break
        oldest = min(times, default=None)
        if not batch or (oldest and oldest < oldest_needed):
            break
        nxt = await page.query_selector(pack["next_page_sel"])
        if nxt is None:
            break
        await nxt.click()
        await asyncio.sleep(2.0)
    if want:
        print(f"  [TLOGS] evidence pass: {len(want)} matched row(s) not re-found for screenshots")


async def _install_api_proxy(page):
    """Route the site's whole API namespace through a curl_cffi (chrome110) session.
    Cloudflare 403s browser-originated /appsynapse/ calls from a datacentre/VPN IP — the
    token POST succeeds but every follow-up (userinfoextended, history, …) is blocked, so
    login never 'sticks'. curl_cffi clears the same WAF the sweep's auth already passes;
    proxying every /appsynapse/ request through it — forwarding the SPA's own headers, incl.
    the Authorization token — lets the real site run in the browser unchanged. Generic: no
    per-account/endpoint logic, just the transport swapped for the one that isn't blocked."""
    session = _creq.Session(impersonate="chrome110")

    async def _proxy(route):
        req = route.request
        try:
            hdrs = {k: v for k, v in (await req.all_headers()).items()
                    if k.lower() not in ("host", "content-length", "accept-encoding")}
            r = await asyncio.to_thread(
                lambda: session.request(req.method, req.url, headers=hdrs,
                                        data=req.post_data, timeout=25, verify=False))
            await route.fulfill(status=r.status_code,
                                headers={"content-type": r.headers.get("content-type",
                                                                       "application/json")},
                                body=r.content)
        except Exception:
            await route.continue_()   # let the browser try directly rather than hang the page

    await page.route("**/appsynapse/**", _proxy)


# ─── API-based transaction history (generic, no browser) ───────────────────────
# The transaction history is a plain JSON API on both brands, reached with the account's bearer
# token via curl_cffi (which clears the same Cloudflare WAF as the sweep's auth). This replaces
# the old per-site DOM scrape: no login form, no promo modals, no per-region selectors — one code
# path works for every Betway region AND JackpotCity. Endpoints/shapes verified 2026-07-17:
#   Betway: POST {origin}/appsynapse/universal/v1/Betting/GetTransactionHistory
#           -> {"transactions":[{transactionDate,type:"<Provider> Wager|Payout",amount(signed),
#                                totalAmount(running balance),betId}]}
#   JPC:    POST https://app.jpc.africa/balance/v2/Wallet/transactions
#           -> {"data":{"transactionLogs":[{createdDateTime,transactionTypeDescription,
#                                isCredit,amount(abs),internalReferenceId}]}}  (no running balance)
_JPC_HISTORY_URL = "https://app.jpc.africa/balance/v2/Wallet/transactions"


def _parse_api_time(s):
    """API timestamps are ISO-8601 with a tz offset (site's server tz). Convert to NAIVE LOCAL
    time so it lines up with the records' reference times (_parse_record_time does the same)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # over-long fractional seconds on some JPC rows — trim to microseconds
        base = re.sub(r"(\.\d{6})\d+", r"\1", s)
        try:
            dt = datetime.fromisoformat(base)
        except ValueError:
            return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _kind_provider(desc):
    """'Evolution Wager'/'Habanero Casino Games Payout' -> ('Wager'|'Payout', provider)."""
    d = (desc or "").strip()
    low = d.lower()
    kind = "Payout" if low.endswith("payout") else ("Wager" if low.endswith("wager") else d)
    provider = re.sub(r"\s*(Wager|Payout)$", "", d, flags=re.I)
    provider = re.sub(r"\s*Casino Games$", "", provider, flags=re.I).strip()
    return kind, provider


def _entries_from_betway(txns):
    out = []
    for t in txns:
        when = _parse_api_time(t.get("transactionDate"))
        amt = t.get("amount")
        if when is None or amt is None:
            continue
        kind, provider = _kind_provider(t.get("type"))
        out.append({"time": when, "kind": kind, "provider": provider,
                    "amount": float(amt), "running": t.get("totalAmount"),
                    "txid": (t.get("betId") or t.get("transactionLogId") or "").strip()})
    return out


def _entries_from_jpc(logs):
    out = []
    for t in logs:
        when = _parse_api_time(t.get("createdDateTime"))
        amt = t.get("amount")
        if when is None or amt is None:
            continue
        kind, provider = _kind_provider(t.get("transactionTypeDescription"))
        if kind not in ("Wager", "Payout"):   # fall back to the credit flag
            kind = "Payout" if t.get("isCredit") else "Wager"
        signed = abs(float(amt)) * (1 if t.get("isCredit") else -1)
        out.append({"time": when, "kind": kind, "provider": provider,
                    "amount": signed, "running": None,          # JPC exposes no running balance
                    "txid": (t.get("internalReferenceId") or "").strip()})
    return out


def fetch_history_api(brand, region, username, password, oldest_needed, max_pages=10):
    """Auth + fetch this account's transaction history via the brand's JSON API. Returns
    (entries, error): entries in the common reconcile shape, or (None, msg) on failure."""
    brand = (brand or "betway").lower()
    cfg = region_config(brand, region)
    if not cfg.get("configured"):
        return None, f"{brand}/{region} not configured (no site origin / casino endpoint)"
    auth = AuthHandler().authenticate(username, password, brand=brand, region=region)
    if not auth.get("success"):
        return None, f"auth failed: {str(auth.get('message'))[:120]}"
    token = auth["token"]
    hdrs = auth_headers(cfg)                       # carries x-brand-id when the region needs it
    hdrs["authorization"] = f"Bearer {token}"

    if brand == "jackpotcity":
        url, page_size = _JPC_HISTORY_URL, 50
    else:
        url = f"{cfg['origin']}/appsynapse/universal/v1/Betting/GetTransactionHistory"
        page_size = 25

    entries, seen = [], set()
    for page in range(1, max_pages + 1):
        payload = ({"pageNumber": page, "pageSize": page_size}
                   if brand == "jackpotcity"
                   else {"pageNumber": page, "pageSize": page_size, "transactionTypeSource": "All",
                         "sortBy": "Date", "fromTransactionDate": "", "toTransactionDate": "",
                         "referenceNumber": ""})
        try:
            r = _creq.Session(impersonate="chrome110").post(url, headers=hdrs, json=payload,
                                                            timeout=25, verify=False)
            data = r.json()
        except Exception as e:
            if entries:
                break                              # keep what we have
            return None, f"history request failed (HTTP): {str(e)[:100]}"
        if brand == "jackpotcity":
            rows = (data.get("data") or {}).get("transactionLogs") or []
            batch = _entries_from_jpc(rows)
        else:
            rows = data.get("transactions") or []
            batch = _entries_from_betway(rows)
        if not batch:
            break
        for e in batch:
            key = (e["txid"], e["kind"], e["amount"], e["time"].isoformat())
            if key not in seen:
                seen.add(key); entries.append(e)
        # newest-first: stop once this page reaches past the window we need
        if min((e["time"] for e in batch), default=datetime.now()) < oldest_needed:
            break
        if len(rows) < page_size:
            break
    return entries, None


def validate_account(brand, region, username, password, records, window_min):
    """Fetch this account's history via API and reconcile the recorded spins against it."""
    refs = [t for t in (_parse_record_time(r) for r in records) if t]
    oldest_needed = (min(refs) if refs else datetime.now()) - timedelta(minutes=10)
    entries, err = fetch_history_api(brand, region, username, password, oldest_needed)
    if err:
        return None, None, err
    print(f"  [TLOGS] {username}: fetched {len(entries)} history entries via API")
    results, unexpected = reconcile(records, entries, window_min=window_min)
    return results, unexpected, None


# ─── main ─────────────────────────────────────────────────────────────────────
def _load_records(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_passwords():
    try:
        with open(os.path.join(BASE_DIR, "accounts.json"), encoding="utf-8") as f:
            return {a["username"]: a["password"] for a in json.load(f) if a.get("username")}
    except Exception:
        return {}


async def amain(args):
    records = _load_records(args.records)
    if not records:
        raise SystemExit(f"[TLOGS] no records in {args.records}")
    passwords = _load_passwords()

    by_account = {}
    for r in records:
        by_account.setdefault(r.get("account") or "?", []).append(r)
    print(f"[TLOGS] {len(records)} record(s) across {len(by_account)} account(s) "
          f"from {os.path.basename(args.records)}")

    shots_dir = args.records.replace("_records.jsonl", "_tlogs")   # kept for payload schema
    all_results, all_unexpected, account_errors = [], [], {}
    for account, recs in by_account.items():
        brand = recs[0].get("brand") or args.brand
        region = recs[0].get("region") or args.region
        print(f"\n[TLOGS] ── account {account} ({brand}/{region}) · {len(recs)} record(s)")
        pwd = passwords.get(account)
        if not pwd:
            account_errors[account] = "no password in accounts.json"
            print(f"  [TLOGS] ❌ {account_errors[account]}")
            continue
        try:
            results, unexpected, err = validate_account(
                brand, region, account, pwd, recs, args.window_min)
        except Exception as e:
            results, unexpected, err = None, None, f"history fetch crashed: {e}"
        if err:
            account_errors[account] = err
            print(f"  [TLOGS] ❌ {err}")
            continue
        for res in results:
            icon = {"verified": "✅", "absent_ok": "▫️"}.get(res["status"], "❌")
            print(f"  {icon} {res['game']}: {res['status']}"
                  + (f" · txid {res['txid']}" if res["txid"] else "")
                  + (f" · {'; '.join(res['notes'])}" if res["notes"] else ""))
        all_results.extend(results)
        all_unexpected.extend(unexpected)

    summary = {
        "records": len(records),
        "verified": sum(1 for r in all_results if r["status"] == "verified"),
        "verified_weak": sum(1 for r in all_results if r["status"] == "verified_amount_time_only"),
        "missing": sum(1 for r in all_results if r["status"] == "missing"),
        "absent_ok": sum(1 for r in all_results if r["status"] == "absent_ok"),
        "violations": sum(1 for r in all_results if r["status"].startswith("VIOLATION")),
        "unmatchable": sum(1 for r in all_results if r["status"] == "unmatchable"),
        "unexpected_wagers": len(all_unexpected),
        "account_errors": len(account_errors),
    }
    payload = {"records_file": os.path.basename(args.records),
               "generated_at": datetime.now().astimezone().isoformat(),
               "window_min": args.window_min, "summary": summary,
               "shots_dir": os.path.basename(shots_dir) if os.path.isdir(shots_dir) else None,
               "results": all_results, "unexpected": all_unexpected,
               "account_errors": account_errors}

    # ── Finalize the Excel: the sweep leaves Tlogs "Pending" — the transaction-history
    # verdict is what fills it. This is where the deliverable report is born.
    report_path = args.records.replace("_records.jsonl", ".xlsx")
    if os.path.exists(report_path):
        from modules import dsc_report
        VERDICT = {"verified": "Pass", "verified_amount_time_only": "Pass",
                   "missing": "Fail", "VIOLATION_unrecorded_bet": "Fail",
                   "absent_ok": "NA"}   # unmatchable stays Pending — nothing to judge
        n_up = 0
        for res in all_results:
            verdict = VERDICT.get(res["status"])
            if not verdict:
                continue
            note = "; ".join(res["notes"]) if res["notes"] else \
                (f"verified in transaction history ({res['txid']})" if verdict == "Pass" else None)
            try:
                dsc_report.update_result(report_path, res["game"], res.get("srNo"),
                                         {"Tlogs": verdict}, append_error=note)
                n_up += 1
            except Exception as e:
                print(f"  [TLOGS] ⚠️ Excel row update failed for {res['game']}: {e}")
        payload["report"] = os.path.basename(report_path)
        print(f"[TLOGS] Excel finalized ({n_up} Tlogs verdicts) -> {report_path}")
    else:
        print(f"[TLOGS] no report sheet next to the records ({report_path}) — Excel skipped")

    out_path = args.records.replace("_records.jsonl", "_validation.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\n[TLOGS] Validation written -> {out_path}")
    print(f"[TLOGS] Summary: {summary['verified']} verified · {summary['verified_weak']} weak · "
          f"{summary['missing']} missing · {summary['unexpected_wagers']} unexpected · "
          f"{summary['violations']} violations")
    print("VALIDATIONPAYLOAD===")
    print(json.dumps(payload))
    print("===VALIDATIONPAYLOAD")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True, help="runs/DSC_Report_*_records.jsonl")
    ap.add_argument("--brand", default="betway")
    ap.add_argument("--region", default="ZA")
    ap.add_argument("--headed", action="store_true", help="watch the browser")
    ap.add_argument("--window-min", type=float, default=8.0,
                    help="time tolerance (minutes) between recorded and history time")
    asyncio.run(amain(ap.parse_args()))
