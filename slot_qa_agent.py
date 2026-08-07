"""
slot_qa_agent.py — AGENTIC QA CONTROLLER.

The agent (a Gemini "QA brain") drives the WHOLE slot checklist per game:
    observe (screenshot + detected controls + network/balance evidence)
      -> decide (which checklist item to work next, which TOOL to call)
        -> act (a hardened, verified tool does the operation + measurement)
          -> judge (verdicts per checklist item, grounded in the tool's evidence)

Design guarantees (see plan):
  • The agent decides/sequences/operates/judges — "full agent control".
  • Execution + measurement live in VERIFIED tools (network-truth spins, motion, etc.), so
    verdicts are evidence-backed, not blind vision.
  • Money/exit is hard-blocked deterministically — no tool ever clicks buy/deposit/withdraw,
    and autoplay keeps its aggressive ~2-spin stop. Safe on a funded account.
  • Core money items (wager/payout/balance/server) are resolved DETERMINISTICALLY by the spin
    tool, and a safety net runs one spin at the end if the brain never did.

All primitives are reused from slot_spin / slot_agent / test_spin_button — nothing re-implemented.
"""
import os
import time
import asyncio
from PIL import Image

import test_spin_button as T          # gemini_call, parse_gemini_json, detect_controls_merged, …
import slot_spin                       # spin_and_measure, UnifiedGameMonitor, parse_result_body
import slot_agent                      # drive_autoplay, examine_panel, examine_root_options, …
from slot_explore import _cfg          # JSON GenerateContentConfig
import config_env                      # clamp_point

MAX_STEPS = 26                         # hard bound on brain iterations (bounds time + tokens)

# ── Checklist (slot-scoped). `cap` None = always run; else gated by the UI's test-selection
#    ("bet"/"autoplay"/"menu"/"paytable"). `auto` = resolved deterministically by a tool's evidence
#    (the brain need not judge it). The brain still drives WHEN each is exercised. ──
CHECKLIST = [
    {"id": "launch",     "cap": None,       "name": "Game launches & loads",
     "goal": "the game reached a playable state in the iframe"},
    {"id": "controls",   "cap": None,       "name": "All UI controls detected",
     "goal": "the main controls (spin, bet +/-, balance, menu...) are present"},
    {"id": "default_bet","cap": None,       "name": "Default bet amount",
     "goal": "read and report the default stake shown after launch"},
    {"id": "wager",      "cap": None,       "name": "Wager recorded & deducted",
     "goal": "spinning deducts the stake from the wallet (network-verified)"},
    {"id": "payout",     "cap": None,       "name": "Payout recorded & credited",
     "goal": "any win is recorded in the spin response and credited"},
    {"id": "feature",    "cap": None,       "name": "Feature trigger monitored",
     "goal": "detect whether a bonus/free-spins feature triggered"},
    {"id": "balance",    "cap": None,       "name": "Balance updates",
     "goal": "balance changes correctly = before - wager + payout"},
    {"id": "server",     "cap": None,       "name": "Valid server response",
     "goal": "the spin endpoint returns a well-formed response"},
    {"id": "spin_lock",  "cap": None,       "name": "Spin locked during spin",
     "goal": "rapid clicks during a spin do NOT fire extra spins"},
    {"id": "bet_change", "cap": "bet",      "name": "Bet +/- changes stake",
     "goal": "the - / + buttons change the bet value"},
    {"id": "bet_restore","cap": "bet",      "name": "Bet round-trip",
     "goal": "the bet can be returned to its original value"},
    {"id": "autoplay",   "cap": "autoplay", "name": "Autoplay starts & stops",
     "goal": "autoplay can be configured, started, and stopped"},
    {"id": "menu",       "cap": "menu",     "name": "Menu / settings / history",
     "goal": "the menu (or on-screen nav) opens and its options are examined"},
    {"id": "paytable",   "cap": "paytable", "name": "Paytable / info",
     "goal": "the paytable / info pages can be opened and paged"},
    {"id": "audio",      "cap": None,       "name": "Audio toggle",
     "goal": "the sound/music control toggles audio on/off"},
    {"id": "ui",         "cap": None,       "name": "UI renders correctly",
     "goal": "the game renders cleanly in the frame (no cut-off/broken layout)"},
]

# Tools the brain may call. Keep this list and the TOOLS dict in sync.
TOOL_SPEC = {
    "spin":          "Do ONE real spin; network-verifies wager/payout/balance/feature/server.",
    "spam_spin":     "Click spin, then rapidly click again mid-spin to test the spin lock.",
    "change_bet":    "Change the stake via the -/+ buttons. args: {direction:'down'|'up', times:int}.",
    "restore_bet":   "Return the stake toward its original value (opposite direction).",
    "autoplay":      "Open the autoplay panel, configure fewest spins, start, then stop (≤2 spins).",
    "open_menu":     "Open the menu / on-screen nav and examine its options (history/settings/help).",
    "open_paytable": "Open the paytable/info and page through it.",
    "toggle_audio":  "Toggle the sound/music control and confirm it changed.",
    "read_state":    "Read the current balance and bet from the screen.",
    "detect":        "Re-detect the on-screen controls (refresh the inventory).",
    "done":          "Finish — every applicable item is judged or cannot be exercised.",
}


# ───────────────────────────── context ─────────────────────────────
class QAContext:
    """Live handles + helpers shared by every tool."""
    def __init__(self, page, monitor, ss_dir, region, t0, spin_center=None):
        self.page = page
        self.monitor = monitor
        self.ss_dir = ss_dir
        self.region = region
        self.t0 = t0                  # context_start_time, so clock() aligns with the session video
        self.spin_center = spin_center  # KNOWN good spin coord (from the prereq calibration) — avoids
                                         # flaky re-detection that made core spin checks fail/fabricate
        self.controls = []
        self.last_bet_dir = None       # last change_bet direction, so restore goes the opposite way
        self._i = 0

    def clock(self):
        return round(time.time() - self.t0, 1)

    async def shot(self, name):
        self._i += 1
        p = os.path.join(self.ss_dir, f"qa_{self._i:02d}_{name}.png")
        await self.page.screenshot(path=p)
        return p

    async def refresh_controls(self, passes=2):
        p = await self.shot("scan")
        self.controls = T.detect_controls_merged(Image.open(p), passes=passes)
        return p

    def find(self, *keywords):
        return T.find_control(self.controls, *keywords)

    def spin_target(self):
        """Prefer the KNOWN spin coordinate from the prereq calibration; fall back to detection."""
        if self.spin_center:
            return {"center": self.spin_center, "label": "Spin Button"}
        return self.find("spin")

    async def dismiss(self, tries=2):
        """Escape any open overlay so a tool starts from the base game (prevents e.g. the autoplay
        panel leaking into the menu check)."""
        for _ in range(tries):
            try:
                await self.page.keyboard.press("Escape"); await asyncio.sleep(0.4)
            except Exception:
                break


def _v(item_id, passed, details):
    return {"id": item_id, "passed": passed, "details": details}


# ───────────────────────────── tools ─────────────────────────────
# Each tool returns {"summary": str, "shot": filename, "verdicts": [..], "data": {..}}.
async def tool_spin(ctx, **_):
    await ctx.dismiss()   # spin from the base game, not over a leftover panel
    spin = ctx.spin_target()
    if not (spin and spin.get("center")):
        return {"summary": "no spin button found", "shot": None,
                "verdicts": [_v("wager", None, "spin button not detected")], "data": {}}
    rep = await slot_spin.spin_and_measure(ctx.page, spin["center"], ctx.monitor, ctx.ss_dir,
                                           tag="qa_spin", region=ctx.region)
    v = rep.get("values", {})
    src = v.get("source")
    shot = os.path.basename(rep.get("shots", {}).get("result") or rep.get("shots", {}).get("pre") or "")
    wager, payout = v.get("wager"), v.get("payout")
    bb, ba = v.get("balance_before"), v.get("balance_after")
    verdicts = []
    verdicts.append(_v("wager", wager is not None and wager > 0,
                       f"wager={wager} ({src})" if wager is not None else "no wager captured"))
    verdicts.append(_v("payout", payout is not None,
                       f"payout={payout}, win={v.get('win')} ({src})" if payout is not None
                       else "payout not captured"))
    verdicts.append(_v("feature", True,
                       f"feature={v.get('feature')}"
                       + (f" ({v.get('feature_name')})" if v.get('feature_name') else "")))
    # balance math: after ≈ before - wager + payout
    if bb is not None and ba is not None and wager is not None:
        exp = bb - wager + (payout or 0)
        ok = abs(ba - exp) < max(0.01, 0.02 * max(1.0, abs(exp)))
        verdicts.append(_v("balance", ok, f"{bb} -> {ba} (expected {round(exp,2)}, {src})"))
    elif ba is not None and bb is not None:
        verdicts.append(_v("balance", ba != bb, f"{bb} -> {ba}"))
    # server response validity (deterministic)
    sv = _server_verdict(ctx)
    if sv:
        verdicts.append(sv)
    return {"summary": f"spin: wager={wager} payout={payout} bal {bb}->{ba} feature={v.get('feature')} src={src}",
            "shot": shot, "verdicts": verdicts, "data": v}


def _server_verdict(ctx):
    ep = getattr(ctx.monitor, "spin_endpoint", None)
    resps = [r for r in getattr(ctx.monitor, "_all_responses", [])
             if ep and r.get("path") == ep]
    if not resps:
        return _v("server", None, "no spin response captured")
    last = resps[-1]
    body = last.get("body") or ""
    try:
        import json
        json.loads(body); valid = True
    except Exception:
        valid = bool(body)   # non-JSON but non-empty (e.g. WS) still counts as a response
    return _v("server", bool(last.get("status", 0) and last["status"] < 400 and valid),
              f"HTTP {last.get('status')}, parseable={valid}")


async def tool_spam_spin(ctx, **_):
    await ctx.dismiss()
    spin = ctx.spin_target()
    if not (spin and spin.get("center")):
        return {"summary": "no spin button", "shot": None,
                "verdicts": [_v("spin_lock", None, "spin button not detected")], "data": {}}
    since = ctx.monitor.req_count()
    pt = config_env.clamp_point(*spin["center"])
    await ctx.page.mouse.click(*pt); await asyncio.sleep(0.25)
    for _ in range(8):                      # spam mid-spin
        await ctx.page.mouse.click(*pt); await asyncio.sleep(0.12)
    await asyncio.sleep(1.0)
    shot = os.path.basename(await ctx.shot("spamspin"))
    spins = slot_agent.count_autospins(ctx.monitor, since)[0]
    # 1 spin from many clicks = properly locked; >1 = not locked (a real finding, not a tool bug)
    return {"summary": f"9 clicks -> {spins} spin(s)", "shot": shot,
            "verdicts": [_v("spin_lock", spins <= 1,
                            f"9 rapid clicks produced {spins} spin(s)"
                            + ("" if spins <= 1 else " — not locked"))],
            "data": {"spins": spins}}


async def _read_bet(ctx, name):
    p = await ctx.shot(name)
    vals = T.read_game_values(Image.open(p))
    return T.parse_amount(vals.get("bet", "")), vals.get("bet", ""), p


async def tool_read_state(ctx, **_):
    p = await ctx.shot("state")
    vals = T.read_game_values(Image.open(p))
    bet = T.parse_amount(vals.get("bet", ""))
    return {"summary": f"balance={vals.get('balance')} bet={vals.get('bet')}",
            "shot": os.path.basename(p),
            "verdicts": [_v("default_bet", bet is not None, f"default bet = {vals.get('bet')}")],
            "data": {"balance": vals.get("balance"), "bet": vals.get("bet")}}


async def tool_change_bet(ctx, direction="down", times=3, _verdict="bet_change", **_):
    btn = ctx.find("bet decrement", "bet -", "decrease") if direction == "down" \
        else ctx.find("bet increment", "bet +", "increase")
    if not (btn and btn.get("center")):
        return {"summary": f"no bet {direction} button", "shot": None,
                "verdicts": [_v(_verdict, None, f"bet {direction} button not detected")], "data": {}}
    before, before_txt, _ = await _read_bet(ctx, "bet_before")
    pt = config_env.clamp_point(*btn["center"])
    for _ in range(max(1, int(times))):
        await ctx.page.mouse.click(*pt); await asyncio.sleep(0.5)
    after, after_txt, p = await _read_bet(ctx, "bet_after")
    changed = (before is not None and after is not None and after != before)
    if _verdict == "bet_change":
        ctx.last_bet_dir = direction        # remember so restore goes the opposite way
    return {"summary": f"bet {before_txt} -> {after_txt} via {direction}", "shot": os.path.basename(p),
            "verdicts": [_v(_verdict, changed, f"{before_txt} -> {after_txt} via {direction}")],
            "data": {"before": before, "after": after}}


async def tool_restore_bet(ctx, direction=None, times=3, **_):
    # Restore = OPPOSITE of the last change (if we changed UP, go DOWN to return).
    if direction is None:
        direction = "down" if ctx.last_bet_dir == "up" else "up"
    r = await tool_change_bet(ctx, direction=direction, times=times, _verdict="bet_restore")
    return {"summary": "restore " + r["summary"], "shot": r["shot"],
            "verdicts": r["verdicts"], "data": r.get("data", {})}


async def tool_autoplay(ctx, **_):
    await ctx.refresh_controls(passes=1)
    ap = ctx.find("autoplay", "autospin", "auto")
    if not (ap and ap.get("center")):
        return {"summary": "no autoplay control", "shot": None,
                "verdicts": [_v("autoplay", None, "autoplay control not detected")], "data": {}}
    a = await slot_agent.drive_autoplay(ctx.page, ap["center"], ctx.monitor, ctx.ss_dir, tag="qa_auto")
    shot = os.path.basename(a.get("shots", {}).get("running") or a.get("shots", {}).get("panel") or "")
    ok = bool(a.get("started") and a.get("stopped"))
    note = ("; ".join(a.get("notes", [])))
    return {"summary": f"autoplay started={a.get('started')} stopped={a.get('stopped')} "
                       f"spins={a.get('spins_observed')}", "shot": shot,
            "verdicts": [_v("autoplay", ok,
                            f"started={a.get('started')}, spins={a.get('spins_observed')}, "
                            f"stopped={a.get('stopped')}" + (f" | {note}" if note else ""))],
            "data": a}


async def tool_open_menu(ctx, **_):
    await ctx.dismiss()                 # close any leftover panel (e.g. autoplay) before opening menu
    await ctx.refresh_controls(passes=1)
    mn = ctx.find("menu", "settings", "hamburger")
    if mn and mn.get("center"):
        pan = await slot_agent.examine_panel(ctx.page, "Menu", mn["center"], ctx.ss_dir,
                                             tag="qa_menu", max_depth=1)
        if not pan.get("opened"):
            pan = await slot_agent.examine_root_options(ctx.page, ctx.ss_dir, tag="qa_rootmenu")
    else:
        pan = await slot_agent.examine_root_options(ctx.page, ctx.ss_dir, tag="qa_rootmenu")
    opts = pan.get("options", [])
    shot = pan.get("shots", {}).get("panel") or ""
    labels = [o.get("label") for o in opts]
    return {"summary": f"menu opened={pan.get('opened')} options={labels}", "shot": shot,
            "verdicts": [_v("menu", bool(pan.get("opened")),
                            f"{len(opts)} option(s): {labels}" if opts
                            else "; ".join(pan.get("notes", [])) or "did not open")],
            "data": pan}


async def tool_open_paytable(ctx, **_):
    await ctx.dismiss()
    await ctx.refresh_controls(passes=1)
    pt = ctx.find("paytable", "info", "pays", "help", "rules")
    if not (pt and pt.get("center")):
        return {"summary": "no paytable control", "shot": None,
                "verdicts": [_v("paytable", None, "paytable/info control not detected")], "data": {}}
    res = await slot_agent.page_through_paytable(ctx.page, pt["center"], ctx.ss_dir, tag="qa_pt")
    pages = res.get("pages", [])
    return {"summary": f"paytable opened={res.get('opened')} pages={len(pages)}",
            "shot": pages[0] if pages else "",
            "verdicts": [_v("paytable", bool(res.get("opened")),
                            f"{len(pages)} page(s) captured")], "data": res}


async def tool_toggle_audio(ctx, **_):
    snd = ctx.find("sound", "volume", "mute", "speaker", "audio")
    if not (snd and snd.get("center")):
        return {"summary": "no sound control", "shot": None,
                "verdicts": [_v("audio", None, "sound control not detected")], "data": {}}
    before = await ctx.shot("audio_before")
    await ctx.page.mouse.click(*config_env.clamp_point(*snd["center"])); await asyncio.sleep(0.8)
    after = await ctx.shot("audio_after")
    changed = slot_spin.frame_motion(before, after) > 2.0
    # toggle back so we leave audio as we found it
    await ctx.page.mouse.click(*config_env.clamp_point(*snd["center"])); await asyncio.sleep(0.4)
    # Verdict reflects REALITY: pass only if the click visibly changed the sound control's state.
    return {"summary": f"sound toggled (changed={changed})", "shot": os.path.basename(after),
            "verdicts": [_v("audio", bool(changed),
                            "sound control toggled and its icon changed" if changed
                            else "clicked the sound control but no visible state change")],
            "data": {"changed": changed}}


async def tool_detect(ctx, **_):
    p = await ctx.refresh_controls(passes=2)
    labels = [c.get("label") for c in ctx.controls]
    return {"summary": f"controls: {labels}", "shot": os.path.basename(p),
            "verdicts": [_v("controls", len(ctx.controls) >= 3, f"{len(ctx.controls)} controls: {labels}")],
            "data": {"controls": labels}}


TOOLS = {
    "spin": tool_spin, "spam_spin": tool_spam_spin, "change_bet": tool_change_bet,
    "restore_bet": tool_restore_bet, "autoplay": tool_autoplay, "open_menu": tool_open_menu,
    "open_paytable": tool_open_paytable, "toggle_audio": tool_toggle_audio,
    "read_state": tool_read_state, "detect": tool_detect,
}


# ───────────────────────────── the brain ─────────────────────────────
def plan_qa_step(obs_shot, items, history):
    """ONE Gemini call: given the checklist state + the current screen + recent evidence, pick the
    next tool and record any verdicts now justified. Returns {thought, verdicts:[..], next:{tool,args}}."""
    pending = [i for i in items if i["status"] == "pending"]
    state_lines = "\n".join(
        f"- [{i['status'].upper()}] {i['id']}: {i['name']} — {i['goal']}" for i in items)
    tools_lines = "\n".join(f"- {k}: {v}" for k, v in TOOL_SPEC.items())
    hist_lines = "\n".join(f"  step {h['step']}: {h['tool']} -> {h['summary']}" for h in history[-6:]) \
        or "  (none yet)"
    prompt = f"""You are the QA lead driving an automated test of a slot game. Work the CHECKLIST by
choosing ONE tool at a time, then judging items from the evidence the tools return. You see the
current game screen (image). The tools do the real, verified operations and measurement.

CHECKLIST (status / id / goal):
{state_lines}

TOOLS you can call next:
{tools_lines}

RECENT ACTIONS & evidence:
{hist_lines}

RULES:
- Pick the single best `next` tool to make progress on a PENDING item. Prefer `spin` early (it
  resolves wager/payout/feature/balance/server at once). Use `change_bet` then `restore_bet` for the
  bet items; `autoplay`/`open_menu`/`open_paytable`/`toggle_audio` for those. `detect`/`read_state`
  to refresh info.
- Record `verdicts` ONLY for items you can justify from the evidence above (especially the `ui` item,
  which you judge from the screen: is the game rendered cleanly, not cut off / broken?). Do NOT
  invent results for items a tool hasn't exercised yet.
- Money is auto-protected; never worry about buy/deposit. When every PENDING item is either judged
  or cannot be exercised on this game, set next.tool = "done".

Return ONLY JSON:
{{"thought": "one line",
  "verdicts": [{{"id": "<checklist id>", "passed": true|false, "details": "evidence-based reason"}}],
  "next": {{"tool": "<tool name>", "args": {{}}}}}}"""
    try:
        data = T.parse_gemini_json(T.gemini_call([Image.open(obs_shot), prompt], _cfg()))
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"    [QA-AGENT] brain error: {e}")
    # Fallback: deterministically advance to the next obvious tool for a pending item.
    nxt = "done"
    order = {"wager": "spin", "payout": "spin", "balance": "spin", "feature": "spin", "server": "spin",
             "spin_lock": "spam_spin", "bet_change": "change_bet", "bet_restore": "restore_bet",
             "autoplay": "autoplay", "menu": "open_menu", "paytable": "open_paytable",
             "audio": "toggle_audio", "default_bet": "read_state", "controls": "detect"}
    for i in pending:
        if order.get(i["id"]):
            nxt = order[i["id"]]; break
    return {"thought": "fallback", "verdicts": [], "next": {"tool": nxt, "args": {}}}


# ───────────────────────────── the loop ─────────────────────────────
def _resolve(items, item_id, passed, details, shot, v0, v1):
    for it in items:
        if it["id"] != item_id:
            continue
        # Resolve when pending, OR upgrade a prior SKIP (None) to a DEFINITIVE pass/fail — so a
        # failed early attempt (e.g. spin button momentarily not detected) doesn't permanently
        # lock the item when a later real measurement succeeds. Never downgrade a definitive verdict.
        if it["status"] == "pending" or (it["status"] == "skip" and passed is not None):
            it["status"] = "pass" if passed else ("fail" if passed is False else "skip")
            it["passed"] = passed
            it["details"] = details
            it["shot"] = shot or it.get("shot", "")
            it["v0"], it["v1"] = v0, v1
            return True
        return False
    return False


# The brain may only JUDGE genuinely subjective items from the screenshot. Everything measurable
# (money, server, spins, bets, panels) must be resolved by a verified TOOL — the brain cannot
# fabricate a wager/payout/balance/etc. verdict from vision.
_BRAIN_VERDICT_OK = {"ui"}


async def run_qa(page, monitor, ss_dir, region="ZA", caps=None, context_start_time=0.0, spin_center=None):
    """Drive the checklist agentically. Returns a list of TestResult (one per applicable item)."""
    on = (lambda c: True) if caps is None else (lambda c: c is None or c in caps)
    items = [dict(it, status="pending", passed=None, details="", shot="", v0=None, v1=None)
             for it in CHECKLIST if on(it["cap"])]

    # Prereq facts already true by the time we're here (startup + control detection ran).
    _resolve(items, "launch", True, "game reached a playable state (startup ok)", "", 0.0, 0.0)

    ctx = QAContext(page, monitor, ss_dir, region, context_start_time, spin_center=spin_center)
    await ctx.refresh_controls(passes=2)
    _resolve(items, "controls", len(ctx.controls) >= 3,
             f"{len(ctx.controls)} controls: {[c.get('label') for c in ctx.controls]}", "", 0.0, ctx.clock())

    history = []
    for step in range(MAX_STEPS):
        if all(i["status"] != "pending" for i in items):
            break
        obs = await ctx.shot("obs")
        obs_t = ctx.clock()
        decision = plan_qa_step(obs, items, history)
        # brain verdicts: accept ONLY for subjective items (UI). Measurable items are tool-resolved
        # so the brain can't fabricate a money/server/spin result from vision.
        for v in decision.get("verdicts", []) or []:
            if v.get("id") in _BRAIN_VERDICT_OK:
                _resolve(items, v.get("id"), v.get("passed"), v.get("details", ""),
                         os.path.basename(obs), obs_t, ctx.clock())
        nxt = (decision.get("next") or {})
        tool = nxt.get("tool")
        print(f"  [QA-AGENT] step {step}: tool={tool} | {decision.get('thought','')}")
        if tool in (None, "done") or tool not in TOOLS:
            if tool == "done" or tool is None:
                break
            history.append({"step": step, "tool": tool, "summary": "unknown tool"}); continue
        # re-detect before tools that resolve a target by label
        if tool in ("spin", "spam_spin", "change_bet", "restore_bet"):
            await ctx.refresh_controls(passes=1)
        t0 = ctx.clock()
        try:
            res = await TOOLS[tool](ctx, **(nxt.get("args") or {}))
        except Exception as e:
            print(f"    [QA-AGENT] tool {tool} error: {e}")
            history.append({"step": step, "tool": tool, "summary": f"error: {e}"}); continue
        t1 = ctx.clock()
        for v in res.get("verdicts", []) or []:
            _resolve(items, v.get("id"), v.get("passed"), v.get("details", ""), res.get("shot") or "", t0, t1)
        history.append({"step": step, "tool": tool, "summary": res.get("summary", "")})

    # ── core-coverage safety net: never report without the core money checks ──
    core = {"wager", "payout", "balance", "server", "feature"}
    if any(i["id"] in core and i["status"] == "pending" for i in items):
        print("  [QA-AGENT] core checks still pending — running a deterministic spin to fill them")
        t0 = ctx.clock()
        try:
            res = await tool_spin(ctx)
            for v in res.get("verdicts", []) or []:
                _resolve(items, v.get("id"), v.get("passed"), v.get("details", ""), res.get("shot") or "", t0, ctx.clock())
        except Exception as e:
            print(f"    [QA-AGENT] safety-net spin failed: {e}")

    # ── build TestResults ──
    results = []
    for it in items:
        r = T.TestResult(it["name"], it.get("shot") or "")
        r.passed = it["passed"] if it["status"] != "skip" else None
        r.details = it["details"] or ("not exercised on this game" if it["status"] == "pending" else "")
        if it.get("v0") is not None and it.get("v1") is not None and it["v1"] > it["v0"]:
            r.video_start, r.video_end = it["v0"], it["v1"]
        results.append(r)
    return results
