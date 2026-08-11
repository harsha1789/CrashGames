"""
crash_auto.py — Crash-game UI test suite (FlyX Party / Aviator-style games)
===========================================================================
Sibling of test_spin_button.py for the CRASH vertical. Crash games are a
different paradigm from slots:
  - Place a bet during a BETTING window (before the round starts).
  - A multiplier ASCENDS from 1.00x along a curve.
  - You must CASH OUT before it CRASHES; payout = bet x multiplier at cash-out.
  - Often two parallel bet panels.

Built NATIVELY on the dsc-auto-sweep framework — it reuses, rather than
re-implements, the slot stack:
  - config_env          : live VIEWPORT_* + coordinate clamping + MAX_STAKE cap
  - test_spin_button (T): Gemini client/helpers, TestResult, _ss, annotate_controls,
                          finalize_media, _emit_report (identical report contract)
  - slot_spin           : UnifiedGameMonitor (fetch/xhr/WS capture), frame_motion

Recon (FlyX Party / Games Global MGS): round state streams over a SignalR
websocket "pushhub" (wss://api3.gameassists.co.uk/shared/push/v1/signalr),
topic "vpb.message". UnifiedGameMonitor already captures WS frames, so the
round feed is available via monitor.responses; parse_round_frame() maps it once
a live payload is captured.

SAFETY
------
SAFE BY DEFAULT (--dry-run): observes the game and validates detection / phase
timing WITHOUT placing any bet. Wager-dependent tests are SKIPPED in dry-run and
only run under --live. Any live wager is additionally gated by config_env.MAX_STAKE
(the team's hard betting-safety cap) — the same gate slot_spin enforces.

Usage:
  python crash_auto.py "<launch-url>" --dry-run --run-dir runs/<id>
  python crash_auto.py "<launch-url>" --live --bet 1.00 --target 1.5   # real wagers
"""
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import os
import json
import time
import asyncio
from datetime import datetime

from PIL import Image
from google.genai import types
from playwright.async_api import async_playwright

import config_env
import test_spin_button as T          # Gemini helpers, TestResult, _ss, report plumbing
import slot_spin                      # UnifiedGameMonitor, frame_motion
from modules.utils import region_config

# Round-state channel confirmed during recon (SignalR pushhub topic).
ROUND_STATE_TOPIC = "vpb.message"

# Phases in which the game is live/interactive (vs. still loading or disconnected).
ROUND_READY_PHASES = ("BETTING", "ASCENDING", "CRASHED", "RESULT")


def _cfg():
    """Shared Gemini JSON config (mirrors slot_explore._cfg)."""
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def _center(box):
    """Gemini box [ymin,xmin,ymax,xmax] (0-1000) -> clamped CSS pixel center (config_env)."""
    return config_env.norm_box_center(box)


# ═══════════════════════════════════════════════════════════════════
#   Crash-specific Gemini vision
# ═══════════════════════════════════════════════════════════════════
def detect_crash_controls(image: Image.Image) -> list:
    """Detect crash-game UI controls. Returns [{label, box_2d, center}].

    v2 prompt (2026-07-30): broadened from a closed literal-wording vocabulary to a
    FUNCTIONAL description after a 15-provider validation sweep
    (runs/provider_validation_dryrun/) found the v1 wording ("Bet Button (BET / Place Bet)",
    "Cash Out Button") missed real controls on providers that don't use that exact text —
    e.g. Playtech-Live's "Big Bad Wolf Crash & Crumble" has its action button in a small
    control strip beside a live-dealer video feed, never labeled "BET"/"CASH OUT" verbatim,
    so v1 found peripheral controls (inputs, balance, history) but never the action button
    itself and TEST 1 failed. v1 kept verbatim in crash_prompt_v1_backup.md."""
    api_img = image.copy()
    api_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
    prompt = """Analyze this "crash" style casino game screenshot (a multiplier rises from 1.00x and
can crash at any time — Aviator, JetX, Spaceman, and similar games all share this mechanic, but
provider skins vary widely: some use a plane/rocket, some a car/animal/character race, some are a
live-dealer feed with a small control strip instead of a full-screen canvas). Detect ALL
interactive UI controls.

RULES:
- ONLY detect clickable buttons/inputs and the key value displays (NOT decorations, NOT the
  video/animation itself).
- Identify controls by FUNCTION, not exact wording — the same control is worded differently
  across providers (a wager button might say BET, PLAY, WAGER, GO, ENTER, or show no text at
  all, just a colored icon; a cash-out button might say CASH OUT, COLLECT, TAKE WIN, STOP, or
  also be icon-only). Always normalize to the canonical label below even if the on-screen text
  differs — do not invent new labels outside this list.
- If a splash/overlay is blocking the real game UI (a "Tap to Play"/"Click to continue" prompt,
  a cookie/age-consent banner, an autoplay-unlock message) report ONLY "Continue/Start Gate" for
  its clickable element — this is a different situation from the real in-game controls below and
  should not be confused with a Bet Button.

Detect when visible:
1. Bet Amount Input      2. Bet Increment (+)     3. Bet Decrement (-)
4. Bet Button (BET / Place Bet / Wager — the primary action button that places a wager, whatever
   its exact wording or icon)
5. Cancel Bet Button
6. Cash Out Button (Cash Out / Collect / Take Win — the action button taken mid-round to lock in
   a payout, whatever its exact wording or icon)
7. Auto Bet Toggle       8. Auto Cash Out Toggle
9. Auto Cash Out Input (target multiplier)       10. Multiplier Display (e.g. "1.00x")
11. Round History (strip of previous multipliers) 12. Balance Display
13. Second Bet Panel Bet Button (if a parallel panel exists)
14. Continue/Start Gate (a splash/overlay's clickable element blocking the real game — see rule above)

Return a JSON array of objects with:
- "label": a specific name from the list above
- "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000"""
    data = T.parse_gemini_json(T.gemini_call([api_img, prompt], _cfg()))
    if not isinstance(data, list):
        data = [data]
    controls = []
    for item in data:
        box = item.get("box_2d")
        if box and isinstance(box, list) and len(box) >= 4:
            item["center"] = _center(box)
            controls.append(item)
        elif item.get("label"):
            controls.append(item)
    return controls


# Provider recon table: real DOM selectors, tried before paying for a Gemini vision call.
# Spribe (Aviator) confirmed 2026-07-24 via a live authenticated session — this provider's
# chrome (tabs, stake stepper, quick-bet chips, Bet button) is real Angular-rendered DOM, not
# canvas; only the flight-path/multiplier graph is a <canvas>. DOM beats vision when it
# matches: no Gemini call, no box_2d->CSS scaling, none of the viewport-hazard exposure
# FRAMEWORK_BRIEF.md §2.4 warns about for the vision pipelines.
# Only the pre-bet BETTING-phase markup has been observed so far — Cash Out / Cancel Bet /
# Auto Cash Out selectors are unconfirmed (recon never reached ASCENDING with a live bet) and
# intentionally omitted here; a request for any of those labels falls through to vision until
# a live round captures their real selectors.
SPRIBE_DOM_SELECTORS = [
    ("Bet Amount Input",             "input[inputmode=decimal]:not([disabled])"),
    ("Bet Decrement (-)",            "button.minus"),
    ("Bet Increment (+)",            "button.plus"),
    ("Bet Button (BET / Place Bet)", "button.bet.btn-success"),
    ("Auto Bet Toggle",              "button.tab:has-text('Auto')"),
    ("Round History",                "button.tab:has-text('All Bets')"),
]


async def detect_crash_controls_dom(page) -> list:
    """DOM-first control detection for recon'd providers (see SPRIBE_DOM_SELECTORS).
    Returns [] on an unrecon'd provider (no selectors match) so the caller falls back to
    detect_crash_controls() unchanged — this is a pure optimization, never a hard dependency.

    A selector matching >1 element (e.g. two parallel bet panels) labels the first match
    with the base label and subsequent matches "<label> (panel N)", same convention the
    vision prompt uses for the second bet panel.
    """
    controls = []
    for label, selector in SPRIBE_DOM_SELECTORS:
        try:
            loc = page.locator(selector)
            count = await loc.count()
        except Exception:
            continue
        for i in range(count):
            try:
                box = await loc.nth(i).bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            center = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            entry_label = label if i == 0 else f"{label} (panel {i + 1})"
            # Synthesize box_2d (normalized 0-1000) from the real pixel bounding box so the
            # existing vision-oriented annotate_controls() draws DOM-detected controls too,
            # with no changes needed to that shared function.
            vw, vh = config_env.VIEWPORT_WIDTH, config_env.VIEWPORT_HEIGHT
            box_2d = [
                max(0, min(1000, box["y"] / vh * 1000)),
                max(0, min(1000, box["x"] / vw * 1000)),
                max(0, min(1000, (box["y"] + box["height"]) / vh * 1000)),
                max(0, min(1000, (box["x"] + box["width"]) / vw * 1000)),
            ]
            controls.append({"label": entry_label, "center": center, "box_2d": box_2d, "_dom": True})
    return controls


async def detect_controls(page, ss_dir, tag) -> tuple:
    """Screenshot + detect in one call: DOM-first (detect_crash_controls_dom), falling back to
    vision (detect_crash_controls) only if DOM found neither a Bet nor a Cash Out control —
    i.e. an unrecon'd provider, or a post-bet state DOM selectors don't cover yet.

    Returns (controls, screenshot_path). Callers should re-detect after every phase
    transition rather than reuse a stale snapshot — the Bet button commonly morphs into
    Cash Out (same screen slot, new label) the instant a round leaves BETTING, so controls
    detected pre-bet do not describe the ASCENDING-phase UI.
    """
    path = os.path.join(ss_dir, f"{tag}.png")
    await page.screenshot(path=path)
    controls = await detect_crash_controls_dom(page)
    if not (T.find_control(controls, "bet button", "place bet")
            or T.find_control(controls, "cash out", "cashout")):
        try:
            controls = detect_crash_controls(Image.open(path))
        except Exception as e:
            print(f"    detect warning: {e}")
            controls = []
    return controls, path


async def detect_controls_vision(page, ss_dir, tag) -> tuple:
    """Screenshot + vision detection only, no DOM shortcut. Use this (not detect_controls())
    for anything post-bet/ASCENDING: recon only confirmed the pre-bet BETTING-phase DOM
    (SPRIBE_DOM_SELECTORS), never whether the morphed Bet->Cash Out control keeps the same
    CSS classes. If it does, DOM matching would keep reporting "Bet Button" no matter what
    the control actually says right now, silently masking the exact state changes these
    call sites need to see. Returns (controls, screenshot_path)."""
    path = os.path.join(ss_dir, f"{tag}.png")
    await page.screenshot(path=path)
    try:
        controls = detect_crash_controls(Image.open(path))
    except Exception as e:
        print(f"    detect warning: {e}")
        controls = []
    return controls, path


def read_round_state(image: Image.Image) -> dict:
    """Classify the crash round phase + read live values from a screenshot.

    Returns {"phase": GATE|LOADING|BETTING|ASCENDING|CRASHED|RESULT|DISCONNECTED,
             "multiplier": float|null, "balance": str|null, "bet": str|null,
             "gate_target": [ymin,xmin,ymax,xmax]|null}.
    Vision-based; the primary phase signal until parse_round_frame() is filled in.

    v2 prompt (2026-07-30): added the GATE phase + gate_target after the 15-provider
    validation sweep found Light-and-Wonder's "Red Light Green Light" gets stuck behind a
    "Tap to continue..." splash that v1's phase list had no name for — it silently fell into
    LOADING forever, and auto_handle_crash_startup()'s only response was a blind screen-center
    click on a fixed schedule (attempts 6/12), which doesn't reliably hit an off-center gate
    button. GATE + a real click target lets the caller dismiss it precisely, whatever provider
    it's from. v1 kept verbatim in crash_prompt_v1_backup.md.
    """
    api_img = image.copy()
    api_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
    prompt = """This is a "crash" style casino game (a multiplier rises from 1.00x and can crash).
Determine the CURRENT round phase and read the values.

Phases:
- "GATE"         : a splash/overlay is blocking the real game and needs a click to proceed —
                   "Tap to Play"/"Click to continue", a cookie/age-consent banner, an
                   autoplay-unlock prompt, a provider/game logo screen with a "play" affordance.
                   This is DIFFERENT from LOADING (below): a GATE needs a user click, LOADING
                   just needs more time. Many providers' UIs use one or more of these before the
                   real game appears — don't assume every splash is LOADING.
- "LOADING"      : still loading (progress bar / spinner), no interactive element visible, UI not
                   interactive yet.
- "BETTING"      : a betting window is open (countdown / "place your bet"); the round has not started rising.
- "ASCENDING"    : a multiplier is currently rising / the plane/rocket/vehicle is in flight.
- "CRASHED"      : the round just ended ("FLEW AWAY" / "BUSTED"), multiplier frozen/red.
- "RESULT"       : a brief win/settlement shown between rounds.
- "DISCONNECTED" : an error/disconnect overlay is shown ("You have been disconnected", a red ✗/X circle,
                   a "HOME" or "refresh" button) — the game is NOT running. Do NOT confuse the red ✗ with CRASHED.

Return JSON:
{"phase": "...",
 "multiplier": current multiplier as a number like 2.53 (or null if none shown),
 "balance": "exact balance text or null",
 "bet": "exact bet/stake text or null",
 "gate_target": [ymin, xmin, ymax, xmax] normalized 0-1000 box of the element to click to
   dismiss the GATE (or null if phase is not GATE)}"""
    return T.parse_gemini_json(T.gemini_call([api_img, prompt], _cfg()))


def parse_round_frame(body: str):
    """Map a SignalR `vpb.message` websocket frame to a round-state update.

    TODO(recon): fill in the exact FlyX Party payload. Recon confirmed the transport
    (SignalR pushhub, topic "vpb.message") but the session token expired before a live
    round broadcast was captured. Expected fields once captured: round id, phase
    (betting|flying|crashed), current multiplier, crash multiplier.
    Returns a dict or None if `body` is not a round frame.

    Until this mapping is filled in, CrashObserver logs every raw WS frame it sees to
    `round_frames.jsonl` in the run dir (recognized by this function or not) — the next
    run against a live token gives real payloads to finish this function against.
    """
    import json
    try:
        data = json.loads(body)
    except Exception:
        return None
    msgs = data.get("M") if isinstance(data, dict) else None
    if not msgs:
        return None
    return {"raw": msgs}   # placeholder until a live vpb.message frame is captured


# ═══════════════════════════════════════════════════════════════════
#   Round-state observer (vision + motion; websocket-ready)
# ═══════════════════════════════════════════════════════════════════
class CrashObserver:
    """Tracks crash round phases primarily by polling the screen (vision), corroborated by
    frame_motion (a rising multiplier animates; betting/crashed are comparatively still).

    Also drains `monitor.responses` for WS pushhub frames each sample: any frame
    parse_round_frame() recognizes with a "phase"/"multiplier" field overrides the vision
    read (network > vision, same precedence slot_spin gives spins). Every WS frame is also
    persisted to `frames_log_path` (recognized or not) so a run against a live token captures
    real payloads for finishing parse_round_frame(). New traffic also wakes waits early
    (see _wait_tick) instead of sitting on a blind fixed-interval poll, since round
    transitions tend to push a WS frame right as they happen."""

    def __init__(self, page, monitor=None, frames_log_path=None):
        self.page = page
        self.monitor = monitor
        self.samples = []
        self.round_frames = []
        self._last_shot = None
        self._resp_idx = len(monitor.responses) if monitor else 0
        self.frames_log_path = frames_log_path

    def _drain_network_frames(self):
        """Return newly-decoded round frames since the last drain; log every raw WS frame."""
        if not self.monitor:
            return []
        new = self.monitor.responses[self._resp_idx:]
        self._resp_idx = len(self.monitor.responses)
        parsed_frames = []
        for r in new:
            if not r["path"].endswith("@ws"):
                continue
            parsed = parse_round_frame(r.get("body", ""))
            if self.frames_log_path:
                try:
                    with open(self.frames_log_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"t": r["t"], "path": r["path"],
                                            "recognized": parsed is not None,
                                            "body": (r.get("body") or "")[:2000]}) + "\n")
                except Exception:
                    pass
            if parsed is not None:
                self.round_frames.append(parsed)
                parsed_frames.append(parsed)
        return parsed_frames

    async def _wait_tick(self, seconds, poll=0.25):
        """Sleep up to `seconds`, but return early the moment new network traffic arrives —
        wakes samples close to a round transition instead of on a blind fixed interval."""
        start = time.time()
        baseline = len(self.monitor.responses) if self.monitor else -1
        while time.time() - start < seconds:
            if self.monitor and len(self.monitor.responses) > baseline:
                return
            await asyncio.sleep(poll)

    async def sample(self, tag="sample"):
        net_frames = self._drain_network_frames()
        path = T._ss(f"crash_{tag}.png")
        try:
            await self.page.screenshot(path=path)
            state = read_round_state(Image.open(path))
            shot_ok = True
        except Exception as e:
            # A transient screenshot/vision hiccup must NOT kill the whole run. auto_handle_
            # crash_startup already tolerates this exact failure class (Page.screenshot timeouts
            # during heavy page activity); sample() needs the same tolerance — it's called from
            # every wait_for_phase/observe_ascending loop across TEST 2-4 and every wager test, so
            # one unhandled timeout here previously crashed the whole process before _emit_report
            # ever ran, losing every result gathered so far (confirmed 2026-07-29 on a real
            # FlyX Party run: Page.screenshot timeout mid-TEST-4 -> zero results.json written).
            print(f"    [WARN] sample({tag}) failed: {e}")
            state, shot_ok = {}, False
        motion = (slot_spin.frame_motion(self._last_shot, path)
                 if self._last_shot and shot_ok else None)
        if shot_ok:
            self._last_shot = path
        phase = (state.get("phase") or "").upper()
        multiplier = state.get("multiplier")
        for nf in net_frames:
            if nf.get("phase"):
                phase = str(nf["phase"]).upper()
            if nf.get("multiplier") is not None:
                multiplier = nf["multiplier"]
        entry = {
            "t": time.time(),
            "phase": phase,
            "multiplier": multiplier,
            "balance": state.get("balance"),
            "bet": state.get("bet"),
            "motion": motion,
            "network_frames": len(net_frames),
        }
        self.samples.append(entry)
        return entry

    async def wait_for_phase(self, target_phase, timeout=45, interval=1.5):
        start = time.time()
        i = 0
        while time.time() - start < timeout:
            s = await self.sample(tag=f"{target_phase.lower()}_{i}")
            if s["phase"] == target_phase.upper():
                return s
            i += 1
            await self._wait_tick(interval)
        return None

    async def wait_for_any_phase(self, phases, timeout=45, interval=1.5, tag="ready"):
        """Like wait_for_phase, but returns on the first match among several target phases
        (or on DISCONNECTED, so callers can abort fast instead of timing out)."""
        start = time.time()
        i = 0
        while time.time() - start < timeout:
            s = await self.sample(tag=f"{tag}_{i}")
            if s["phase"] in phases or s["phase"] == "DISCONNECTED":
                return s
            i += 1
            await self._wait_tick(interval)
        return None

    async def observe_ascending(self, timeout=30, interval=0.8):
        """Sample the multiplier through an ASCENDING phase until it CRASHES.
        Returns the ordered list of multiplier readings during the flight."""
        readings = []
        start = time.time()
        i = 0
        while time.time() - start < timeout:
            s = await self.sample(tag=f"asc_{i}")
            if s["phase"] == "ASCENDING" and isinstance(s["multiplier"], (int, float)):
                readings.append(s["multiplier"])
            elif s["phase"] in ("CRASHED", "RESULT") and readings:
                break
            i += 1
            await self._wait_tick(interval)
        return readings


# ═══════════════════════════════════════════════════════════════════
#   Wager actions — place bet / cash out (LIVE only, gated by MAX_STAKE)
# ═══════════════════════════════════════════════════════════════════
async def _read_balance(page, ss_dir, tag):
    """Screenshot + vision balance read (mirrors read_game_values/parse_amount in slot_spin).
    Same transient-timeout tolerance as CrashObserver.sample() — a failed read here mid-wager
    must not crash the process and lose the whole run's results; the caller sees balance=None
    and the affected wager test reports "could not read balance" instead of the run vanishing."""
    path = os.path.join(ss_dir, f"{tag}.png")
    try:
        await page.screenshot(path=path)
        state = read_round_state(Image.open(path))
    except Exception as e:
        print(f"    [WARN] _read_balance({tag}) failed: {e}")
        state = {}
    return T.parse_amount(state.get("balance") or ""), state


async def _read_bet_value(page, ss_dir, tag):
    """Screenshot + vision read of the CURRENT stake/bet field (mirrors _read_balance above).
    Used by floor_bet_input's closed-loop flooring, which needs to see whether a decrement
    click actually moved the on-screen stake."""
    path = os.path.join(ss_dir, f"{tag}.png")
    try:
        await page.screenshot(path=path)
        state = read_round_state(Image.open(path))
    except Exception as e:
        print(f"    [WARN] _read_bet_value({tag}) failed: {e}")
        state = {}
    return T.parse_amount(state.get("bet") or ""), state


async def floor_bet_input(page, controls, ss_dir, max_clicks=8, rounds=4):
    """No reliable minimum bet is known for this game (no operator override, no catalog
    minBetAmount) — discover the real floor IN-GAME instead of guessing, mirroring
    slot_dsc.py's closed-loop stepper flooring (_probe_and_floor): one probe click, confirm
    the stake did not RISE (a mislabeled increment), then commit rounds of clicks until the
    value stops dropping. Only used for a raw launch URL with no catalog lookup — the crash
    sweep and by-name single launches almost always have a catalog hint by the time this
    would matter (see resolve_stake). Returns (stake, at_floor); at_floor=False means we gave
    up before confirming the true minimum (unreadable value, or the round cap hit while it
    was still dropping) — the caller should treat the returned stake as a best-effort value,
    not a confirmed floor."""
    dec = T.find_control(controls, "bet decrement", "bet -", "decrease")
    if not dec:
        print("  [FLOOR] No bet-decrement control detected — cannot floor the stake in-game")
        return None, False
    stake, _ = await _read_bet_value(page, ss_dir, "floor_before")
    print(f"  [FLOOR] Flooring stake in-game via '{dec.get('label')}' "
          f"(currently {stake if stake is not None else 'unreadable'})")
    try:
        await page.mouse.click(*dec["center"])
    except Exception as e:
        print(f"    [FLOOR] click warning: {e}")
        return stake, False
    await asyncio.sleep(0.8)
    probed, _ = await _read_bet_value(page, ss_dir, "floor_probe")
    if stake is not None and probed is not None and probed > stake + 0.01:
        print(f"    [FLOOR] ⚠️ '{dec.get('label')}' RAISED the stake ({stake:g} → {probed:g}) — "
              f"mislabeled increment, abandoning after one click")
        return probed, False
    cur = probed if probed is not None else stake
    for rnd in range(rounds):
        for _ in range(max_clicks - (1 if rnd == 0 else 0)):
            await page.mouse.click(*dec["center"])
            await asyncio.sleep(0.25)
        new, _ = await _read_bet_value(page, ss_dir, f"floor_r{rnd}")
        if new is None:
            return cur, False              # unreadable — keep best-known, floor unconfirmed
        if cur is not None and new >= cur - 0.01:
            print(f"  [FLOOR] Stepper bottomed out at {new:g} — this is the game minimum")
            return new, True                # stopped moving — this stepper's real floor
        cur = new
    return cur, False                       # round cap hit while still dropping


async def resolve_stake(page, controls, ss_dir, bet_override=None, min_bet_hint=None):
    """Decide what to wager, in priority order: an explicit operator-typed override, then the
    casino catalog's own minBetAmount for this game, then (only when neither exists — a raw
    launch URL with no catalog lookup) in-game floor detection. Never guesses a number when
    none of the three is available. Returns (stake, source) for logging/reporting; source in
    {"override","catalog","floored","unknown"}."""
    if bet_override is not None and bet_override > 0:
        return bet_override, "override"
    if min_bet_hint is not None and min_bet_hint > 0:
        return min_bet_hint, "catalog"
    stake, at_floor = await floor_bet_input(page, controls, ss_dir)
    if stake is not None and stake > 0:
        return stake, "floored" if at_floor else "floored (unconfirmed)"
    return None, "unknown"


async def _await_request(monitor, since_idx, timeout=5.0, poll=0.2):
    """Poll for the first non-idle POST/WS request since `since_idx` — a real wager action is
    a POST or WS send, exactly like a slot spin (reuses UnifiedGameMonitor.spin_request_since
    verbatim rather than re-implementing the same timing loop)."""
    waited = 0.0
    while waited < timeout:
        req = monitor.spin_request_since(since_idx, any_method=False, allow_idle=False)
        if req:
            return req
        await asyncio.sleep(poll)
        waited += poll
    return None


def _count_requests(monitor, path, since_idx):
    if not path:
        return 0
    return sum(1 for r in monitor.requests[since_idx:]
               if r["path"] == path and r["method"] in ("POST", "WS_SEND"))


async def place_bet(page, controls, monitor, stake, ss_dir, tag="bet"):
    """Set the stake (if a bet-amount input is detected) and click Bet. Verified the same way
    slot_spin verifies a spin: first non-idle POST/WS after the click, reconciled with the
    provider-agnostic slot_spin.parse_result_body() (it scans by field MEANING, not by
    provider, so it works unchanged for a bet response too). Hard-gated by
    config_env.MAX_STAKE — the same cap every slot spin is refused above.

    Returns {"clicked", "path", "request_count", "net", "shots", "status"}.
    """
    rep = {"clicked": False, "path": None, "request_count": 0, "net": {}, "shots": {}}
    sp = lambda n: os.path.join(ss_dir, f"{tag}_{n}.png")

    if stake is not None and stake > config_env.MAX_STAKE:
        print(f"    [CAP] Stake {stake:g} exceeds the safety cap {config_env.MAX_STAKE:g} — bet refused")
        rep["status"] = "stake_cap"
        return rep

    bet_btn = T.find_control(controls, "bet button", "place bet")
    if not bet_btn:
        rep["status"] = "no_bet_button"
        return rep
    bet_input = T.find_control(controls, "bet amount input", "bet input")

    await page.screenshot(path=sp("pre")); rep["shots"]["pre"] = sp("pre")
    if bet_input and stake is not None:
        try:
            await page.mouse.click(*bet_input["center"])
            await page.keyboard.press("Control+A")
            await page.keyboard.type(f"{stake:g}")
        except Exception as e:
            print(f"    [BET] stake-input warning: {e}")

    idx = monitor.req_count()
    t0 = time.time()
    await page.mouse.click(*bet_btn["center"])
    rep["clicked"] = True
    req = await _await_request(monitor, idx)
    await asyncio.sleep(1.2)   # let any duplicate/delayed request land before counting
    if req:
        rep["path"] = req["path"]
        rep["request_count"] = _count_requests(monitor, req["path"], idx)
        resp = monitor.response_for(req["path"], t0)
        if resp:
            rep["net"] = slot_spin.parse_result_body(resp["body"])
    await page.screenshot(path=sp("post")); rep["shots"]["post"] = sp("post")
    rep["status"] = "ok"
    return rep


async def cash_out(page, controls, monitor, ss_dir, tag="cashout"):
    """Click Cash Out. Verified the same way as place_bet (first non-idle POST/WS after the
    click, reconciled via parse_result_body)."""
    rep = {"clicked": False, "path": None, "net": {}, "shots": {}}
    sp = lambda n: os.path.join(ss_dir, f"{tag}_{n}.png")

    cashout_btn = T.find_control(controls, "cash out", "cashout")
    if not cashout_btn:
        rep["status"] = "no_cashout_button"
        return rep

    await page.screenshot(path=sp("pre")); rep["shots"]["pre"] = sp("pre")
    idx = monitor.req_count()
    t0 = time.time()
    await page.mouse.click(*cashout_btn["center"])
    rep["clicked"] = True
    req = await _await_request(monitor, idx)
    await asyncio.sleep(1.2)
    if req:
        rep["path"] = req["path"]
        resp = monitor.response_for(req["path"], t0)
        if resp:
            rep["net"] = slot_spin.parse_result_body(resp["body"])
    await page.screenshot(path=sp("post")); rep["shots"]["post"] = sp("post")
    rep["status"] = "ok"
    return rep


async def auto_handle_crash_startup(page, max_attempts=25, reload_retries=2):
    """Wait for the crash game to finish loading; click through any intro splash.

    Requires TWO CONSECUTIVE live-phase reads before declaring "ready" — a single read can
    catch a transient screen (e.g. a reconnect banner) between the loading splash and the
    real game UI, which was observed reporting "ready" one frame before a screen that only
    showed a stray HOME button (see runs/20260721_223946_crash_aviator/results.json).

    reload_retries: a "DISCONNECTED" read is not always a dead session — recon on 2026-07-27
    showed the dominant real-world cause is Spribe's own client-config fetch
    (app-config.spribegaming.com/aviator/<operator>.json — a static per-operator file, NOT
    scoped to this session's launch token) intermittently failing CORS and giving up for
    good on THAT page load ("Retry limit of client config exceeded!"), while reloading the
    identical URL succeeds moments later roughly half the time with no other change. Safe to
    retry precisely because the failing resource carries no token — a reload cannot burn a
    single-use launch token the way re-authenticating would. Waiting longer never helps here
    (the client has already given up); only a fresh fetch does. This does NOT cover a genuine
    spent/reused-token disconnect ("session held by another tab") — reload_retries just caps
    the blast radius if that's what's actually happening, so the caller still aborts instead
    of looping forever.

    Returns a status string:
      "ready"        — TWO consecutive interactive round-phase reads (game is live)
      "disconnected" — still showing the disconnect/error screen after exhausting reloads
                       (token spent/expired, or the session is held by another tab)
      "timeout"      — never became ready within max_attempts
    """
    print("\n  [STARTUP] Waiting for crash game to load...")
    await asyncio.sleep(8)
    last_ready_phase = None
    reloads_used = 0
    gates_clicked = 0
    for attempt in range(1, max_attempts + 1):
        try:
            ss_path = T._ss("crash_startup_check.png")
            await page.screenshot(path=ss_path)
            state = read_round_state(Image.open(ss_path))
            phase = (state.get("phase") or "LOADING").upper()
            if phase == "GATE":
                # A splash/consent/tap-to-play overlay is blocking the real UI — click it
                # precisely (from gate_target) rather than guessing screen-center on a fixed
                # schedule. Some providers stack more than one gate in a row (e.g. cookie
                # consent -> tap to play), so keep clicking every time GATE is seen, capped
                # generously since a stuck GATE read is cheaper to retry than to time out on.
                target = state.get("gate_target")
                w, h = config_env.VIEWPORT_WIDTH, config_env.VIEWPORT_HEIGHT
                x, y = _center(target) if target else (w // 2, h // 2)
                gates_clicked += 1
                print(f"    [{attempt}/{max_attempts}] GATE screen detected — "
                      f"clicking ({x:.0f},{y:.0f}) to dismiss (gate #{gates_clicked})...")
                try:
                    await page.mouse.click(x, y)
                except Exception as e:
                    print(f"    gate-click warning: {e}")
                last_ready_phase = None
                await asyncio.sleep(2.5)
                continue
            if phase == "DISCONNECTED":
                if reloads_used < reload_retries:
                    reloads_used += 1
                    print(f"    [{attempt}/{max_attempts}] DISCONNECTED screen detected — "
                          f"reloading (attempt {reloads_used}/{reload_retries})...")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception as e:
                        print(f"    reload warning: {e}")
                    last_ready_phase = None
                    await asyncio.sleep(8)
                    continue
                print(f"    [{attempt}/{max_attempts}] DISCONNECTED screen detected — "
                      f"reloads exhausted, aborting startup.")
                return "disconnected"
            if phase in ROUND_READY_PHASES:
                if last_ready_phase is not None:
                    print(f"    [{attempt}/{max_attempts}] Game ready (phase={phase}, "
                          f"confirmed after {last_ready_phase}).")
                    return "ready"
                print(f"    [{attempt}/{max_attempts}] phase={phase} (confirming...)")
                last_ready_phase = phase
                await asyncio.sleep(2)
                continue
            last_ready_phase = None
            print(f"    [{attempt}/{max_attempts}] phase={phase} (still loading)...")
            if attempt in (6, 12):
                w, h = config_env.VIEWPORT_WIDTH, config_env.VIEWPORT_HEIGHT
                await page.mouse.click(w // 2, h // 2)
            await asyncio.sleep(4)
        except Exception as e:
            print(f"    [{attempt}/{max_attempts}] startup check warning: {e}")
            last_ready_phase = None
            await asyncio.sleep(4)
    print("  [STARTUP] Reached max attempts; proceeding anyway.")
    return "timeout"


async def _abort_session(page, results, ctx_start, context, browser, recordings_dir, detail):
    """Shared abort path: one clear failing TestResult + normal teardown/report, instead of
    running every remaining test against a dead/drifted screen."""
    await page.screenshot(path=T._ss("crash_controls.png"))
    tr = T.TestResult("Crash game session is live", "crash_controls.png")
    tr.passed = False
    tr.details = detail
    tr.video_start, tr.video_end = 0.0, time.time() - ctx_start
    results.append(tr); print(tr)
    try:
        await T.finalize_media(page, results, recordings_dir)
    except Exception:
        pass
    try:
        await context.close()
    except Exception:
        pass
    await browser.close()
    T._emit_report(results)
    return results


# ═══════════════════════════════════════════════════════════════════
#   MAIN TEST FLOW
# ═══════════════════════════════════════════════════════════════════
async def run_crash_tests(url, live=False, bet="", min_bet="", target="", rounds=2, mobile=False,
                          headless=False, account=None, brand="betway", region="ZA",
                          provider=None, game_name=None):
    results = []
    os.makedirs(T.SCREENSHOT_DIR, exist_ok=True)
    monitor = slot_spin.UnifiedGameMonitor()

    print(f"\n{'='*70}\n  CRASH GAME UI TEST SUITE  "
          f"({'LIVE — REAL WAGERS' if live else 'DRY-RUN — no wagers'})\n{'='*70}")
    print(f"URL: {url[:80]}...\n")

    recordings_dir = os.path.join(T.RUN_DIR, "video") if T.RUN_DIR else \
        os.path.join(T._base_dir(), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)

    # Tlogs plumbing (live only): a "report_path" the same way the slot pipeline anchors one —
    # doesn't need to be a real populated Excel, dsc_report.records_path() just derives the
    # sibling *_records.jsonl name from it. Lives directly in runs/ (not the per-run subfolder)
    # so /tlogs-reports' glob (runs/*_records.jsonl) and the dashboard's Validate button find it,
    # exactly like the slot DSC reports already do.
    dsc_report_path = None
    if live and T.RUN_DIR:
        from modules import dsc_report
        dsc_report_path = os.path.join(os.path.dirname(T.RUN_DIR), f"Crash_Live_{T.RUN_ID}.xlsx")

    async with async_playwright() as p:
        _no_throttle = ["--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling"]
        if mobile:
            browser = await p.chromium.launch(headless=headless, args=_no_throttle)
            context_args = {"ignore_https_errors": True, "record_video_dir": recordings_dir,
                            "locale": "en-ZA"}
            context_args.update(p.devices['iPhone 13'])
        else:
            _win_pos = os.environ.get("GAMEGUARD_WINDOW_POS", "0,0")
            browser = await p.chromium.launch(
                headless=headless,
                args=["--window-size=1920,1080", f"--window-position={_win_pos}"] + _no_throttle)
            context_args = {"ignore_https_errors": True, "record_video_dir": recordings_dir,
                            "locale": "en-ZA", "no_viewport": True}
        context = await browser.new_context(**context_args)
        page = await context.new_page()
        ctx_start = time.time()
        monitor.attach(page)

        # Live viewport → config_env (box_2d 0-1000 maps to CSS px). no_viewport => read window.
        _vp = page.viewport_size
        if _vp:
            config_env.set_viewport(_vp["width"], _vp["height"])
        else:
            try:
                dims = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                config_env.set_viewport(dims["w"], dims["h"])
            except Exception:
                pass

        print("[SETUP] Loading crash game...")
        # A real player never navigates the top-level browser to this URL directly — it's
        # normally loaded as an IFRAME inside the casino's own lobby page
        # (https://www.betway.co.za/lobby/casino-games/game/<slug>), so a genuine request always
        # carries that page as its Referer. Confirmed 2026-08-04 (real user report): Aviatrix,
        # one of the most consistently-DISCONNECTED/connection-reset providers all session, loads
        # fine when reached that way manually. Passing the lobby origin as `referer` here gets
        # this request closer to what a real browser sends, without the much larger change of
        # actually embedding the game in an iframe (unnecessary — every downstream test already
        # works off full-page screenshots/pixel-coordinate clicks, iframe-position-agnostic).
        try:
            origin = region_config(brand, region).get("origin") or "https://www.betway.co.za"
        except Exception:
            origin = "https://www.betway.co.za"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000, referer=origin)
        except Exception as e:
            print(f"  Nav warning: {e}")

        startup = await auto_handle_crash_startup(page)
        if startup == "disconnected":
            # The launch token is spent/expired, or another tab holds the session. Every test
            # would run against a dead screen — abort fast with one clear result instead.
            return await _abort_session(
                page, results, ctx_start, context, browser, recordings_dir,
                "DISCONNECTED screen shown — the launch token is spent/expired, or the session "
                "is held by another tab/browser. Supply a FRESH launch URL that has not been "
                "opened elsewhere (or mint a dedicated automation session).")

        await monitor.learn_idle(duration=8)
        if os.environ.get("GAMEGUARD_DUMP_NETWORK"):
            # Recon aid: learn_idle() only keeps a deduped path set (no status/body). Dump the
            # full request+response capture (incl. any WS frames) so a stuck-load/DISCONNECT can
            # be diagnosed from actual server responses instead of guessing.
            import json as _json
            dump_path = os.path.join(T.RUN_DIR or os.path.dirname(T.SCREENSHOT_DIR), "network_dump.json")
            with open(dump_path, "w", encoding="utf-8") as f:
                _json.dump({"requests": monitor.requests, "responses": monitor.responses},
                           f, indent=2, default=str)
            print(f"  [DEBUG] Full network capture dumped -> {dump_path}")
        frames_log_path = os.path.join(T.RUN_DIR, "round_frames.jsonl") if T.RUN_DIR else \
            os.path.join(os.path.dirname(T.SCREENSHOT_DIR), "round_frames.jsonl")
        obs = CrashObserver(page, monitor=monitor, frames_log_path=frames_log_path)

        # auto_handle_crash_startup's "ready" can go stale during learn_idle() (e.g. a
        # reconnect banner slipping in) — reconfirm a live phase right before the controls
        # screenshot instead of trusting a startup result that's now several seconds old.
        #
        # One reload retry here, same reasoning as auto_handle_crash_startup's reload_retries:
        # the 2026-08-03 provider validation sweep hit this exact abort path repeatedly (Aviatrix,
        # Betway, Games-Global, Playtech, Pragmatic-Play, Spribe, Light-and-Wonder) on runs where
        # startup itself had already succeeded moments earlier — i.e. the same transient-drop
        # class auto_handle_crash_startup already recovers from about half the time via reload,
        # just occurring a few seconds later than that function's own retry window covers. A
        # single retry here is cheap (one reload, ~10s) against a real chance of saving an
        # otherwise-wasted run; it does NOT help a genuine spent/reused-token disconnect, so this
        # still aborts for real if the second check also fails.
        pre_check = await obs.wait_for_any_phase(ROUND_READY_PHASES, timeout=20, interval=2, tag="pre_check")
        if pre_check is None or pre_check["phase"] == "DISCONNECTED":
            print(f"  [PRE-CHECK] phase drifted (last={pre_check['phase'] if pre_check else 'none (timed out)'}) "
                  f"— reloading once before aborting...")
            try:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"    reload warning: {e}")
            await asyncio.sleep(8)
            pre_check = await obs.wait_for_any_phase(ROUND_READY_PHASES, timeout=20, interval=2, tag="pre_check_retry")
        if pre_check is None or pre_check["phase"] == "DISCONNECTED":
            last_phase = pre_check["phase"] if pre_check else "none (timed out)"
            return await _abort_session(
                page, results, ctx_start, context, browser, recordings_dir,
                f"Phase drifted away from a live round after startup (last phase={last_phase}) "
                "— session likely dropped. Supply a fresh launch URL.")

        # ── TEST 1: Crash UI controls detected ──
        # Full vision detection here (not the DOM-first detect_controls() used later for
        # wager-test re-detects) — this is the one-time "detect EVERYTHING" inventory the
        # report's annotated hero shot is built from (Multiplier Display, Balance, Round
        # History, ...), not just the actionable Bet/Cash Out controls DOM selectors cover.
        print(f"\n{'='*70}\n  TEST 1: Detect crash UI controls\n{'='*70}")
        await page.screenshot(path=T._ss("crash_controls.png"))
        try:
            controls = detect_crash_controls(Image.open(T._ss("crash_controls.png")))
        except Exception as e:
            controls = []
            print(f"  detect warning: {e}")
        labels = [c.get("label") for c in controls]
        print(f"  Detected {len(controls)} controls: {labels}")
        # Annotated hero shot atop the report (same convention as the slot suite).
        try:
            T.annotate_controls(T._ss("crash_controls.png"), controls, T._ss("crash_elements.png"))
            T.ELEMENTS_SHOT = "crash_elements.png"
        except Exception:
            pass

        bet_btn = T.find_control(controls, "bet button", "place bet")
        cashout_btn = T.find_control(controls, "cash out", "cashout")
        multiplier_disp = T.find_control(controls, "multiplier")
        history = T.find_control(controls, "history")

        t1 = T.TestResult("Crash UI controls detected", "crash_controls.png")
        core = sum(bool(x) for x in (bet_btn, cashout_btn, multiplier_disp))
        t1.passed = core >= 2
        t1.details = f"Found {len(controls)} controls: {labels}"
        t1.video_start, t1.video_end = 0.0, time.time() - ctx_start
        results.append(t1); print(t1)

        # ── TEST 2: Full round lifecycle observed (no wager) ──
        # BETTING's wait is generously longer than ASCENDING/CRASHED's: TEST 1's screenshot +
        # detection call can land at ANY point in a round already in flight, so this wait may
        # have to cover however much of the CURRENT round remains PLUS the RESULT pause before
        # the NEXT betting window opens — not just one round's ascent. 45s repeatedly produced
        # "Phases observed: []" on Light-and-Wonder and Playtech-Live during the 2026-07-30
        # provider validation sweep even after the GATE-phase/functional-detection fix landed
        # (both had already reached a live round by then — a real multiplier reading of 1.05
        # confirmed it — TEST 2 just started mid-flight on a round too long for a 45s catch-up).
        # This is a round-timing race, not provider-specific: any provider can crash late in a
        # 45s window. 90s comfortably covers every round-cycle length seen across the 15-provider
        # sweep (longest full BETTING->ASCENDING->CRASHED observed end-to-end was ~130s).
        print(f"\n{'='*70}\n  TEST 2: Observe BETTING -> ASCENDING -> CRASHED\n{'='*70}")
        _t2s = time.time() - ctx_start
        betting = await obs.wait_for_phase("BETTING", timeout=90)
        ascending = await obs.wait_for_phase("ASCENDING", timeout=45) if betting else None
        crashed = await obs.wait_for_phase("CRASHED", timeout=45) if ascending else None
        seen = [ph for ph, s in [("BETTING", betting), ("ASCENDING", ascending), ("CRASHED", crashed)] if s]
        t2 = T.TestResult("Round lifecycle observed (betting->ascending->crashed)", "crash_ascending_0.png")
        t2.passed = len(seen) == 3
        t2.details = f"Phases observed: {seen}"
        t2.video_start, t2.video_end = _t2s, time.time() - ctx_start
        results.append(t2); print(t2)

        # ── TEST 3: Multiplier ascends monotonically (no wager) ──
        # Same round-cycle-length reasoning as TEST 2's BETTING wait above — this is the wait
        # for the NEXT round to start, not the current one, so it needs the same margin.
        print(f"\n{'='*70}\n  TEST 3: Multiplier ascends monotonically\n{'='*70}")
        _t3s = time.time() - ctx_start
        await obs.wait_for_phase("BETTING", timeout=90)
        readings = await obs.observe_ascending(timeout=30)
        print(f"  Multiplier readings: {readings}")
        t3 = T.TestResult("Multiplier ascends monotonically", "crash_asc_0.png")
        if len(readings) >= 3:
            non_decreasing = all(b >= a - 0.01 for a, b in zip(readings, readings[1:]))
            t3.passed = non_decreasing
            t3.details = f"{len(readings)} readings, monotonic={non_decreasing}: {readings}"
        else:
            t3.passed = None
            t3.details = f"Too few multiplier readings to judge: {readings}"
        t3.video_start, t3.video_end = _t3s, time.time() - ctx_start
        results.append(t3); print(t3)

        # ── TEST 4: Round history updates after a crash (no wager) ──
        print(f"\n{'='*70}\n  TEST 4: Round history updates after a crash\n{'='*70}")
        t4 = T.TestResult("Round history updates after crash", "crash_hist_after.png")
        if history:
            await page.screenshot(path=T._ss("crash_hist_before.png"))
            await obs.wait_for_phase("CRASHED", timeout=60)
            await asyncio.sleep(2)
            await page.screenshot(path=T._ss("crash_hist_after.png"))
            b = Image.open(T._ss("crash_hist_before.png")).copy(); b.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
            a = Image.open(T._ss("crash_hist_after.png")).copy(); a.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
            prompt = ("Compare the round-history strip (row of previous multipliers) in these two crash-game "
                      "screenshots. Did a new multiplier value get added/changed in the second image? "
                      'Return JSON: {"updated": true/false, "reason": "brief"}')
            try:
                res = T.parse_gemini_json(T.gemini_call([b, a, prompt], _cfg()))
                t4.passed = bool(res.get("updated"))
                t4.details = res.get("reason", "")
            except Exception as e:
                t4.passed = None
                t4.details = f"history check error: {e}"
        else:
            t4.passed = None
            t4.details = "No round-history control detected"
        results.append(t4); print(t4)

        # ── WAGER-DEPENDENT TESTS (only under --live) ──
        wager_tests = [
            "Place bet during betting window = 1 place-bet request",
            "Cash out during flight returns bet x multiplier",
            "Balance updates correctly (-bet, +payout on cash out)",
            "Auto cash-out fires at configured target",
            "Bet rejected when placed mid-flight",
            "Cancel bet before round start refunds stake",
            "Rapid double-click Bet = still 1 bet",
        ]
        if not live:
            print(f"\n{'='*70}\n  WAGER TESTS SKIPPED (dry-run). Re-run with --live to execute.\n{'='*70}")
            for name in wager_tests:
                tr = T.TestResult(name, "crash_controls.png")
                tr.passed = None
                tr.details = "Skipped: requires --live (places real wagers)"
                results.append(tr); print(tr)
        else:
            # Re-detect controls right before wagering — state may have drifted since TEST 1.
            # Done BEFORE stake resolution (not after, as before) because the in-game floor
            # fallback in resolve_stake needs real, fresh controls to floor against.
            w_controls, _ = await detect_controls(page, T.SCREENSHOT_DIR, "wager_controls")
            bet_override = T.parse_amount(bet) if bet else None
            min_bet_hint = T.parse_amount(str(min_bet)) if min_bet else None
            stake, stake_source = await resolve_stake(page, w_controls, T.SCREENSHOT_DIR,
                                                       bet_override=bet_override,
                                                       min_bet_hint=min_bet_hint)
            if stake is None:
                print(f"\n{'='*70}\n  [!] Could not resolve a safe minimum stake automatically "
                      f"(no --bet override, no catalog min-bet, and no in-game bet control to "
                      f"floor) — refusing to guess.\n{'='*70}")
                for name in wager_tests:
                    tr = T.TestResult(name, "crash_controls.png")
                    tr.passed = None
                    tr.details = "Skipped: could not resolve a safe minimum stake automatically"
                    results.append(tr); print(tr)
            elif stake > config_env.MAX_STAKE:
                print(f"\n{'='*70}\n  [!] Stake {stake:g} (source={stake_source}) exceeds "
                      f"MAX_STAKE {config_env.MAX_STAKE:g} — refusing all wager tests.\n{'='*70}")
                for name in wager_tests:
                    tr = T.TestResult(name, "crash_controls.png")
                    tr.passed = False
                    tr.details = f"Refused: stake {stake:g} exceeds the safety cap {config_env.MAX_STAKE:g}"
                    results.append(tr); print(tr)
            else:
                try:
                    target_mult = float(target) if target else None
                except ValueError:
                    target_mult = None
                print(f"\n{'='*70}\n  LIVE WAGER TESTS — stake={stake:g} (source={stake_source}), "
                      f"target={target_mult}\n{'='*70}")

                # ── WAGER TEST 1: place bet during betting window -> exactly 1 request ──
                _wt_s = time.time() - ctx_start
                await obs.wait_for_phase("BETTING", timeout=45)
                bet_res = await place_bet(page, w_controls, monitor, stake, T.SCREENSHOT_DIR, tag="wager_place")
                t5 = T.TestResult(wager_tests[0], "wager_place_post.png")
                t5.passed = bool(bet_res.get("clicked")) and bet_res.get("request_count") == 1
                t5.details = (f"clicked={bet_res.get('clicked')}, requests={bet_res.get('request_count')}, "
                              f"path={bet_res.get('path')}, status={bet_res.get('status')}")
                t5.video_start, t5.video_end = _wt_s, time.time() - ctx_start
                results.append(t5); print(t5)

                # ── WAGER TEST 2 & 3: cash out mid-flight -> payout=bet*mult; balance updates ──
                _wt_s = time.time() - ctx_start
                t6 = T.TestResult(wager_tests[1], "wager_cashout_post.png")
                t7 = T.TestResult(wager_tests[2], "wager_cashout_post.png")
                if bet_res.get("clicked"):
                    bal_before_bet, _ = await _read_balance(page, T.SCREENSHOT_DIR, "wager_bal_prebet")
                    asc = await obs.wait_for_phase("ASCENDING", timeout=30)
                    if asc:
                        await asyncio.sleep(2)   # let the multiplier build before cashing out
                        mult_at_cashout = (await obs.sample(tag="wager_precashout")).get("multiplier")
                        # Bet -> Cash Out commonly morphs the SAME on-screen control the instant
                        # BETTING ends — the pre-bet w_controls snapshot has no Cash Out entry at
                        # all, so cash_out() would always report no_cashout_button without this.
                        # detect_controls_vision() (not detect_controls()'s DOM-first path):
                        # recon never confirmed whether the morphed button keeps the same CSS
                        # classes it had pre-bet — if it does, DOM matching would keep
                        # mislabeling it "Bet Button" instead of finding "Cash Out".
                        asc_controls, _ = await detect_controls_vision(page, T.SCREENSHOT_DIR, "wager_asc_controls")
                        co_res = await cash_out(page, asc_controls, monitor, T.SCREENSHOT_DIR, tag="wager_cashout")
                        bal_after_cashout, _ = await _read_balance(page, T.SCREENSHOT_DIR, "wager_bal_postcashout")
                        net_payout = co_res.get("net", {}).get("payout")
                        expected = stake * mult_at_cashout if isinstance(mult_at_cashout, (int, float)) else None

                        if co_res.get("clicked") and net_payout is not None and expected is not None:
                            rel_err = abs(net_payout - expected) / max(expected, 0.01)
                            t6.passed = rel_err <= 0.15
                            t6.details = (f"multiplier@cashout={mult_at_cashout}, expected~{expected:.2f}, "
                                         f"network payout={net_payout:.2f} (rel err {rel_err:.1%})")
                        else:
                            t6.passed = None
                            t6.details = (f"cashout clicked={co_res.get('clicked')}, network payout "
                                         f"unavailable (status={co_res.get('status')})")

                        if bal_before_bet is not None and bal_after_cashout is not None:
                            net_delta = bal_after_cashout - bal_before_bet
                            expected_delta = (net_payout - stake) if net_payout is not None else None
                            t7.passed = (expected_delta is not None
                                        and abs(net_delta - expected_delta) <= max(0.05, 0.1 * stake))
                            t7.details = (f"balance {bal_before_bet:g} -> {bal_after_cashout:g} "
                                         f"(delta {net_delta:+.2f}); expected delta {expected_delta}")
                        else:
                            t7.passed = None
                            t7.details = "Could not read balance before/after to compute delta"

                        # Bet record for the deferred transaction-history check (same shape/
                        # intent as the slot pipeline's — Betway's back office reflects bets
                        # ~10-15 min late, so tlogs_validate.py runs this as a second pass, not
                        # part of the live test). "tlogs" starts blank/pending — that pass fills
                        # it in later via dsc_report.update_result(), never here.
                        if dsc_report_path:
                            try:
                                dsc_report.append_record(dsc_report_path, {
                                    "recorded_at": datetime.now().astimezone().isoformat(),
                                    "spin_at": datetime.now().astimezone().isoformat(),
                                    "brand": brand, "region": region, "account": account,
                                    "srNo": 1, "provider": provider or "", "game": game_name or "",
                                    "launch": "Pass", "bet_placed": "Pass" if bet_res.get("clicked") else "Fail",
                                    "tlogs": "",
                                    "wager": stake, "wager_response": net_payout,
                                    "payout": net_payout,
                                    "balance_before": bal_before_bet, "balance_after": bal_after_cashout,
                                    "non_slot": "crash", "evidence": T.RUN_ID or "",
                                })
                                print(f"    [DSC] Bet record appended -> {dsc_report.records_path(dsc_report_path)}")
                            except Exception as e:
                                print(f"    [DSC] [WARN] could not append bet record: {e}")
                    else:
                        t6.passed = t7.passed = None
                        t6.details = t7.details = "ASCENDING phase never observed after bet"
                else:
                    t6.passed = t7.passed = None
                    t6.details = t7.details = "Skipped: bet was not placed"
                t6.video_start = t7.video_start = _wt_s
                t6.video_end = t7.video_end = time.time() - ctx_start
                results.append(t6); print(t6)
                results.append(t7); print(t7)

                # ── WAGER TEST 4: auto cash-out fires at configured target ──
                _wt_s = time.time() - ctx_start
                t8 = T.TestResult(wager_tests[3], "wager_auto_post.png")
                await obs.wait_for_phase("BETTING", timeout=45)
                # detect_controls_vision(), not detect_controls()'s DOM-first path: Auto Cash
                # Out toggle/input aren't in SPRIBE_DOM_SELECTORS yet (unconfirmed markup), and
                # since the Bet button IS found via DOM, detect_controls()'s fallback would
                # never trigger, silently starving this test of the labels it actually needs.
                w_controls, _ = await detect_controls_vision(page, T.SCREENSHOT_DIR, "wager_auto_controls")
                auto_toggle = T.find_control(w_controls, "auto cash out toggle", "auto cashout toggle")
                auto_input = T.find_control(w_controls, "auto cash out input", "auto cashout input")
                if auto_toggle and auto_input and target_mult:
                    try:
                        await page.mouse.click(*auto_input["center"])
                        await page.keyboard.press("Control+A")
                        await page.keyboard.type(f"{target_mult:g}")
                        await page.mouse.click(*auto_toggle["center"])
                    except Exception as e:
                        print(f"    [AUTO] setup warning: {e}")
                    bal_pre_auto, _ = await _read_balance(page, T.SCREENSHOT_DIR, "wager_bal_preauto")
                    bet_res2 = await place_bet(page, w_controls, monitor, stake, T.SCREENSHOT_DIR, tag="wager_auto_bet")
                    crashed = await obs.wait_for_phase("CRASHED", timeout=45) if bet_res2.get("clicked") else None
                    bal_post_auto, _ = await _read_balance(page, T.SCREENSHOT_DIR, "wager_bal_postauto")
                    if bet_res2.get("clicked") and crashed and bal_pre_auto is not None and bal_post_auto is not None:
                        delta = bal_post_auto - bal_pre_auto
                        # Any credit above a pure stake loss implies a cash-out fired automatically.
                        t8.passed = delta > -stake + 0.01
                        t8.details = f"target={target_mult:g}x, balance {bal_pre_auto:g} -> {bal_post_auto:g} (delta {delta:+.2f})"
                    else:
                        t8.passed = None
                        t8.details = "Could not complete an auto cash-out round to judge"
                else:
                    t8.passed = None
                    t8.details = "No auto cash-out control detected, or --target not supplied"
                t8.video_start, t8.video_end = _wt_s, time.time() - ctx_start
                results.append(t8); print(t8)

                # ── WAGER TEST 5: bet rejected when placed mid-flight ──
                _wt_s = time.time() - ctx_start
                t9 = T.TestResult(wager_tests[4], "wager_midflight.png")
                await obs.wait_for_phase("BETTING", timeout=45)
                w_controls, _ = await detect_controls(page, T.SCREENSHOT_DIR, "wager_mf_controls")
                pre_bet = await place_bet(page, w_controls, monitor, stake, T.SCREENSHOT_DIR, tag="wager_mf_setup")
                asc = await obs.wait_for_phase("ASCENDING", timeout=30) if pre_bet.get("clicked") else None
                if asc:
                    # Re-detect now that a round is live — the Bet control commonly morphs into
                    # Cash Out in the same screen slot the instant BETTING ends, so reusing the
                    # pre-bet w_controls risks clicking what is now Cash Out while believing
                    # it's still Bet. detect_controls_vision(), not DOM: this test's entire
                    # premise is judging whether "Bet" still resolves mid-flight, and recon
                    # never confirmed whether the morphed control keeps the same CSS classes
                    # it had pre-bet — if it does, DOM matching would keep reporting "Bet
                    # Button" no matter what the button actually says, silently defeating
                    # this exact check.
                    asc_controls, _ = await detect_controls_vision(page, T.SCREENSHOT_DIR, "wager_mf_asc_controls")
                    bet_btn2 = T.find_control(asc_controls, "bet button", "place bet")
                    idx = monitor.req_count()
                    if bet_btn2:
                        try:
                            await page.mouse.click(*bet_btn2["center"])
                        except Exception:
                            pass
                        req = await _await_request(monitor, idx, timeout=3.0)
                        t9.passed = req is None
                        t9.details = ("No new bet request fired while ASCENDING (correctly rejected)" if req is None
                                     else f"[DEFECT] a bet request fired mid-flight: {req.get('path')}")
                    else:
                        # No Bet-labeled control exists mid-flight at all (it morphed into Cash
                        # Out) — there is nothing on screen a player could click to place a
                        # second bet, which is the strongest form of "rejected."
                        t9.passed = True
                        t9.details = "No Bet control present during ASCENDING (morphed to Cash Out) — nothing to place a bet with"
                    # A real bet landed during the BETTING setup above — close the position
                    # instead of riding it blind to whatever the round crashes at.
                    try:
                        await cash_out(page, asc_controls, monitor, T.SCREENSHOT_DIR, tag="wager_mf_cleanup")
                    except Exception:
                        pass
                else:
                    t9.passed = None
                    t9.details = "Could not reach ASCENDING after a bet to test mid-flight rejection"
                t9.video_start, t9.video_end = _wt_s, time.time() - ctx_start
                results.append(t9); print(t9)

                # ── WAGER TEST 6: cancel bet before round start refunds stake ──
                _wt_s = time.time() - ctx_start
                t10 = T.TestResult(wager_tests[5], "wager_cancel.png")
                await obs.wait_for_phase("BETTING", timeout=45)
                w_controls, _ = await detect_controls(page, T.SCREENSHOT_DIR, "wager_cancel_controls")
                bal_before_cancel, _ = await _read_balance(page, T.SCREENSHOT_DIR, "wager_bal_precancel")
                cancel_bet_res = await place_bet(page, w_controls, monitor, stake, T.SCREENSHOT_DIR, tag="wager_cancel_bet")
                # Cancel Bet commonly only appears once a bet is active (same morph pattern as
                # Bet -> Cash Out) — looking for it in the pre-bet w_controls above would
                # always miss it, so re-detect after the bet lands instead.
                # detect_controls_vision(), not detect_controls()'s DOM-first path: Cancel Bet
                # isn't in SPRIBE_DOM_SELECTORS (unconfirmed markup), and if the post-bet
                # button keeps the same "bet btn-success" classes with just its text changed,
                # DOM would mislabel it "Bet Button" and the fallback would never trigger.
                cancel_btn = None
                if cancel_bet_res.get("clicked"):
                    post_bet_controls, _ = await detect_controls_vision(page, T.SCREENSHOT_DIR, "wager_cancel_postbet_controls")
                    cancel_btn = T.find_control(post_bet_controls, "cancel bet")
                cancelled = False
                if cancel_bet_res.get("clicked") and cancel_btn:
                    await asyncio.sleep(1)
                    try:
                        await page.mouse.click(*cancel_btn["center"])
                        cancelled = True
                    except Exception as e:
                        print(f"    [CANCEL] click warning: {e}")
                    await asyncio.sleep(1.5)
                    bal_after_cancel, _ = await _read_balance(page, T.SCREENSHOT_DIR, "wager_bal_postcancel")
                    if bal_before_cancel is not None and bal_after_cancel is not None:
                        t10.passed = abs(bal_after_cancel - bal_before_cancel) <= 0.01
                        t10.details = (f"balance {bal_before_cancel:g} -> {bal_after_cancel:g} after cancel "
                                      f"(expected unchanged)")
                    else:
                        t10.passed = None
                        t10.details = "Could not read balance before/after cancel"
                elif not cancel_btn:
                    t10.passed = None
                    t10.details = "No Cancel Bet control detected"
                else:
                    t10.passed = None
                    t10.details = "Skipped: bet was not placed"
                if cancel_bet_res.get("clicked") and not cancelled:
                    try:
                        await obs.wait_for_phase("ASCENDING", timeout=30)
                        asc_controls, _ = await detect_controls_vision(page, T.SCREENSHOT_DIR, "wager_cancel_asc_controls")
                        await cash_out(page, asc_controls, monitor, T.SCREENSHOT_DIR, tag="wager_cancel_cleanup")
                    except Exception:
                        pass
                t10.video_start, t10.video_end = _wt_s, time.time() - ctx_start
                results.append(t10); print(t10)

                # ── WAGER TEST 7: rapid double-click Bet = still 1 bet ──
                _wt_s = time.time() - ctx_start
                t11 = T.TestResult(wager_tests[6], "wager_doubleclick.png")
                await obs.wait_for_phase("BETTING", timeout=45)
                w_controls, _ = await detect_controls(page, T.SCREENSHOT_DIR, "wager_dc_controls")
                bet_btn3 = T.find_control(w_controls, "bet button", "place bet")
                if bet_btn3:
                    idx = monitor.req_count()
                    try:
                        await page.mouse.click(*bet_btn3["center"])
                        await page.mouse.click(*bet_btn3["center"])
                    except Exception as e:
                        print(f"    [DBLCLICK] warning: {e}")
                    req = await _await_request(monitor, idx, timeout=3.0)
                    await asyncio.sleep(1.5)   # let any duplicate/delayed request land before counting
                    count = _count_requests(monitor, req["path"], idx) if req else 0
                    t11.passed = count == 1
                    t11.details = f"bet requests observed after rapid double-click: {count}"
                    if req:
                        # A real bet landed — close the position rather than leaving it to ride blind.
                        try:
                            await obs.wait_for_phase("ASCENDING", timeout=30)
                            asc_controls, _ = await detect_controls_vision(page, T.SCREENSHOT_DIR, "wager_dc_asc_controls")
                            await cash_out(page, asc_controls, monitor, T.SCREENSHOT_DIR, tag="wager_dc_cleanup")
                        except Exception:
                            pass
                else:
                    t11.passed = None
                    t11.details = "No Bet button detected"
                t11.video_start, t11.video_end = _wt_s, time.time() - ctx_start
                results.append(t11); print(t11)

        # ── teardown: attach video via the shared finalizer ──
        try:
            await T.finalize_media(page, results, recordings_dir)
        except Exception as e:
            print(f"  finalize_media warning: {e}")
        try:
            await context.close()
        except Exception:
            pass
        await browser.close()

    T._emit_report(results)
    return results


def _setup_run_dir(run_dir):
    """Mirror test_spin_button.__main__: repoint the shared report/screenshot globals."""
    T.RUN_STARTED_AT = datetime.now().isoformat()
    T.RUN_START_TS = time.time()
    if run_dir:
        T.RUN_DIR = os.path.abspath(run_dir)
        T.RUN_ID = os.path.basename(T.RUN_DIR.rstrip(os.sep))
        T.SCREENSHOT_DIR = os.path.join(T.RUN_DIR, "screenshots")
    os.makedirs(T.SCREENSHOT_DIR, exist_ok=True)


def resolve_launch_url(game, username, password, brand="betway", region="ZA", category=None):
    """Resolve a crash game's launch URL at runtime — the SAME flow the slot suite uses:
    authenticate -> search the casino catalog -> fetch the launch URL. Returns
    (launch_url, provider, min_bet) or exits with a clear message. Reuses the region-aware
    modules verbatim. `provider` and `min_bet` (the catalog's own minBetAmount, same field
    test_spin_button.py reads for slots) are threaded through so run_crash_tests can wager the
    game's own minimum automatically (see resolve_stake) — no second lookup needed.

    `category` is the launch API's routing field. Confirmed 2026-08-08 by capturing a real
    browser launch of Aviator (Spribe, gameId 1322) on betway.co.za: the site's own POST to
    /Gaming/launch sends `"category":"crashgames"`, not the slot value 'redtigerroyal' that
    IframeHandler defaults to. Defaulting to 'crashgames' here for all crash titles; `--category`
    still overrides it for recon on a title that turns out to need something else.
    """
    from modules.auth_handler import AuthHandler
    from modules.game_handler import GameHandler
    from modules.iframe_handler import IframeHandler

    print(f"\n{'='*70}\n  CRASH GAME RESOLVER\n{'='*70}")
    print(f"Brand: {brand} | Region: {region}")
    print(f"Authenticating (user: {username})...")
    auth_res = AuthHandler().authenticate(username, password, brand=brand, region=region)
    if not auth_res.get("success"):
        print(f"❌ Auth failed: {auth_res.get('message')}")
        sys.exit(1)
    token = auth_res["token"]
    print("✅ Authenticated.\n")

    info = GameHandler().search_game(game, token, brand=brand, region=region)
    if not info:
        print(f"❌ Game not found in catalog: {game}")
        sys.exit(1)
    gid = info.get("id")
    print(f"✅ Found: {game} (id={gid}, type={info.get('game_type')}, provider={info.get('provider')})")

    kwargs = {"brand": brand, "region": region, "category": category or "crashgames"}
    if category:
        print(f"   Overriding launch category -> {category!r} (recon)")
    try:
        launch_url = IframeHandler().get_iframe_url(gid, token, **kwargs)
    except Exception as e:
        print(f"❌ Launch URL fetch failed: {e}")
        sys.exit(1)
    if not launch_url:
        print("❌ No launch URL returned for this game.")
        sys.exit(1)
    print(f"🔗 Launch URL: {launch_url[:80]}...")
    return launch_url, info.get("provider"), info.get("minBetAmount")


async def run_crash_tests_with_retry(resolve_fn, max_retries=3, retry_cooldown=20, **kwargs):
    """Run run_crash_tests up to max_retries times, re-resolving a FRESH launch URL each retry
    via resolve_fn() — a full retry (new session, new browser), not just a page reload.

    Why: the 2026-08-03 provider validation sweep showed the SAME game flip between a clean
    detection pass and a session-drop/weak-detection failure across different single attempts
    on IDENTICAL code (Split-the-Pot, Incentive-Gaming, Playtech-Live, Aviatrix all did this).
    A single attempt materially understates what this pipeline can actually detect for a given
    provider — vision-model detection has real run-to-run variance, and a fresh session isn't
    guaranteed to hit the same transient session-lock twice. Retrying gives a fairer read
    instead of recording a false negative off one unlucky attempt.

    DRY-RUN ONLY (kwargs["live"] must be falsy) — enforced by the caller, not here, since this
    function has no visibility into whether an earlier attempt's wager block already placed a
    real bet before some later thing failed. Retrying THAT would risk a second real wager on
    the same intended stake. Live mode keeps its original single-attempt behavior untouched.

    'Good enough to stop' = the run did not abort (no session drop at pre_check, which always
    happens before TEST 1 and thus before any wager action could occur) AND TEST 1 (control
    detection) passed. A weak/incomplete round-lifecycle read (TEST 2/3) does NOT trigger a
    retry by itself — that's a round-timing race outside this code's control, not a defect a
    fresh attempt reliably fixes, and every retry costs a full multi-minute browser session.

    resolve_fn: zero-arg callable returning (launch_url, provider, min_bet); may raise
    SystemExit (as resolve_launch_url does on a real resolution failure) — caught here as a
    failed attempt on any try but the last, where it propagates same as a non-retried run
    would. `min_bet` (the catalog's minBetAmount) is threaded into kwargs the same way
    `provider` already is, unless an explicit --min-bet already won.
    """
    results = None
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            print(f"\n{'#'*70}\n  RETRY ATTEMPT {attempt}/{max_retries} (fresh launch URL)\n{'#'*70}")
        try:
            url, provider, catalog_min_bet = resolve_fn()
        except SystemExit:
            print(f"  [RETRY] attempt {attempt}/{max_retries} could not resolve a launch URL.")
            if attempt == max_retries:
                raise
            await asyncio.sleep(retry_cooldown)
            continue
        if provider and not kwargs.get("provider"):
            kwargs["provider"] = provider
        if catalog_min_bet and not kwargs.get("min_bet"):
            kwargs["min_bet"] = catalog_min_bet
        results = await run_crash_tests(url, **kwargs)
        aborted = any(r.name == "Crash game session is live" for r in results)
        t1 = next((r for r in results if r.name == "Crash UI controls detected"), None)
        detection_ok = bool(t1 and t1.passed)
        if not aborted and detection_ok:
            if attempt > 1:
                print(f"\n  [RETRY] Succeeded on attempt {attempt}/{max_retries}.")
            return results
        reason = "session dropped" if aborted else "weak/failed control detection"
        print(f"\n  [RETRY] attempt {attempt}/{max_retries} — {reason}.")
        if attempt < max_retries:
            print(f"  [RETRY] retrying with a fresh launch URL in {retry_cooldown}s...")
            await asyncio.sleep(retry_cooldown)
    print(f"\n  [RETRY] All {max_retries} attempts exhausted — keeping the last attempt's result.")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Crash Game UI Test Suite")
    parser.add_argument("url", nargs="?", default=None, help="Direct crash-game launch URL")
    parser.add_argument("--game", type=str,
                        help="Crash game name to resolve at RUNTIME (auth -> search -> launch URL), "
                             "exactly like the slot suite. Requires --username/--password.")
    parser.add_argument("--username", type=str, default="", help="Account username (for --game)")
    parser.add_argument("--password", type=str, default="", help="Account password (for --game)")
    parser.add_argument("--brand", type=str, default="betway", help="Brand: betway | jackpotcity")
    parser.add_argument("--region", type=str, default="ZA", help="Region code (ZA, GH, NG, ...)")
    parser.add_argument("--category", type=str, default="",
                        help="Override the launch API 'category' field (default sends the slot "
                             "value 'redtigerroyal' unchanged; use this for recon against real "
                             "crash titles until a live launch payload confirms the right value)")
    parser.add_argument("--live", action="store_true",
                        help="Run wager-dependent tests (PLACES REAL BETS). Default is safe dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run (default behaviour)")
    parser.add_argument("--bet", type=str, default="",
                        help="Operator-typed stake OVERRIDE for live tests — wins over the "
                             "catalog minimum and in-game floor detection when set. Optional: "
                             "leave unset to auto-wager the game's own minimum bet.")
    parser.add_argument("--min-bet", type=str, default="",
                        help="Known catalog minimum bet for this game (auto-filled when --game "
                             "resolves it; pass explicitly alongside a raw URL to skip in-game "
                             "floor detection). Used only when --bet is not set.")
    parser.add_argument("--target", type=str, default="", help="Auto cash-out target multiplier (e.g. 1.5)")
    parser.add_argument("--rounds", type=int, default=2, help="Rounds to observe")
    parser.add_argument("--mobile", action="store_true", help="Mobile viewport (iPhone 13)")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    parser.add_argument("--run-dir", type=str, default="", help="Per-run artifact folder")
    parser.add_argument("--retries", type=int, default=1,
                        help="Dry-run only: retry up to N times (fresh launch URL each try) if "
                             "the run aborts (session drop) or TEST 1 control detection fails. "
                             "Default 1 = no retry. Ignored under --live (see "
                             "run_crash_tests_with_retry's docstring for why).")
    parser.add_argument("--retry-cooldown", type=float, default=20,
                        help="Seconds to wait between retries (default 20 — same reasoning as "
                             "the crash-sweep fleet scheduler's inter-launch cooldown).")
    args = parser.parse_args()

    # A direct URL wins; otherwise resolve it at runtime from --game (just like slots).
    launch_url = args.url
    provider = None
    catalog_min_bet = None
    if not launch_url and args.game:
        if not args.username or not args.password:
            print("Runtime resolution needs --username and --password (or pass a direct URL).")
            sys.exit(1)
        launch_url, provider, catalog_min_bet = resolve_launch_url(
            args.game, args.username, args.password,
            brand=args.brand, region=args.region, category=args.category or None)
    if not launch_url:
        parser.print_help()
        sys.exit(1)

    _setup_run_dir(args.run_dir)
    live = args.live and not args.dry_run
    if live:
        print("\n[!] LIVE MODE: wager-dependent tests will place REAL bets.\n")

    max_retries = args.retries if (not live and args.retries > 1) else 1
    # Attempt 1 reuses the launch_url/provider/min_bet already resolved above (no point
    # re-resolving immediately); a --game retry re-resolves fresh from attempt 2 onward.
    _used_first = []
    if args.game:
        def _resolve():
            if _used_first:
                return resolve_launch_url(args.game, args.username, args.password,
                                          brand=args.brand, region=args.region,
                                          category=args.category or None)
            _used_first.append(True)
            return launch_url, provider, catalog_min_bet
    else:
        def _resolve():
            return launch_url, provider, catalog_min_bet

    asyncio.run(run_crash_tests_with_retry(
        _resolve, max_retries=max_retries, retry_cooldown=args.retry_cooldown,
        live=live, bet=args.bet, min_bet=args.min_bet or catalog_min_bet, target=args.target,
        rounds=args.rounds, mobile=args.mobile, headless=args.headless,
        account=args.username or None, brand=args.brand, region=args.region,
        provider=provider, game_name=args.game or None,
    ))
