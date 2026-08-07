"""
slot_spin.py — PROVIDER-INDEPENDENT spin execution, completion detection & results.
================================================================================
Works on any slot game from any provider. It never assumes a specific endpoint or
JSON shape. Two layers:

  UNIVERSAL (always works, any provider)
    - Completion: after the spin click we confirm (a) a non-idle network request
      fired AND/OR (b) the reels actually moved (frame motion), then wait for the
      UI to settle (frame motion < threshold for N frames, win-aware hard cap).
    - Result: read balance/bet via vision (Gemini) before & after; win and payout
      are derived from the balance delta.

  ENHANCEMENT (used automatically when the response is parseable)
    - If the spin response carries recognizable money fields (…InCents, balance,
      win, payout, totalWin, …) we use those EXACT values instead of OCR.

Completion signals, motion, and vision all generalize across providers; only the
optional exact-value parse is format-aware, and it degrades gracefully to vision.
"""
import os
import re
import json
import time
import asyncio
from urllib.parse import urlparse
from PIL import Image, ImageChops

# Reuse the existing vision + noise helpers (provider-agnostic).
from test_spin_button import read_game_values, parse_amount, NOISE_PATTERNS
import config_env


# ─── Visual motion (universal completion signal) ──────────────────
def frame_motion(a_path: str, b_path: str) -> float:
    """Mean absolute grayscale difference (0-255) between two downscaled frames."""
    a = Image.open(a_path).convert("L").resize((160, 320))
    b = Image.open(b_path).convert("L").resize((160, 320))
    d = ImageChops.difference(a, b)
    px = d.get_flattened_data() if hasattr(d, "get_flattened_data") else list(d.getdata())
    return sum(px) / len(px)


def _is_noise(url: str) -> bool:
    return any(p in url.lower() for p in NOISE_PATTERNS)


def _path(url: str) -> str:
    return urlparse(url).path


# ─── Unified game monitor (provider-agnostic spin detection) ──────
class UnifiedGameMonitor:
    """
    THE single network monitor for the framework — merges the former NetworkMonitor
    (test_spin_button) and SpinNetMonitor (slot_spin) into one robust class.

    It captures every fetch / xhr / WebSocket request+response into a SINGLE backing store and
    exposes BOTH historical interfaces over it, so every call site uses one object:
      • generic spin detection  — learn_idle(), req_count(), spin_request_since(), response_for()
      • calibrated counting      — discover_spin_endpoint(), start/stop_monitoring(),
                                    spin_count, clear_spins(), wait_for_spin_completion()

    Each request dict carries both `t` and `time` (same value) plus path/url/method/post_data, and
    each response carries t/time/path/url/status/body, so legacy readers of either schema work.
    Attribute aliases (`requests`==`_all_requests`, `idle_paths`==`idle_post_paths`) point at the
    same objects. WebSocket frames use a consistent `@ws` path suffix.
    """
    def __init__(self):
        # one backing store, exposed under both historical names
        self.requests = self._all_requests = []
        self.responses = self._all_responses = []
        self.idle_paths = self.idle_post_paths = set()
        self.spin_path = None        # SpinNetMonitor name
        self.spin_endpoint = None    # NetworkMonitor name (kept in sync)
        self.spin_requests = []
        self._monitoring = False

    # ── capture ──────────────────────────────────────────────────
    def attach(self, page):
        def on_request(req):
            if req.resource_type not in ("fetch", "xhr"):
                return
            now = time.time()
            entry = {"t": now, "time": now, "path": _path(req.url), "url": req.url,
                     "method": req.method,
                     "post_data": (req.post_data[:500] if getattr(req, "post_data", None) else None)}
            self.requests.append(entry)
            self._note_spin(entry)

        async def on_response(resp):
            try:
                if resp.request.resource_type in ("fetch", "xhr"):
                    body = await resp.text()
                    now = time.time()
                    self.responses.append({"t": now, "time": now, "path": _path(resp.url),
                                           "url": resp.url, "status": resp.status, "body": body})
            except Exception:
                pass

        def on_ws(ws):
            def on_sent(payload):
                now = time.time(); path = _path(ws.url) + "@ws"
                entry = {"t": now, "time": now, "path": path, "url": ws.url,
                         "method": "WS_SEND", "post_data": str(payload)[:500]}
                self.requests.append(entry)
                self._note_spin(entry)
            def on_recv(payload):
                now = time.time(); path = _path(ws.url) + "@ws"
                self.responses.append({"t": now, "time": now, "path": path, "url": ws.url,
                                       "status": 200, "body": str(payload)})
            ws.on("framesent", on_sent)
            ws.on("framereceived", on_recv)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", on_ws)

    def _note_spin(self, entry):
        if self._monitoring and self.spin_endpoint and entry["path"] == self.spin_endpoint:
            self.spin_requests.append(entry)
            print(f"    [NET] >> Spin request intercepted! (total: {len(self.spin_requests)})")

    # ── idle baseline + generic spin detection (SpinNetMonitor API) ──
    async def learn_idle(self, duration=6):
        start = len(self.requests)
        await asyncio.sleep(duration)
        for r in self.requests[start:]:
            if r["method"] in ("GET", "POST", "WS_SEND") and not _is_noise(r["url"]):
                self.idle_paths.add(r["path"])
        print(f"  [IDLE] Background paths: {self.idle_paths or '{none}'}")

    def req_count(self):
        return len(self.requests)

    def spin_request_since(self, idx, any_method=False, allow_idle=False):
        """First non-idle, non-noise request after idx (the spin call). A real wager is a
        POST or a WebSocket send; the incidental GETs a click can trigger (promo/campaign
        fetches, assets) only count when any_method=True — a GET mistaken for the spin
        poisons both the endpoint and the result (2026-07-09 Thor's Rage: a Spin Gifts
        campaign popup's XHR was reported as a completed spin).
        allow_idle: some platforms multiplex EVERYTHING through one endpoint that also
        polls while idle (Bitville/3 Oaks '/process/' — 2026-07-10: two real 3 China Pots
        wagers were invisible because the spin path was idle-blacklisted). Callers enable
        this as a fallback only after the strict pass found nothing."""
        for r in self.requests[idx:]:
            if _is_noise(r["url"]):
                continue
            if r["path"] in self.idle_paths and not allow_idle:
                continue
            if r["method"] in ("POST", "WS_SEND") or any_method:
                self.spin_path = self.spin_endpoint = r["path"]
                return r
        return None

    def response_for(self, path, after_t):
        """Latest response on `path` after a timestamp (the spin result body)."""
        cand = [r for r in self.responses if r["path"] == path and r["t"] >= after_t]
        return cand[-1] if cand else None

    def get_new_posts_since(self, start_idx):
        return [r for r in self.requests[start_idx:]
                if r["method"] in ("GET", "POST", "WS_SEND") and not _is_noise(r["url"])]

    # ── calibrated counting (NetworkMonitor API) ──────────────────
    async def discover_spin_endpoint(self, page, spin_center):
        """Unified WS/XHR spin-endpoint discovery: click spin, take the first new non-idle path."""
        before = len(self.requests)
        print(f"  [CALIBRATE] Clicking spin at {spin_center}...")
        await page.mouse.click(*spin_center)
        await asyncio.sleep(8)
        candidates = [r for r in self.get_new_posts_since(before) if r["path"] not in self.idle_paths]
        # A real slot spin is a POST (carries bet data) or a WebSocket send — prefer those over the
        # incidental GETs a spin click fires (sounds, sprite/asset fetches). Only fall back to a GET
        # if nothing better appeared.
        pick = next((r for r in candidates if r["method"] in ("POST", "WS_SEND")), None) \
            or next((r for r in candidates if r.get("post_data")), None) \
            or (candidates[0] if candidates else None)
        if pick:
            self.spin_endpoint = self.spin_path = pick["path"]
        print(f"  [CALIBRATE] Discovered endpoint: {self.spin_endpoint} "
              f"({pick['method'] if pick else 'none'})")
        return self.spin_endpoint

    def start_monitoring(self):
        self.spin_requests.clear()
        self._monitoring = True

    def stop_monitoring(self):
        self._monitoring = False

    @property
    def spin_count(self):
        return len(self.spin_requests)

    def clear_spins(self):
        self.spin_requests.clear()

    async def wait_for_spin_completion(self, timeout=20):
        start = time.time()
        while time.time() - start < timeout:
            resp_count = sum(1 for r in self.responses if r["path"] == self.spin_endpoint)
            if len(self.spin_requests) > 0 and resp_count >= len(self.spin_requests):
                await asyncio.sleep(1.5)
                return True
            await asyncio.sleep(0.5)
        return False


# Backward-compatible alias (deprecated): the two former monitors are now one class.
SpinNetMonitor = UnifiedGameMonitor


# ─── Exact-value parse (enhancement; degrades to None) ────────────
def parse_result_body(body: str) -> dict:
    """
    PROVIDER-AGNOSTIC extraction of wager/payout/balance/feature from ANY spin
    response. It does NOT know any provider or field name — it deep-scans every
    numeric field and matches by MEANING (substring of the key), normalizing any
    field whose name implies cents (…InCents) by /100. Returns None for anything
    not confidently found, so the caller can fall back to vision.
    """
    out = {"wager": None, "payout": None, "balance": None, "win": None,
           "feature": False, "feature_name": None, "found": {},
           # Reconciliation keys for the transaction-history check (phase 2): the ids and
           # exact wallet accounting the back office can be matched against.
           "round_id": None, "tnum": None, "server_time": None,
           "balance_at_start": None, "balance_after_bet": None, "balance_at_end": None}
    if not body:
        return out

    # Wire ids + server time. Providers expose these as JSON keys (RagingRiver:
    # result.transactions.roundId, serverTime) OR as XML attributes inside a data string
    # (Bugatti: <Response time="..." tnum="...">) — regex the RAW body so both shapes work.
    m = re.search(r'"(?:roundId|round_id|transactionId|transaction_id|txId|betId)"\s*:\s*"?([\w-]+)"?', body)
    if m:
        out["round_id"] = m.group(1)
    # NB: Bugatti's XML rides INSIDE a JSON string, so its quotes arrive escaped (tnum=\"108\")
    # — the \\? makes both the raw and the JSON-escaped form match.
    m = re.search(r'\btnum=\\?"(\d+)\\?"', body)
    if m:
        out["tnum"] = m.group(1)
    m = re.search(r'"serverTime"\s*:\s*"([^"]+)"', body) \
        or re.search(r'<Response time=\\?"([^"\\]+)\\?"', body)
    if m:
        out["server_time"] = m.group(1)

    # RagingRiver-style exact wallet accounting: balance.cash.{atStart,afterBet,atEnd} —
    # STRING-valued, so the numeric deep-scan below can't see it. afterBet is precisely the
    # running balance the transaction history prints in parentheses next to the wager.
    m = re.search(r'"cash"\s*:\s*\{([^{}]*)\}', body)
    if m:
        blk = m.group(1)
        for key, field in (("atStart", "balance_at_start"), ("afterBet", "balance_after_bet"),
                           ("atEnd", "balance_at_end")):
            km = re.search(r'"%s"\s*:\s*"?(-?[\d.]+)"?' % key, blk)
            if km:
                try:
                    out[field] = float(km.group(1))
                except ValueError:
                    pass

    try:
        d = json.loads(body)
    except Exception:
        d = None

    if d is not None:
        items = []  # (key_lower, key, value) in document order

        def rec(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        items.append((k.lower(), k, v))
                    rec(v)
            elif isinstance(x, list):
                for v in x:
                    rec(v)
        rec(d)
        out["found"] = {k: v for _, k, v in items}

        def pick(includes, excludes=()):
            for klow, _k, v in items:
                if any(i in klow for i in includes) and not any(e in klow for e in excludes):
                    return v / 100 if "cent" in klow else v   # cents implied by the key name
            return None

        # "bet" is included for fields like betAmount/betValue, but excluded for the many
        # bet-* fields that are NOT a money amount (betLevel, betLine, betThresholdTime, ...).
        out["wager"] = pick(("wager", "stake", "totalbet", "bet"),
                            excludes=("level", "index", "line", "way", "count", "between",
                                      "button", "id", "time", "threshold"))
        out["payout"] = pick(("payout", "totalwin", "winamount", "wonamount"))
        if out["payout"] is None:
            out["payout"] = pick(("win",), excludes=("window", "winning", "lines", "winid"))
        out["balance"] = pick(("balance", "cashbalance", "cash", "wallet"), excludes=("cashout",))

    # Derive money from the exact wallet accounting when the generic pick found nothing
    # (RagingRiver reports every amount as strings, invisible to the numeric scan).
    if out["wager"] is None and out["balance_at_start"] is not None \
            and out["balance_after_bet"] is not None:
        delta = round(out["balance_at_start"] - out["balance_after_bet"], 2)
        if delta > 0:
            out["wager"] = delta
    if out["payout"] is None and out["balance_at_end"] is not None \
            and out["balance_after_bet"] is not None:
        out["payout"] = round(out["balance_at_end"] - out["balance_after_bet"], 2)
    if out["balance"] is None and out["balance_at_end"] is not None:
        out["balance"] = out["balance_at_end"]
    if out["payout"] is not None:
        out["win"] = out["payout"] > 0

    # Feature / free-spin / bonus detection (semantic, works for XML-in-JSON or flat).
    m = re.search(r'(FreeGame|FreeSpins?|[A-Za-z]*Bonus[A-Za-z]*|PickFeature|Respin)', body, re.I)
    if m and m.group(1).lower() not in ("bonusbuydisabled",):
        out["feature_name"] = m.group(1)
        out["feature"] = True
    return out


def descale_cents(net: dict, bet_screen, bal_screen) -> bool:
    """Some providers report money as PLAIN integers that are actually cents (Bitville,
    2026-07-13 3 China Pots: bet=50, win=70, balance=37944 for an R0.50 spin at R380 —
    no 'Cents' in any key name, so parse_result_body can't know). Cross-check the parsed
    values against the ON-SCREEN bet/balance: if either is ~100x its screen counterpart,
    scale every parsed money field down. Returns True when scaling was applied."""
    w, b = net.get("wager"), net.get("balance")
    wager_100x = (w is not None and bet_screen and bet_screen > 0
                  and abs(w - 100 * bet_screen) <= max(1.0, 100 * bet_screen * 0.02))
    bal_100x = (b is not None and bal_screen and bal_screen > 0
                and abs(b - 100 * bal_screen) <= 100 * bal_screen * 0.05)
    if not (wager_100x or bal_100x):
        return False
    for k in ("wager", "payout", "balance"):
        if net.get(k) is not None:
            net[k] = round(net[k] / 100.0, 2)
    return True


def withholding_tax(payout, wager, region: str) -> float:
    """15% on (payout - wager) for Mozambique (all) / Zambia (virtual)."""
    if region in ("MZ", "ZM") and payout and wager and payout > wager:
        return round((payout - wager) * 0.15, 2)
    return 0.0


# ─── The universal spin ───────────────────────────────────────────
def network_shows_movement(net, bal_before, bal_moved, reels_moved):
    """THE money-movement invariant (2026-07-13, 3 China Pots false Pass): a parseable
    response verifies a spin ONLY when money verifiably moved. The game's own state/
    heartbeat POST answers on the same endpoint with a balance echo — fields parse, but
    balance is UNCHANGED, no wager, no payout, reels still: that is not a transaction.
    Accept as network truth only with movement evidence: reels moved, the visual balance
    moved, or the response balance differs from the pre-spin balance."""
    if not any(net.get(k) is not None for k in ("wager", "balance", "payout")):
        return False
    net_moved = net.get("balance") is not None and bal_before is not None \
        and abs(net["balance"] - bal_before) > 0.001
    return bool(reels_moved or bal_moved or net_moved)


async def spin_and_measure(page, spin_center, monitor: UnifiedGameMonitor, ss_dir, tag="spin",
                           region="ZA", use_network=True,
                           settle_thresh=3.0, settle_need=2, hard_cap=12.0, result_timeout=8.0):
    """
    Provider-independent spin. Returns a report with:
      spin_fired (bool), reels_moved (bool), result_time, settle_time,
      values {wager,payout,balance_before,balance_after,win,feature,source,confidence},
      tax, shots {pre,spinning,result}.
    """
    os.makedirs(ss_dir, exist_ok=True)
    sp = lambda n: os.path.join(ss_dir, f"{tag}_{n}.png")
    rep = {"shots": {}, "spin_fired": False, "reels_moved": False,
           "result_time": None, "settle_time": None, "values": {}, "tax": 0.0}

    # ── pre: visual baseline (universal) ──
    await page.screenshot(path=sp("pre")); rep["shots"]["pre"] = sp("pre")
    pre_vals = read_game_values(Image.open(sp("pre")))
    bal_before = parse_amount(pre_vals.get("balance", ""))
    bet = parse_amount(pre_vals.get("bet", ""))

    # Pre-flight: don't spin if funds can't cover the bet (avoids phantom results).
    if bet is not None and bal_before is not None and bal_before + 0.001 < bet:
        rep["values"] = {"status": "insufficient_funds", "source": "visual", "confidence": "high",
                         "wager": bet, "balance_before": bal_before, "balance_after": bal_before,
                         "payout": 0.0, "win": False, "feature": False, "feature_name": None}
        rep["shots"]["result"] = rep["shots"]["pre"]
        return rep

    # Pre-flight: HARD stake cap. This is the last gate before the click for every caller —
    # whatever upstream flow (mis)set the stake, a spin above config_env.MAX_STAKE never fires.
    if bet is not None and bet > config_env.MAX_STAKE:
        print(f"    [CAP] Stake {bet:g} exceeds the safety cap {config_env.MAX_STAKE:g} — spin refused")
        rep["values"] = {"status": "stake_cap", "source": "visual", "confidence": "high",
                         "wager": bet, "balance_before": bal_before, "balance_after": bal_before,
                         "payout": 0.0, "win": False, "feature": False, "feature_name": None}
        rep["shots"]["result"] = rep["shots"]["pre"]
        return rep

    req_idx = monitor.req_count()
    t0 = time.time()
    await page.mouse.click(*spin_center)

    # ── confirm the spin actually started (network OR motion) ──
    spin_req = None
    await page.screenshot(path=sp("spinning")); rep["shots"]["spinning"] = sp("spinning")
    loops = int(result_timeout / 0.2)
    for i in range(loops):
        # Strict first (POST/WS only, non-idle paths). Halfway through, also accept
        # POST/WS on idle-learned paths (single-endpoint platforms multiplex the spin
        # over their idle poller's path). Any method only in the last quarter.
        spin_req = monitor.spin_request_since(req_idx,
                                              any_method=(i >= int(loops * 0.75)),
                                              allow_idle=(i >= int(loops * 0.5)))
        if spin_req:
            rep["spin_fired"] = True
            break
        await asyncio.sleep(0.2)
    # motion check confirms reels moved even if network is opaque (e.g. WS-only)
    m_spin = frame_motion(sp("pre"), sp("spinning"))
    rep["reels_moved"] = m_spin > settle_thresh
    rep["result_time"] = round(time.time() - t0, 2)

    # ── motion-settle (universal), win-aware cap ──
    # When the NETWORK already gave the result, we know wager/payout/balance — we don't need to wait
    # out a long win animation, just enough to keep the UI sane for the next action. Cap tightly in
    # that case; fall back to the full win-aware cap only for opaque (visual-only) games.
    cap = 4.0 if (use_network and rep["spin_fired"]) else hard_cap
    prev = None; low = 0; t = 0.0; max_mv = 0.0
    while t < cap:
        fp = sp(f"f{t:04.1f}"); await page.screenshot(path=fp)
        mv = frame_motion(prev, fp) if prev else 99.0
        if prev:
            max_mv = max(max_mv, mv)
        low = low + 1 if mv < settle_thresh else 0
        prev = fp
        if low >= settle_need:
            break
        await asyncio.sleep(0.4); t = round(t + 0.4, 1)
    rep["settle_time"] = t
    # Reels that start AFTER the immediate post-click frame (3 China Pots: click→server
    # round-trip first, reels later) are invisible to the pre-vs-spinning check — the
    # settle frames carry the motion instead.
    if not rep["reels_moved"] and max_mv > settle_thresh:
        rep["reels_moved"] = True

    # ── post: visual read (universal) ──
    await page.screenshot(path=sp("result")); rep["shots"]["result"] = sp("result")
    post_vals = read_game_values(Image.open(sp("result")))
    bal_after = parse_amount(post_vals.get("balance", ""))

    # ── result reconciliation: network exact > vision ──
    net = {}
    if use_network and spin_req:
        resp = monitor.response_for(spin_req["path"], t0)
        if resp:
            net = parse_result_body(resp["body"])
            if descale_cents(net, bet, bal_before):
                print(f"    [CENTS] provider reports plain-integer cents — money fields scaled /100 "
                      f"(wager {net.get('wager')}, balance {net.get('balance')})")
            # Impossible-wager guard: you cannot stake more than the balance you had going in.
            # A parsed wager above the pre-spin balance means the money fields are in the wrong
            # unit or the wrong field was read (Foody Drive 2026-07-16: 'wager 420' on a R1.59
            # balance for a real R1.00 spin, which faked a >50-cap SAFETY BREACH). Descaling
            # runs first; if the wager is STILL impossible, drop the whole network money parse
            # and let visual/balance-delta below carry the result.
            if net.get("wager") is not None and bal_before is not None \
                    and net["wager"] > bal_before + 0.011:
                print(f"    [NET] parsed wager {net['wager']:g} exceeds pre-spin balance "
                      f"{bal_before:g} — impossible; discarding network money parse")
                for k in ("wager", "payout", "balance"):
                    net.pop(k, None)

    # A post-spin balance read of EXACTLY 0 on an account that had funds is a failed read,
    # not a bust (Bison Prime 2026-07-16: OCR returned 0.00 while the frame plainly showed
    # 371.42, inventing a R372 'wager' and a false safety breach). Treat it as unknown
    # unless the wire itself carries a balance. A true bust-to-zero still surfaces via the
    # network balance or the transaction-history validation.
    if bal_after is not None and bal_after == 0.0 and (bal_before or 0) > 0.01 \
            and net.get("balance") is None:
        print("    [READ] balance_after read 0.00 on a funded account — treating as unreadable")
        bal_after = None

    v = {"source": "visual", "confidence": "low", "status": "ok",
         "wager": bet, "balance_before": bal_before, "balance_after": bal_after,
         "payout": None, "win": None, "feature": False, "feature_name": None}

    # Reconciliation extras (transaction-history validation): pass through whatever the wire
    # exposed, independent of whether the money parse below succeeds.
    for k in ("round_id", "tnum", "server_time",
              "balance_at_start", "balance_after_bet", "balance_at_end"):
        if net.get(k) is not None:
            v[k] = net[k]

    spin_happened = rep["spin_fired"] or rep["reels_moved"]

    bal_moved = bal_before is not None and bal_after is not None \
        and abs(bal_after - bal_before) > 0.001

    accept_net = network_shows_movement(net, bal_before, bal_moved, rep["reels_moved"])
    if any(net.get(k) is not None for k in ("wager", "balance", "payout")) and not accept_net:
        print("    [NET] response parsed but shows NO money movement (balance unchanged, "
              "reels still) — rejected: a state/heartbeat echo is not a spin")

    if accept_net:
        v["source"] = "network"; v["confidence"] = "high"
        if net.get("wager") is not None:   v["wager"] = net["wager"]
        if net.get("payout") is not None:  v["payout"] = net["payout"]; v["win"] = net["payout"] > 0
        if net.get("balance") is not None: v["balance_after"] = net["balance"]
        v["feature"] = net.get("feature", False); v["feature_name"] = net.get("feature_name")
    elif bal_moved and bet is not None:
        # MONEY MOVED — the spin executed even when both action signals were dark
        # (2026-07-10 3 China Pots: spin POST hidden on an idle-multiplexed path AND reels
        # started after the post-click frame; two real wagers were reported "no_spin").
        # Visual derivation: the bet was deducted; payout is the net change vs "lost the bet".
        delta = round(bal_after - (bal_before - bet), 2)
        v["payout"] = max(0.0, delta)
        v["win"] = delta > 0.0
        v["confidence"] = "medium" if spin_happened else "low"
    elif not spin_happened:
        # No spin request fired, reels never moved, and the balance didn't move ->
        # the spin didn't happen (disabled button / blocked).
        v["status"] = "no_spin"; v["payout"] = None; v["win"] = False
    elif bal_before is not None and bal_after is not None:
        # Unchanged balance with nothing parseable on the wire is the NO-SPIN signature:
        # the delta formula above would report payout == wager (a phantom "win") for a
        # click that did nothing. Report unverified instead of inventing a result.
        v["status"] = "unverified"; v["payout"] = None; v["win"] = None

    rep["values"] = v
    rep["tax"] = withholding_tax(v.get("payout"), v.get("wager"), region)
    return rep
