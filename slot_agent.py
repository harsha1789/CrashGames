"""
slot_agent.py — capabilities for OPERATING a slot game like a QA (not just detecting it).
================================================================================
First capability: drive_autoplay — actually RUN autoplay end-to-end:
  open the autoplay control -> work out how to start it with the FEWEST spins (vision,
  so it adapts to count-buttons / sliders / Start buttons) -> Start -> confirm it is
  auto-spinning (the spin endpoint fires repeatedly) -> STOP as soon as confirmed ->
  verify it stopped. Aggressive early-stop bounds the real-money spend to ~2 spins.

All vision-driven + network-verified, so it's provider-independent. This is capability
#1 of the agentic observe->decide->act->verify loop; more capabilities (menu-drill,
paytable, bonus) plug in the same way.
"""
import os
import re
import asyncio
from PIL import Image
from google.genai import types

import test_spin_button as T          # gemini_call, parse_gemini_json, flash_target
import slot_spin                      # _is_noise, UnifiedGameMonitor, frame_motion
from slot_explore import _thumb, _cfg, _safe, detect_panel_controls, describe_panel
import config_env                     # single source of truth for viewport + clamped scaling

# ─── standardized viewport coordinate scaling ─────────────────────
# All scaling now lives in config_env (the single source of truth) and is shared verbatim with
# test_spin_button and slot_explore. These module-level aliases keep the existing call sites
# (`_clamp_point`, `_norm_to_css`, `_norm_rect`, `_center`) unchanged while routing every click
# coordinate through the one clamped, live-viewport-aware implementation.
_clamp_point = config_env.clamp_point
_norm_to_css = config_env.norm_to_css
_norm_rect = config_env.norm_rect
_center = config_env.norm_box_center


def segment_click_point(box, target_index=0, total_segments=6, kind=None):
    """
    Resolve a single CSS click point INSIDE a composite selector whose box_2d
    ([ymin,xmin,ymax,xmax], 0-1000) covers a whole row/grid/slider of values that the vision
    model returned as ONE element. Used to pick e.g. the lowest spin count out of
    "10 20 30 50 100 200" detected as a single 'spin_row_selector' box.

      - slider  -> click the FAR-LEFT edge (minimum X) to select the lowest value.
      - container -> decide orientation by aspect ratio: width >= height is a HORIZONTAL row
        (split the width), otherwise a VERTICAL grid (split the height). Click the CENTER of
        segment `target_index` (0-based; 0 == lowest/first).

    Returns (x, y) CSS px, or None if the box is unusable.
    """
    rect = _norm_rect(box)
    if rect is None:
        return None
    l, t, r, b = rect
    w, h = r - l, b - t
    cx, cy = (l + r) // 2, (t + b) // 2
    k = (kind or "").lower()

    if "slider" in k:
        # lowest value lives at the left end; nudge in slightly so we land on the track/handle
        return (l + max(2, int(w * 0.04)), cy)

    n = max(1, int(total_segments or 6))
    i = max(0, min(int(target_index), n - 1))
    if w >= h:                                   # horizontal row -> split width
        seg = w / n
        return (int(l + seg * (i + 0.5)), cy)
    seg = h / n                                  # vertical grid -> split height
    return (cx, int(t + seg * (i + 0.5)))


# ─── spin counting via the network monitor ───────────────────────
def count_autospins(monitor, since_idx):
    """Count repeated non-idle spin requests since an index. Returns (count, path)."""
    paths = {}
    for r in monitor.requests[since_idx:]:
        if not slot_spin._is_noise(r["url"]) and r["path"] not in monitor.idle_paths:
            paths[r["path"]] = paths.get(r["path"], 0) + 1
    if not paths:
        return 0, None
    best = max(paths, key=paths.get)
    return paths[best], best


# ─── vision locators (single-purpose, tight boxes) ───────────────
def locate_autoplay_button(image_path):
    """Precisely locate the AUTOPLAY/AUTOSPIN button on the control bar (tight box)."""
    prompt = """Find the AUTOPLAY / AUTOSPIN button on this slot game's control bar. It shows
CIRCULAR LOOPING ARROWS (↻ / two curved arrows forming a loop) or the word "AUTO"/"AUTOPLAY", and
starts repeated automatic spins. It is often one of the two round buttons beside the Spin button.
Do NOT return the TURBO / fast-spin button (a speedometer/gauge dial, lightning bolt, or fast-
forward »») — that is a different control. Tell them apart by the ICON, not by which side they're on.
Return a TIGHT bounding box around JUST the autoplay button.
JSON: {"box_2d": [ymin,xmin,ymax,xmax]} or {"box_2d": null}. Normalize 0-1000."""
    try:
        r = T.parse_gemini_json(T.gemini_call([_thumb(image_path), prompt], _cfg()))
        return _center(r.get("box_2d"))
    except Exception:
        return None


def find_stop_control(running_path) -> dict:
    """While autoplay runs, find the control that STOPS it (often the spin button becomes Stop)."""
    prompt = """Autoplay is RUNNING in this slot game. Find the control that STOPS it. It is usually
ONE of these, most often near the center-bottom where the spin button was:
- the SPIN button changed into a STOP button (a square ■ / "STOP" / a spins-remaining countdown number), or
- a separate "Stop" / "Stop Autoplay" button.
Return JSON: {"stop_control": {"label": "", "box_2d": [ymin,xmin,ymax,xmax]} or null}.
If autoplay is clearly NOT running (no stop/countdown visible), return {"stop_control": null}. Normalize 0-1000."""
    try:
        return T.parse_gemini_json(T.gemini_call([_thumb(running_path), prompt], _cfg()))
    except Exception:
        return {"stop_control": None}


# ─── unified panel context: ONE vision call, cached ───────────────
_PANEL_CONTEXT_PROMPT = """This is a slot-game screenshot with an OPEN settings menu (autoplay /
bet / buy-bonus — any provider) shown as a DARK CENTRAL OVERLAY CARD on top of the dimmed game.
In ONE pass, detect EVERY actionable element that lives INSIDE that overlay card — and ONLY those.

SCOPE — CRITICAL. Return elements located WITHIN the boundaries of the central overlay card ONLY.
You MUST IGNORE everything outside it, specifically:
- the background game reels / symbols behind the dimmed overlay,
- the bottom CONTROL BAR (bet +/- steppers, coin +/- steppers, bet value, bet max, the round
  spin button, the autoplay button, balance display) that runs along the very bottom of the screen,
- the system clock / time display, sound and settings icons, and any footer text.
If an element sits BELOW the overlay card (in the bottom strip of the screen) it is background — do
NOT return it. When unsure whether something is part of the card, leave it out.

Return ONLY raw JSON (no markdown, no code fences):
{"elements": [
   {"label": "<specific, e.g. 'spins_10','loss_limit','start_autoplay','close_panel'>",
    "role": "spin_count|spin_row_selector|spin_slider|start|stop|toggle|input|close|other",
    "field": "<what it configures: spins|loss_limit|win_limit|amount|stop_condition|other|null>",
    "input_type": "<HOW you set it: buttons|dropdown|textbox|stepper|slider|toggle|none>",
    "required": true|false,
    "value": "<shown value or null>",
    "segments": <int: ONLY for spin_row_selector — how many distinct values are inside the box>,
    "box_2d": [ymin,xmin,ymax,xmax]}],
 "plan": ["ordered steps to start autoplay with the FEWEST spins, e.g. 'set loss_limit','select spins_10','press start_autoplay'"]}

EXPECT ANYTHING — autoplay panels vary hugely across games:
- The panel may have JUST a spin count, OR also a loss limit, win limit, bet/amount, or other
  stop-conditions — ANY subset. Detect only what is ACTUALLY present; never invent fields.
- Each field can be set in DIFFERENT ways: a row of number BUTTONS, a collapsed DROPDOWN (▼), a
  TEXTBOX you type into, a +/- STEPPER, a draggable SLIDER, or an on/off TOGGLE. Set input_type to
  what you actually see for THAT field.
- "required": true if the game forces it before Start (e.g. label says "(Required)", or the field
  shows only a placeholder like "---"/"Select" with no value and Start looks disabled). Else false.

Rules:
- Spin-count buttons visible as separate numbers (10, 20, 50) -> one "spin_count" EACH (number in
  "value", input_type "buttons"). A fused row ("10 20 30 50 100") -> ONE "spin_row_selector" with
  "segments". A spins slider -> "spin_slider". A spins dropdown/stepper/textbox -> role "input",
  field "spins", the matching input_type.
- start = the button that BEGINS autoplay (Start/Play/confirm, often green; NOT Cancel/Close).
- stop = a stop control if visible. close = the cancel/close (X) control.
- There may be ANY number of elements — do NOT assume a fixed count.
- box_2d MUST be a TIGHT box, normalized 0-1000 RELATIVE TO THE WHOLE IMAGE. Precision is critical."""


# Focused SECONDARY detection — only run when the primary pass finds NO discrete/row/slider spin
# chooser, i.e. the count is hidden behind a dropdown or set via a +/- stepper. Kept separate from
# the primary prompt because mixing these roles in degrades the common discrete-button case.
_SPIN_CHOOSER_PROMPT = """This is a slot AUTOPLAY panel whose spin-count is NOT a row of visible
number buttons. Identify HOW the count is chosen and return ONLY raw JSON:
{"kind": "dropdown|stepper|slider|none",
 "dropdown": {"label":"","value":"","box_2d":[ymin,xmin,ymax,xmax]} or null,
 "minus": {"box_2d":[ymin,xmin,ymax,xmax]} or null,
 "plus":  {"box_2d":[ymin,xmin,ymax,xmax]} or null,
 "value_field": {"value":"","box_2d":[ymin,xmin,ymax,xmax]} or null,
 "slider": {"box_2d":[ymin,xmin,ymax,xmax]} or null}
- dropdown = a COLLAPSED combo-box: ONE current count + a ▼ arrow, choices hidden until opened.
- stepper = a separate minus (−) and plus (+) button flanking a single number field.
- slider  = a draggable track/handle.
- If none of these are present, return {"kind":"none"}. Normalize box_2d to 0-1000."""


class PanelContext:
    """
    The complete, parsed object-detection map for ONE panel screenshot. Built by a SINGLE vision
    call (parse_panel_context) and cached, so downstream decision logic queries this in-memory map
    instead of re-invoking the model. Each element carries the raw `box_2d` plus a precomputed
    `center` (CSS px) and `css_rect` (left,top,right,bottom CSS px), all via the standardized scalers.
    """
    def __init__(self, elements, plan, image_path, error=None):
        self.elements = elements
        self.plan = plan
        self.image_path = image_path
        self.error = error

    def __bool__(self):
        return bool(self.elements)

    def clickable(self):
        return [e for e in self.elements if e.get("center")]

    def by_role(self, *roles):
        rs = set(roles)
        return [e for e in self.elements if e.get("role") in rs]

    def find(self, predicate):
        return next((e for e in self.elements if predicate(e)), None)

    def panel_bbox(self):
        """Union (normalized [ymin,xmin,ymax,xmax]) of the CORE controls — the overlay card's
        extent. Used to isolate processing to inside the active panel. None if no core control."""
        boxes = [e["box_2d"] for e in self.elements
                 if e.get("role") in _CORE_PANEL_ROLES
                 and isinstance(e.get("box_2d"), (list, tuple)) and len(e["box_2d"]) >= 4]
        if not boxes:
            return None
        return [min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes)]

    def is_autoplay_menu(self):
        """A REAL autoplay menu must expose a Start or some way to choose spins."""
        return any(e.get("role") in _SPIN_CHOOSER_ROLES + ("start",) for e in self.clickable())


# Cache keyed by screenshot path. Each frame has a unique filename, so this both (a) guarantees
# one vision call per frame and (b) lets every downstream query reuse the same parsed map.
_PANEL_CTX_CACHE = {}

# Every role that can be part of a spin-count chooser (discrete / composite / dropdown / stepper).
_SPIN_CHOOSER_ROLES = ("spin_count", "spin_row_selector", "spin_slider",
                       "spin_dropdown", "spin_minus", "spin_plus", "spin_value")
# Roles that genuinely belong to the autoplay overlay card. These are KEPT even when they sit
# low on the screen, because a panel's own Start/Cancel can legitimately be ~80% down.
_CORE_PANEL_ROLES = _SPIN_CHOOSER_ROLES + ("start", "stop", "toggle", "input", "close")
# Bottom band of the FULL screenshot (normalized 0-1000). The background control bar (bet/coin
# steppers, balance, spin/autoplay buttons), the clock and footer text live here. Non-core
# elements detected in this band are background bleed and are rejected.
_BOTTOM_BAND_YMAX = 800   # i.e. the bottom 20% of screen height
_PANEL_BBOX_PAD = 60      # normalized padding around the panel bbox when isolating elements


def parse_panel_context(image_path, force=False) -> PanelContext:
    """
    THE unified vision entry point for an open panel. Makes a SINGLE Gemini call on the full
    screenshot, returns the complete element map (box_2d + label + role + value), and caches it
    by path. Detect at the fixed viewport (dsf=1) for tight, accurate boxes; every coordinate is
    translated to CSS click px through the standardized scalers, so clicks land correctly.
    Subsequent calls for the same screenshot are served from cache — no extra LLM calls.
    """
    if not force and image_path in _PANEL_CTX_CACHE:
        return _PANEL_CTX_CACHE[image_path]

    img = Image.open(image_path)
    W, H = img.size
    api = img.copy()
    api.thumbnail((1280, 1280), Image.Resampling.LANCZOS)   # box_2d is normalized -> resize-safe
    try:
        data = T.parse_gemini_json(T.gemini_call([api, _PANEL_CONTEXT_PROMPT], _cfg()))
    except Exception as e:
        ctx = PanelContext([], [], image_path, error=str(e))
        _PANEL_CTX_CACHE[image_path] = ctx
        return ctx

    if isinstance(data, dict):
        raw = data.get("elements", [])
        plan = data.get("plan", [])
    elif isinstance(data, list):
        raw, plan = data, []
    else:
        raw, plan = [], []

    els = []
    dropped = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        box = e.get("box_2d")
        # STRICT Y-BOUND FILTER: reject background bleed — anything in the bottom band of the
        # screen that is NOT a core panel control (the bet/coin bar, balance, clock, footer).
        # box_2d is [ymin, xmin, ymax, xmax]; ymax is the element's lowest edge.
        ymax = box[2] if isinstance(box, (list, tuple)) and len(box) >= 4 else None
        if (ymax is not None and ymax > _BOTTOM_BAND_YMAX
                and e.get("role") not in _CORE_PANEL_ROLES):
            dropped.append(e.get("label"))
            continue
        e["center"] = _center(box)            # standardized 0-1000 -> CSS px (clamped on-screen)
        e["css_rect"] = _norm_rect(box)
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            try:
                ymin, xmin, ymax, xmax = box[:4]
                e["_box_full_px"] = (int(xmin / 1000 * W), int(ymin / 1000 * H),
                                     int(xmax / 1000 * W), int(ymax / 1000 * H))  # overlay/debug
            except (TypeError, ValueError):
                e["_box_full_px"] = None
        els.append(e)

    # PANEL-BBOX ISOLATION (point 4): build the overlay card's bounding box from the CORE controls,
    # then reject any NON-core element whose center falls OUTSIDE that (padded) box. Core controls
    # are always kept. This strips stray background detections that survived the band filter, and
    # adapts to each layout instead of relying on a fixed strip.
    core_boxes = [e["box_2d"] for e in els if e.get("role") in _CORE_PANEL_ROLES
                  and isinstance(e.get("box_2d"), (list, tuple)) and len(e["box_2d"]) >= 4]
    if core_boxes:
        uy0 = min(b[0] for b in core_boxes) - _PANEL_BBOX_PAD
        ux0 = min(b[1] for b in core_boxes) - _PANEL_BBOX_PAD
        uy1 = max(b[2] for b in core_boxes) + _PANEL_BBOX_PAD
        ux1 = max(b[3] for b in core_boxes) + _PANEL_BBOX_PAD
        kept = []
        for e in els:
            box = e.get("box_2d")
            if e.get("role") not in _CORE_PANEL_ROLES and isinstance(box, (list, tuple)) and len(box) >= 4:
                cy, cx = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                if not (ux0 <= cx <= ux1 and uy0 <= cy <= uy1):
                    dropped.append(e.get("label"))
                    continue
            kept.append(e)
        els = kept

    if dropped:
        print(f"    [PANEL] filtered {len(dropped)} off-panel/background element(s): {dropped}")

    ctx = PanelContext(els, plan, image_path)
    _PANEL_CTX_CACHE[image_path] = ctx
    return ctx


# ─── dynamic minimum-spin calculation (point 1: no hardcoded targets) ─────
def _extract_int(text):
    """First/representative integer embedded in a string, or None. Uses the LAST run of digits so
    'Number Of Spins: 10' -> 10 and 'x25' -> 25, while ignoring currency-y noise around it."""
    if text is None:
        return None
    nums = re.findall(r'\d+', str(text))
    return int(nums[-1]) if nums else None


def spin_count_value(e):
    """Integer spin-count an element represents — prefer its 'value', else parse its 'label'."""
    for src in (e.get("value"), e.get("label")):
        v = _extract_int(src)
        if v is not None:
            return v
    return None


def lowest_spin_count_element(elements):
    """Among role 'spin_count' elements, return (element, value) with the ABSOLUTE LOWEST integer
    — the safest real-money limit — or (None, None). No hardcoded strings: purely numeric."""
    best = None
    for e in elements:
        if e.get("role") != "spin_count" or not e.get("center"):
            continue
        v = spin_count_value(e)
        if v is None or not (1 <= v <= 100000):
            continue
        if best is None or v < best[1]:
            best = (e, v)
    return best if best else (None, None)


# ─── observe → act → verify primitive (point 3) ───────────────────
async def _act_and_verify(page, point, label, ss_dir, tag, step, color="cyan", settle=1.4, thresh=6.0):
    """Click `point` (clamped on-screen), screenshotting BEFORE and AFTER, and report whether the
    UI visibly changed. The atomic unit of the state machine: every interaction is verified."""
    point = _clamp_point(*point)
    before = os.path.join(ss_dir, f"{tag}_{step}_b.png"); await page.screenshot(path=before)
    try:
        await T.flash_target(page, point, label, color)
    except Exception:
        pass
    await page.mouse.click(*point); await asyncio.sleep(settle)
    after = os.path.join(ss_dir, f"{tag}_{step}_a.png"); await page.screenshot(path=after)
    changed = slot_spin.frame_motion(before, after) > thresh
    return changed, after


def _region_motion(a_path, b_path, rect=None):
    """Mean abs grayscale diff (0-255) within `rect` (CSS px), or whole-frame if None. Cheap, no
    LLM — used to detect when a stepper's value stops changing (reached its minimum)."""
    try:
        a = Image.open(a_path).convert("L"); b = Image.open(b_path).convert("L")
        if a.size != b.size:
            b = b.resize(a.size)
        if rect:
            a = a.crop(rect); b = b.crop(rect)
        import numpy as np
        return float((abs(np.asarray(a, dtype=np.int16) - np.asarray(b, dtype=np.int16))).mean())
    except Exception:
        return slot_spin.frame_motion(a_path, b_path)


# ─── dynamic interaction handlers (point 2) ───────────────────────
async def _select_via_dropdown(page, dd, ss_dir, tag):
    """Expand a spin-count dropdown, RE-DETECT the now-visible options, click the lowest, and
    verify the menu collapsed. Returns (ok, value)."""
    print(f"    [SPINS] dropdown '{dd.get('label')}' -> expanding to read options")
    _changed, opened = await _act_and_verify(page, dd["center"], dd.get("label", "spins_dropdown"),
                                             ss_dir, tag, "dd_open", color="cyan")
    ctx2 = parse_panel_context(opened, force=True)            # options are only visible now
    el, val = lowest_spin_count_element(ctx2.clickable())
    if el is None:                                            # list might be a row/slider block
        sel = ctx2.find(lambda e: e.get("role") in ("spin_row_selector", "spin_slider") and e.get("box_2d"))
        if sel:
            pt = segment_click_point(sel["box_2d"], 0, int(sel.get("segments") or 6), sel.get("role"))
            if pt:
                await _act_and_verify(page, pt, "lowest_option", ss_dir, tag, "dd_pick", color="lime")
                return True, None
        print("    [SPINS] dropdown opened but no numeric options detected")
        return False, None
    print(f"    [SPINS] lowest dropdown option = {val}")
    await _act_and_verify(page, el["center"], el.get("label", f"spins_{val}"),
                          ss_dir, tag, "dd_pick", color="lime")
    return True, val


async def _select_via_stepper(page, minus_el, value_el, ss_dir, tag, max_steps=12):
    """Click the 'minus' control until the value display stops changing (stable across 2 clicks)
    — i.e. the stepper has bottomed out at its minimum. Returns (ok, presses)."""
    pt = _clamp_point(*minus_el["center"])
    rect = value_el.get("css_rect") if value_el else None
    print(f"    [SPINS] stepper -> clicking minus to the minimum (watch region={rect})")
    prev = os.path.join(ss_dir, f"{tag}_st_init.png"); await page.screenshot(path=prev)
    stable = presses = 0
    for i in range(max_steps):
        try:
            await T.flash_target(page, pt, minus_el.get("label", "minus"), "cyan")
        except Exception:
            pass
        await page.mouse.click(*pt); await asyncio.sleep(0.6)
        cur = os.path.join(ss_dir, f"{tag}_st{i}.png"); await page.screenshot(path=cur)
        presses += 1
        mv = _region_motion(prev, cur, rect)
        prev = cur
        if mv < 2.0:                       # value didn't move -> at (or near) the floor
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
    print(f"    [SPINS] stepper stabilized after {presses} minus-press(es)")
    return True, presses


async def select_minimum_spins(page, ctx, panel_path, ss_dir, tag, spin_segments=6, spin_target_index=0):
    """
    UNIVERSAL fewest-spins selector. Inspects the parsed panel and dispatches to whichever control
    type is present — discrete buttons, a composite row/grid, a slider, a dropdown, or a +/- stepper
    — always aiming at the lowest value. Every interaction is verified. Returns a summary dict.
    """
    res = {"strategy": "none", "selected": False, "value": None, "label": None}
    clk = ctx.clickable()

    # 1) Discrete buttons -> click the absolute lowest integer (no hardcoded labels).
    el, val = lowest_spin_count_element(clk)
    if el is not None:
        res.update(strategy="buttons", label=el.get("label"), value=val)
        await _act_and_verify(page, el["center"], el.get("label", f"spins_{val}"),
                              ss_dir, tag, "count", color="cyan")
        res["selected"] = True
        return res

    # 2) Composite row/grid OR slider returned as ONE box -> segment/edge math.
    sel = ctx.find(lambda e: e.get("role") in ("spin_row_selector", "spin_slider") and e.get("box_2d"))
    if sel is not None:
        kind = sel.get("role"); segs = int(sel.get("segments") or spin_segments)
        pt = segment_click_point(sel["box_2d"], spin_target_index, segs, kind)
        res.update(strategy=kind, label=sel.get("label"))
        if pt:
            await _act_and_verify(page, pt, f"{sel.get('label', 'spins')}[seg{spin_target_index}]",
                                  ss_dir, tag, "count", color="cyan")
            res["selected"] = True
        return res

    # 3) No discrete/row/slider chooser was visible -> run the FOCUSED secondary detection for a
    #    dropdown / stepper / slider (kept out of the primary prompt to protect the common case).
    chooser = _detect_spin_chooser(panel_path)
    kind = chooser.get("kind")
    print(f"    [SPINS] no visible buttons; secondary chooser detection -> {kind}")

    if kind == "dropdown" and chooser.get("dropdown"):
        dd = dict(chooser["dropdown"]); dd["center"] = _center(dd.get("box_2d"))
        if dd["center"]:
            ok, v = await _select_via_dropdown(page, dd, ss_dir, tag)
            res.update(strategy="dropdown", label=dd.get("label"), selected=ok, value=v)
            return res

    if kind == "slider" and chooser.get("slider"):
        pt = segment_click_point(chooser["slider"].get("box_2d"), 0, spin_segments, "spin_slider")
        if pt:
            await _act_and_verify(page, pt, "slider_min", ss_dir, tag, "count", color="cyan")
            res.update(strategy="slider", selected=True)
            return res

    if kind == "stepper" and chooser.get("minus"):
        minus = {"center": _center(chooser["minus"].get("box_2d")), "label": "minus"}
        vf = chooser.get("value_field") or {}
        value_el = {"css_rect": _norm_rect(vf.get("box_2d"))} if vf.get("box_2d") else None
        if minus["center"]:
            ok, presses = await _select_via_stepper(page, minus, value_el, ss_dir, tag)
            res.update(strategy="stepper", selected=ok, value=f"min after {presses} press(es)")
            return res

    # 4) Nothing to choose — a count is presumably already set / default.
    return res


def _detect_spin_chooser(panel_path):
    """Secondary focused vision call: how is the spin count chosen when it isn't a row of visible
    buttons (dropdown / stepper / slider)? Returns the parsed dict or {'kind':'none'}."""
    try:
        data = T.parse_gemini_json(T.gemini_call([_thumb(panel_path), _SPIN_CHOOSER_PROMPT], _cfg()))
        return data if isinstance(data, dict) else {"kind": "none"}
    except Exception as e:
        print(f"    [SPINS] secondary chooser detection failed: {e}")
        return {"kind": "none"}


# A required stop-condition selector (loss limit / budget / single win-or-loss) that some autoplay
# panels (e.g. Thor's Rage) force you to set before Start enables. We stop autoplay after the first
# spin regardless, so picking any concrete option is safe — the point is just to unlock Start.
# A required, unset stop-condition selector the game forces before Start (loss/win limit, budget,
# stop-on-X, time limit). Generic across providers. We stop autoplay after ~2 spins, so picking any
# concrete option is safe — the point is only to UNLOCK Start.
_REQUIRED_FIELD_RX = re.compile(
    r"(loss[\s_-]*limit|win[\s_-]*limit|stop[\s_-]*(on|at|condition|loss|win)|budget|"
    r"single[\s_-]*(win|loss)|time[\s_-]*limit|required)", re.I)
_UNSET_VALUES = {"", "-", "--", "---", "----", "------", "—", "–", "select", "none", "required",
                 "(required)", "n/a", "null"}


def _norm_label(s):
    """Underscores/dashes -> spaces, so role-style labels ('loss_limit_input') match the same way
    as human labels ('Loss limit (Required)')."""
    return re.sub(r"[\s_\-]+", " ", (s or "")).strip().lower()


_REQUIRED_FIELDS = {"loss_limit", "win_limit", "amount", "stop_condition"}


def _is_required_unset(el):
    """A field the game likely forces before Start, and not yet set. Uses the panel parse's
    explicit `required`/`field` hints when present, and falls back to the label/value heuristic —
    so it works whether or not the model filled the new fields."""
    val = str(el.get("value") or "").strip().lower()   # value may be int/float, not just str
    has_val = bool(val) and val not in _UNSET_VALUES
    if has_val:
        return False
    if el.get("required") is True:
        return True
    if (el.get("field") or "").lower() in _REQUIRED_FIELDS:
        return True
    return bool(_REQUIRED_FIELD_RX.search(_norm_label(el.get("label"))))


def _pick_value_option(opts):
    """From a just-opened dropdown's controls, pick the first CONCRETE value (not a placeholder,
    the field label, a close, or a money/exit control)."""
    for o in opts:
        if not o.get("center"):
            continue
        ol = _norm_label(o.get("label"))
        if not ol or ol in _UNSET_VALUES or "required" in ol or _REQUIRED_FIELD_RX.search(ol):
            continue
        if classify_target(o.get("label"), None) in ("close", "exit", "money"):
            continue
        return o
    return None


async def _set_one_field(page, el, ss_dir, tag):
    """Set ONE required field, dispatching on its input_type so we handle ANY form a game uses:
    dropdown/buttons/slider -> open & pick a concrete value; textbox -> type a small value;
    stepper -> nudge up; toggle -> enable. Falls back to the dropdown approach if the type is
    unknown, then to typing. Returns True if it did something plausible. Money-safe (we stop after
    ~2 spins), so any valid low value is fine — the goal is only to UNLOCK Start."""
    label = el.get("label") or "required field"
    it = (el.get("input_type") or "").lower()
    pt = _clamp_point(*el["center"])
    try:
        if it == "toggle":
            await page.mouse.click(*pt); await asyncio.sleep(0.5)
            print(f"    [AUTOPLAY] required '{label}': toggled on"); return True

        if it == "stepper":
            await page.mouse.click(*pt); await asyncio.sleep(0.3)
            for _ in range(2):                     # nudge above zero (arrow works on most steppers)
                await page.keyboard.press("ArrowUp"); await asyncio.sleep(0.2)
            print(f"    [AUTOPLAY] required '{label}': stepped up"); return True

        if it == "textbox":
            await page.mouse.click(*pt); await asyncio.sleep(0.3)
            try:
                await page.keyboard.press("Control+A")
            except Exception:
                pass
            await page.keyboard.type("10"); await asyncio.sleep(0.2)
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass
            print(f"    [AUTOPLAY] required '{label}': typed a value"); return True

        # dropdown | buttons | slider | unknown -> open, then pick a concrete option
        await page.mouse.click(*pt); await asyncio.sleep(0.9)
        shot = os.path.join(ss_dir, f"{tag}_req_{_safe(label)}.png"); await page.screenshot(path=shot)
        opt = _pick_value_option(detect_panel_controls(shot))
        if opt:
            await page.mouse.click(*_clamp_point(*opt["center"])); await asyncio.sleep(0.7)
            print(f"    [AUTOPLAY] set required '{label}' -> '{opt.get('label')}'"); return True
        # nothing opened — maybe it's actually a textbox; try typing as a last resort
        await page.keyboard.type("10"); await asyncio.sleep(0.2)
        print(f"    [AUTOPLAY] required '{label}': no option list; typed a fallback value")
        return True
    except Exception as e:
        print(f"    [AUTOPLAY] could not set required '{label}': {e}")
        return False


async def _set_required_fields(page, ctx, ss_dir, tag):
    """Satisfy EVERY required-but-unset field in an autoplay panel so Start enables — regardless of
    which fields a game has (spins / loss limit / win limit / amount / other, ANY subset) or how
    they're set (buttons / dropdown / textbox / stepper / slider / toggle). Returns True if anything
    was set. Safe: autoplay is stopped after ~2 spins."""
    chosen = []   # one click target per field (dedupe the label+control pair that shares a row)
    for el in ctx.clickable():
        if not _is_required_unset(el):
            continue
        c = el.get("center")
        if not c:
            continue
        if any(abs(c[0] - k["center"][0]) < 140 and abs(c[1] - k["center"][1]) < 55 for k in chosen):
            continue
        chosen.append(el)

    set_any = False
    for el in chosen:
        if await _set_one_field(page, el, ss_dir, tag):
            set_any = True
    return set_any


# ─── capability: drive autoplay (observe → act → verify state machine) ─────
async def drive_autoplay(page, autoplay_center, monitor, ss_dir, tag="autoplay",
                         confirm_spins=2, watch_s=16.0, spin_segments=6, spin_target_index=0):
    """
    Universal, platform-agnostic autoplay agent driven as an OBSERVE → ACT → VERIFY state machine:

        OPEN  ->  SELECT (fewest spins, any control type)  ->  START  ->  RUN_STOP  ->  VERIFY

    Every interaction is verified by screenshot; an action that doesn't change the UI triggers
    local re-detection (handles layout shifts / animation delays). Real-money exposure is bounded
    by an aggressive stop the moment the spin threshold (network OR observed reel motion) reaches
    `confirm_spins`. `spin_segments`/`spin_target_index` tune composite row/grid/slider selection.
    Preserves the legacy result contract: opened/started/spins_observed/stopped/notes/shots/options.
    """
    os.makedirs(ss_dir, exist_ok=True)
    res = {"opened": False, "started": False, "spins_observed": 0,
           "stopped": False, "notes": [], "shots": {}, "options": [], "plan": []}

    # HARD stake cap (team rule 2026-07-09): autoplay spins repeatedly at the CURRENT stake,
    # so refuse to even open the menu when the on-screen stake exceeds config_env.MAX_STAKE.
    _cap_shot = os.path.join(ss_dir, f"{tag}_cap_check.png")
    await page.screenshot(path=_cap_shot)
    try:
        _stake = T.parse_amount((T.read_game_values(Image.open(_cap_shot)) or {}).get("bet") or "")
    except Exception:
        _stake = None
    if _stake is not None and _stake > config_env.MAX_STAKE:
        res["notes"].append(f"stake {_stake:g} exceeds the safety cap "
                            f"{config_env.MAX_STAKE:g} — autoplay refused")
        print(f"    [AUTOPLAY] 🛑 stake {_stake:g} > cap {config_env.MAX_STAKE:g} — refusing to start")
        return res

    def _is_start(e):
        l = (e.get('label') or '').lower()
        return e.get('role') == 'start' or (("start" in l or "begin" in l or "confirm" in l) and "cancel" not in l)

    def _is_spinstop(lbl):   # the round Spin button (becomes Stop), NOT "Total Spins Selector"
        l = (lbl or "").lower()
        if "stop" in l:
            return True
        return "spin" in l and not any(x in l for x in
            ("total", "selector", "number", "auto", "setting", "free"))

    ctx = PanelContext([], [], None)
    panel = None
    since = monitor.req_count()
    bal_before = None
    stop_fallback = None
    n = motion_spins = 0
    started = stop_clicked = False
    attempts = {"OPEN": 0, "START": 0}

    state = "OPEN"
    while state != "DONE":

        # ── OPEN: click autoplay, verify a real menu appeared; else re-locate & retry ──
        if state == "OPEN":
            attempts["OPEN"] += 1
            try:
                await T.flash_target(page, autoplay_center, "Autoplay", "orange")
            except Exception:
                pass
            await page.mouse.click(*_clamp_point(*autoplay_center)); await asyncio.sleep(2.5)
            panel = os.path.join(ss_dir, f"{tag}_panel.png"); await page.screenshot(path=panel)
            res["shots"]["panel"] = os.path.basename(panel)
            ctx = parse_panel_context(panel, force=True)       # force: same filename reused per attempt
            if ctx.is_autoplay_menu():
                res["opened"] = True
                res["plan"] = ctx.plan
                res["options"] = [{"label": e.get("label"), "role": e.get("role"), "value": e.get("value")}
                                  for e in ctx.clickable()]
                print(f"    [AUTOPLAY] menu elements: {[e.get('label') for e in ctx.clickable()]}")
                print(f"    [AUTOPLAY] plan: {res['plan']}")
                state = "SELECT"; continue
            if attempts["OPEN"] >= 3:
                res["notes"].append("autoplay menu did not open — no Start/spin-count after 3 attempts")
                return res
            await page.keyboard.press("Escape"); await asyncio.sleep(0.6)
            ap2 = locate_autoplay_button(panel)                # re-detect on failure (point 3)
            if ap2:
                print(f"    [AUTOPLAY] menu didn't open (attempt {attempts['OPEN']}); re-located autoplay @ {ap2}")
                autoplay_center = ap2
            continue

        # ── SELECT: set the FEWEST spins via whatever control type is present ──
        if state == "SELECT":
            sel = await select_minimum_spins(page, ctx, panel, ss_dir, tag,
                                             spin_segments=spin_segments, spin_target_index=spin_target_index)
            res["spin_selection"] = sel
            print(f"    [AUTOPLAY] spin-count: strategy={sel.get('strategy')} "
                  f"selected={sel.get('selected')} value={sel.get('value')}")
            # Satisfy any REQUIRED stop-condition (e.g. Thor's Rage "Loss limit (Required)") so Start
            # can enable; re-detect the panel afterwards since setting it changes the layout.
            if await _set_required_fields(page, ctx, ss_dir, tag):
                req_shot = os.path.join(ss_dir, f"{tag}_postreq.png"); await page.screenshot(path=req_shot)
                ctx = parse_panel_context(req_shot, force=True)
                res["notes"].append("set a required field (e.g. loss limit) before Start")
            # Baseline the network index + wallet AFTER selecting (selection isn't a spin), and
            # pre-resolve a fallback stop (round spin button doubles as STOP while running).
            since = monitor.req_count()
            try:
                pre = os.path.join(ss_dir, f"{tag}_prestart.png"); await page.screenshot(path=pre)
                bal_before = T.parse_amount(T.read_game_values(Image.open(pre)).get("balance", ""))
                bar = T.detect_controls_merged(Image.open(pre), passes=1)
                sb0 = next((c for c in bar if c.get("center") and _is_spinstop(c.get("label"))), None)
                stop_fallback = sb0["center"] if sb0 else None
            except Exception:
                pass
            state = "START"; continue

        # ── START: click Start, verify the panel closed / spins began; re-detect on failure ──
        if state == "START":
            attempts["START"] += 1
            start_btn = ctx.find(lambda e: e.get('center') and _is_start(e))
            if start_btn is None:                              # re-detect from a fresh frame
                sf = os.path.join(ss_dir, f"{tag}_startfind{attempts['START']}.png"); await page.screenshot(path=sf)
                ctx = parse_panel_context(sf, force=True)
                start_btn = ctx.find(lambda e: e.get('center') and _is_start(e))
            if start_btn is None:
                if attempts["START"] >= 3:
                    res["notes"].append("no Start button identified in the autoplay menu")
                    state = "RUN_STOP"; continue
                await asyncio.sleep(1.0); continue
            changed, _chk = await _act_and_verify(page, start_btn["center"], start_btn.get("label", "Start"),
                                                  ss_dir, tag, f"start{attempts['START']}",
                                                  color="lime", settle=1.8)
            if changed or count_autospins(monitor, since)[0] >= 1:
                state = "RUN_STOP"; continue
            if attempts["START"] >= 3:
                res["notes"].append("Start click did not take effect (panel stayed open, no spins)")
                state = "RUN_STOP"; continue
            # Start didn't take — most commonly a required field is still unset. Re-detect the panel
            # and try to satisfy it before the next attempt.
            rf = os.path.join(ss_dir, f"{tag}_startretry{attempts['START']}.png"); await page.screenshot(path=rf)
            ctx = parse_panel_context(rf, force=True)
            await _set_required_fields(page, ctx, ss_dir, tag)
            continue

        # ── RUN_STOP: confirm spinning AND aggressively stop in one tight loop ──
        if state == "RUN_STOP":
            n = motion_spins = 0; started = stop_clicked = False
            stop_ctr = None; prev = None; t = 0.0
            while t < watch_s:
                fp = os.path.join(ss_dir, f"{tag}_w{t:04.1f}.png"); await page.screenshot(path=fp)
                n = count_autospins(monitor, since)[0]
                mv = slot_spin.frame_motion(prev, fp) if prev else 0.0
                if prev and mv > 5.0:
                    motion_spins += 1               # visible reel-motion event ≈ a spin (fallback signal)
                prev = fp
                spins = max(n, motion_spins)         # network OR motion — whichever sees the spin
                if n >= 1 or mv > 5.0:
                    started = True
                    res["shots"]["running"] = os.path.basename(fp)
                    if stop_ctr is None:             # resolve the STOP target ONCE, as soon as it's live
                        stop = find_stop_control(fp).get("stop_control")
                        stop_ctr = _center(stop.get("box_2d")) if stop else stop_fallback
                # AGGRESSIVE STOP the instant the threshold is reached (point 4).
                if started and spins >= confirm_spins:
                    target = stop_ctr or stop_fallback
                    if target:
                        try:
                            await T.flash_target(page, target, "STOP", "red")
                        except Exception:
                            pass
                        await page.mouse.click(*_clamp_point(*target)); await asyncio.sleep(1.2)
                        stop_clicked = True
                        res["stop_method"] = "clicked"
                        res["notes"].append(f"stop hit aggressively at spins>={confirm_spins} "
                                            f"(network={n}, motion={motion_spins})")
                    break
                await asyncio.sleep(1.0); t += 1.0

            if not started and bal_before is not None:         # last resort: did the wallet drop?
                try:
                    post = os.path.join(ss_dir, f"{tag}_poststart.png"); await page.screenshot(path=post)
                    bal_after = T.parse_amount(T.read_game_values(Image.open(post)).get("balance", ""))
                    if bal_after is not None and bal_after < bal_before:
                        started = True
                except Exception:
                    pass
            res["spins_observed"] = max(n, motion_spins)
            res["started"] = started
            if not started:
                res["notes"].append("could not confirm autoplay started (no reel motion / network spins / balance drop)")

            # BACKSTOP: vision stop across a few fresh frames -> spin-as-stop -> Escape.
            if not stop_clicked:
                for attempt in range(4):
                    running = os.path.join(ss_dir, f"{tag}_running{attempt}.png"); await page.screenshot(path=running)
                    res["shots"]["running"] = os.path.basename(running)
                    n_now = count_autospins(monitor, since)[0]
                    stop = find_stop_control(running).get("stop_control")
                    stop_ctr = _center(stop.get("box_2d")) if stop else None
                    if stop_ctr:
                        try:
                            await T.flash_target(page, stop_ctr, "STOP", "red")
                        except Exception:
                            pass
                        await page.mouse.click(*_clamp_point(*stop_ctr)); await asyncio.sleep(1.5)
                        stop_clicked = True
                        res["notes"].append(f"stop clicked (frame {attempt}, after {n_now} spins)")
                        break
                    await asyncio.sleep(1.5)
            if not stop_clicked:
                # No vision stop control found. CRITICAL: with the fewest spins selected, a short
                # autoplay often FINISHES on its own before we can stop it. Decide first whether it's
                # still running — because the spin-as-stop fallback clicks the SPIN button, which
                # would fire a NEW spin (extra money!) if autoplay had already ended.
                a = os.path.join(ss_dir, f"{tag}_still_a.png"); await page.screenshot(path=a)
                na = count_autospins(monitor, since)[0]
                await asyncio.sleep(1.3)
                b = os.path.join(ss_dir, f"{tag}_still_b.png"); await page.screenshot(path=b)
                nb = count_autospins(monitor, since)[0]
                still_running = (nb > na) or slot_spin.frame_motion(a, b) > 5.0
                if not still_running:
                    # It completed its selected spins by itself — that IS a valid stop. Do NOT click
                    # the spin button (would start another spin).
                    stop_clicked = True
                    res["stop_method"] = "completed_naturally"
                    res["notes"].append(f"autoplay completed its {res['spins_observed']} selected "
                                        f"spin(s) on its own — no manual stop needed")
                elif stop_fallback:
                    await page.mouse.click(*_clamp_point(*stop_fallback)); await asyncio.sleep(1.5)
                    stop_clicked = True
                    res["stop_method"] = "spin_as_stop"
                    res["notes"].append("still running with no vision stop — used spin-as-stop fallback")
                else:
                    await page.keyboard.press("Escape"); await asyncio.sleep(1.0)
                    res["notes"].append("stop control not found while running; pressed Escape")
            state = "VERIFY"; continue

        # ── VERIFY: the spin count should plateau ──
        if state == "VERIFY":
            n_before = count_autospins(monitor, since)[0]
            await asyncio.sleep(5.0)
            n_after = count_autospins(monitor, since)[0]
            res["stopped"] = (n_after == n_before)
            res["spins_observed"] = max(res.get("spins_observed", 0), n_after)
            final = os.path.join(ss_dir, f"{tag}_stopped.png"); await page.screenshot(path=final)
            res["shots"]["stopped"] = os.path.basename(final)
            state = "DONE"; continue

    return res


def locate_menu_icon(image_path):
    """
    Precisely locate the menu button — usually a SMALL 3-line 'hamburger' (≡) or gear icon
    in a corner. The general detector tends to draw a loose box around tiny icons, so its
    center can miss; this asks for a TIGHT box around JUST the icon. Returns center or None.
    """
    prompt = """Find the MAIN MENU button in this slot game. It is a SMALL icon — usually three
horizontal lines (a "hamburger" ≡) or a gear/settings icon, typically in a top or bottom corner.
Return a TIGHT bounding box around JUST that icon (not the surrounding bar).
JSON: {"box_2d": [ymin,xmin,ymax,xmax]} or {"box_2d": null}. Normalize 0-1000."""
    try:
        r = T.parse_gemini_json(T.gemini_call([_thumb(image_path), prompt], _cfg()))
        box = r.get("box_2d")
        if isinstance(box, list) and len(box) == 4 and box[2] > box[0] and box[3] > box[1]:
            return _center(box)
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════
#   AUTONOMOUS MENU CRAWLER — Depth-First Search state machine
#   Open a panel, dynamically discover its targets from the live vision
#   snapshot, hard-block transactional/exit controls, and DFS-traverse
#   the SAFE navigation targets (observe → act → verify → return).
# ═══════════════════════════════════════════════════════════════════
# Intent classification of a detected element. EXIT/MONEY are HARD-BLOCKED from traversal
# (never clicked). CLOSE controls are reserved for resetting, not explored. SAFE_NAV is crawled.
_EXIT_TOKENS  = ("lobby", "home", "exit", "cashier", "deposit", "withdraw", "logout",
                 "log out", "sign out", "quit", "leave", "account", "banking", "real money")
_MONEY_TOKENS = ("buy", "purchase", "confirm", "spin", "start", "stake", "wager",
                 "bet ", "max bet", "increase bet", "decrease bet")
_NAV_TOKENS   = ("pay", "rule", "info", "help", "history", "setting", "about", "how to",
                 "table", "feature", "limit", "sound", "music", "volume", "option", "game info",
                 "report", "statement", "terms")
_CLOSE_WORDS  = ("close", "back", "done", "ok", "return", "cancel", "x")


def classify_target(label, ttype=None):
    """Intent of a detected element: 'exit' | 'money' | 'close' | 'safe_nav' | 'other'.
    Used to build the SAFE execution list and to hard-block anything that could spend money or
    leave the game environment (point 2)."""
    low = (label or "").strip().lower()
    if not low:
        return "other"
    if any(re.search(rf"\b{re.escape(w)}\b", low) for w in _CLOSE_WORDS):
        return "close"
    if any(w in low for w in _EXIT_TOKENS):
        return "exit"
    if any(w in low for w in _MONEY_TOKENS):
        return "money"
    if any(w in low for w in _NAV_TOKENS) or ttype in ("link", "tab", "button"):
        return "safe_nav"
    return "other"


def _find_close(elements):
    """First detected element that is a close/back/X control (for resetting a sub-view)."""
    return next((e for e in elements if e.get("center")
                 and classify_target(e.get("label"), e.get("type")) == "close"), None)


async def _paginate_subview(page, ss_dir, prefix, max_pages):
    """Click forward/next controls inside an OPEN sub-view, capturing each page (point 3d).
    All click coordinates are clamped on-screen. Returns the list of page screenshots."""
    pages = []
    for i in range(max_pages):
        sh = os.path.join(ss_dir, f"{prefix}_pg{i}.png"); await page.screenshot(path=sh)
        pages.append(os.path.basename(sh))
        if i == max_pages - 1:
            break
        ctrls = detect_panel_controls(sh)
        nxt = next((c for c in ctrls if c.get("center") and any(
            k in (c.get("label") or "").lower()
            for k in ("next", "forward", "right arrow", "next page", ">"))), None)
        if not nxt:
            break
        await page.mouse.click(*_clamp_point(*nxt["center"])); await asyncio.sleep(1.2)
    return pages


async def _return_to(page, base_shot, ss_dir, tag, reopen):
    """Backtrack to the view captured in `base_shot` (point 3e): Escape, verify via image delta;
    if that overshoots, click a detected close control; as a last resort call `reopen()` to
    re-display the level. Returns True if we verified the return."""
    for attempt in range(2):
        await page.keyboard.press("Escape"); await asyncio.sleep(0.9)
        chk = os.path.join(ss_dir, f"{tag}_ret{attempt}.png"); await page.screenshot(path=chk)
        if slot_spin.frame_motion(base_shot, chk) < CLOSE_DELTA:
            return True
        close_el = _find_close(_describe_merged(chk, passes=1))
        if not close_el:
            # Fallback: general detector is better at finding small Xs on modal edges
            try:
                close_el = _find_close(T.detect_controls_merged(Image.open(chk), passes=1))
            except Exception:
                pass
        if close_el:
            await page.mouse.click(*_clamp_point(*close_el["center"])); await asyncio.sleep(0.9)
            chk2 = os.path.join(ss_dir, f"{tag}_retc{attempt}.png"); await page.screenshot(path=chk2)
            if slot_spin.frame_motion(base_shot, chk2) < CLOSE_DELTA:
                return True
    if reopen:
        await reopen()
        # For modals that closed the side-menu, reopen() brings it back.
        # Verify if we actually successfully restored the menu state to save a rescan.
        final_chk = os.path.join(ss_dir, f"{tag}_retfinal.png"); await page.screenshot(path=final_chk)
        if slot_spin.frame_motion(base_shot, final_chk) < CLOSE_DELTA:
            return True
    return False


CLOSE_DELTA = 6.0    # frame-motion below this vs a base snapshot == we are back at that view
OPEN_DELTA = 4.0     # frame-motion above this vs the pre-open frame == a panel actually opened


DEDUPE_DELTA = 3.5   # opened sub-views this similar (frame-motion) are treated as the SAME view


async def _dfs_level(page, ss_dir, tag, base_shot, depth, max_depth, max_breadth,
                     pagination_max, reopen, elements=None, seen_views=None):
    """
    DFS over ONE panel level. Discovers targets from the current frame (or reuses `elements`),
    drills each SAFE navigation target via observe→act→verify, paginates any sub-view, recurses
    one level deeper, then returns to this level before the next sibling. Coordinates are
    re-discovered if a reset overshoots (point 3f). Returns a list of visited node dicts.

    seen_views: screenshots of sub-views already examined THIS crawl. Many games route several
    menu items (Pays/History/Help) into one shared info carousel; if an option opens a view we've
    already paginated, skip the re-pagination/descend so the crawl doesn't loop over duplicates.
    """
    nodes, visited, acted = [], set(), 0
    if elements is None:
        elements = _describe_merged(base_shot, passes=1)
    if seen_views is None:
        seen_views = []

    while acted < max_breadth:
        target = next((e for e in elements
                       if e.get("center")
                       and (e.get("label") or "").strip()
                       and (e.get("label") or "").strip().lower() not in visited
                       and classify_target(e.get("label"), e.get("type")) == "safe_nav"), None)
        if target is None:
            break
        label = (target.get("label") or "").strip()
        visited.add(label.lower()); acted += 1
        node = {"label": label, "type": target.get("type"), "state": target.get("state"),
                "purpose": target.get("purpose"), "depth": depth, "changed": False,
                "screenshot": None, "leads_to": None, "pages": [], "children": []}

        # observe → act
        pt = _clamp_point(*target["center"])
        try:
            await T.flash_target(page, pt, label, "cyan")
        except Exception:
            pass
        await page.mouse.click(*pt); await asyncio.sleep(1.8)
        after = os.path.join(ss_dir, f"{tag}_d{depth}_{_safe(label)}.png"); await page.screenshot(path=after)
        node["screenshot"] = os.path.basename(after)
        # verify a sub-view opened (image delta)
        node["changed"] = slot_spin.frame_motion(base_shot, after) > CLOSE_DELTA

        if node["changed"]:
            # DEDUPE: if this opened the same view as an option we already examined (shared info
            # carousel), record it and move on — don't re-paginate or recurse into duplicates.
            dup = next((sv for sv in seen_views
                        if slot_spin.frame_motion(sv, after) < DEDUPE_DELTA), None)
            if dup:
                node["leads_to"] = "(same view as already examined — skipped)"
                print(f"    [{tag}] '{label}' opened an already-seen view; skipping re-exploration")
                await _return_to(page, base_shot, ss_dir, f"{tag}_d{depth}_{_safe(label)}", reopen)
                nodes.append(node)
                continue
            seen_views.append(after)
            try:
                info = describe_panel(base_shot, after)
                node["leads_to"] = info.get("title") or info.get("panel_type") or "(sub-view)"
            except Exception:
                node["leads_to"] = "(sub-view)"
            node["pages"] = await _paginate_subview(page, ss_dir, f"{tag}_d{depth}_{_safe(label)}", pagination_max)
            # DESCEND (DFS)
            if depth + 1 < max_depth:
                async def _reopen_sub(_pt=pt):
                    await page.mouse.click(*_clamp_point(*_pt)); await asyncio.sleep(1.2)
                node["children"] = await _dfs_level(page, ss_dir, tag, after, depth + 1,
                                                    max_depth, max_breadth, pagination_max,
                                                    _reopen_sub, seen_views=seen_views)
            # RETURN to this level for the next sibling
            clean = await _return_to(page, base_shot, ss_dir, f"{tag}_d{depth}_{_safe(label)}", reopen)
            if not clean:   # UI shifted — refresh discovery so sibling coords are current
                rescan = os.path.join(ss_dir, f"{tag}_d{depth}_rescan{acted}.png"); await page.screenshot(path=rescan)
                elements = _describe_merged(rescan, passes=1)
        nodes.append(node)
    return nodes


async def dfs_explore(page, opener_label, opener_center, ss_dir, tag=None,
                      max_depth=2, max_breadth=4, pagination_max=4, relocate_fn=None):
    """
    Autonomous DFS menu crawler. Opens the panel (verifying via image delta, with an optional
    `relocate_fn` to re-find a small/misplaced opener), dynamically discovers its targets from the
    live snapshot, then DFS-traverses the SAFE navigation targets — hard-blocking exit/money.
    Returns {panel, opened, shots, notes, base_elements, tree, flat}.
    """
    tag = tag or _safe(opener_label)
    os.makedirs(ss_dir, exist_ok=True)
    out = {"panel": opener_label, "opened": False, "shots": {}, "notes": [],
           "base_elements": [], "tree": [], "flat": []}

    base_before = os.path.join(ss_dir, f"{tag}_base.png"); await page.screenshot(path=base_before)
    menu_shot, base_elements, real_open = None, [], False
    for attempt in (1, 2):
        try:
            await T.flash_target(page, opener_center, opener_label, "orange")
        except Exception:
            pass
        await page.mouse.click(*_clamp_point(*opener_center)); await asyncio.sleep(2.0)
        menu_shot = os.path.join(ss_dir, f"{tag}_panel.png"); await page.screenshot(path=menu_shot)
        base_elements = _describe_merged(menu_shot, passes=2)   # 2-pass: vision under-counts
        # Verify a DISTINCT panel/overlay actually opened. Frame-motion alone is fooled by the
        # reels animating behind the game (it reads "changed" even when nothing opened), so confirm
        # with a before/after panel judgement. This stops a non-opener (e.g. a Home icon) from being
        # reported as a menu just because describe_panel_options listed the on-screen icons.
        try:
            real_open = bool(describe_panel(base_before, menu_shot).get("opened"))
        except Exception:
            real_open = slot_spin.frame_motion(base_before, menu_shot) > OPEN_DELTA
        if base_elements and real_open:
            break
        if attempt == 1 and relocate_fn:    # opener missed — re-locate (e.g. tiny hamburger) & retry
            await page.keyboard.press("Escape"); await asyncio.sleep(0.7)
            new = relocate_fn(base_before)
            if new and new != opener_center:
                print(f"  [{tag}] opener click missed; re-located @ {new}, retrying...")
                opener_center = new

    out["shots"]["panel"] = os.path.basename(menu_shot) if menu_shot else ""
    out["opened"] = bool(base_elements and real_open)
    out["base_elements"] = [{"label": e.get("label"), "type": e.get("type"), "state": e.get("state"),
                             "purpose": e.get("purpose"),
                             "intent": classify_target(e.get("label"), e.get("type"))}
                            for e in base_elements]
    if not out["opened"]:
        out["notes"].append("opener did not open a distinct panel (likely a flat-nav layout)")
        return out

    blocked = [b["label"] for b in out["base_elements"] if b["intent"] in ("exit", "money")]
    if blocked:
        print(f"  [{tag}] hard-blocked transactional/exit targets: {blocked}")

    async def _reopen_menu():
        await page.mouse.click(*_clamp_point(*opener_center)); await asyncio.sleep(1.5)

    out["tree"] = await _dfs_level(page, ss_dir, tag, menu_shot, 0, max_depth, max_breadth,
                                   pagination_max, _reopen_menu, elements=base_elements)

    def _flatten(ns):
        for n in ns:
            out["flat"].append({k: n.get(k) for k in
                                ("label", "type", "state", "purpose", "leads_to",
                                 "screenshot", "depth", "pages")})
            _flatten(n.get("children", []))
    _flatten(out["tree"])

    await page.keyboard.press("Escape"); await asyncio.sleep(1.0)
    return out


# ─── capability: drill into the menu (legacy wrapper over the DFS crawler) ─────
async def drill_menu(page, menu_center, ss_dir, tag="menu", max_options=4):
    """Open the menu and DFS-crawl one level into each safe option, capturing what each shows.
    Thin wrapper over dfs_explore (depth-1) preserving the original {opened, options, notes,
    shots} shape; uses locate_menu_icon to re-find the hamburger if the first click misses."""
    out = await dfs_explore(page, "Menu", menu_center, ss_dir, tag=tag, max_depth=1,
                            max_breadth=max_options, pagination_max=4, relocate_fn=locate_menu_icon)
    options = [{"label": f.get("label"), "screenshot": f.get("screenshot")}
               for f in out["flat"] if f.get("screenshot")]
    return {"opened": out["opened"], "options": options,
            "notes": out["notes"], "shots": out["shots"]}


# ─── capability: page through the paytable ────────────────────────
async def page_through_paytable(page, pt_center, ss_dir, tag="paytable", max_pages=5):
    """Open the paytable/info and page through ALL pages, capturing each as evidence."""
    os.makedirs(ss_dir, exist_ok=True)
    res = {"opened": False, "pages": [], "notes": [], "shots": {}}
    try:
        await T.flash_target(page, pt_center, "Paytable", "orange")
    except Exception:
        pass
    await page.mouse.click(*pt_center); await asyncio.sleep(2.5)

    for i in range(max_pages):
        sh = os.path.join(ss_dir, f"{tag}_p{i}.png"); await page.screenshot(path=sh)
        res["pages"].append(os.path.basename(sh))
        ctrls = detect_panel_controls(sh)
        nxt = next((c for c in ctrls if c.get("center")
                    and any(k in (c.get("label") or "").lower()
                            for k in ("next", "forward", "right arrow", "next page", ">"))), None)
        if not nxt:
            break
        await page.mouse.click(*nxt["center"]); await asyncio.sleep(1.5)

    res["opened"] = len(res["pages"]) > 0
    if i == 0 and len(res["pages"]) == 1:
        res["notes"].append("single page (no next-page control found)")
    await page.keyboard.press("Escape"); await asyncio.sleep(1.0)
    return res


async def _fresh_find(page, ss_dir, name, *keywords):
    """
    Re-detect controls RIGHT BEFORE using one (state shifts after each feature is
    operated, so cached coordinates go stale). Clears any leftover overlay first.
    """
    await page.keyboard.press("Escape"); await asyncio.sleep(0.8)   # clear leftovers
    sh = os.path.join(ss_dir, f"qa_find_{name}.png"); await page.screenshot(path=sh)
    ctrls = T.detect_controls_merged(Image.open(sh), passes=2)
    return next((c for c in ctrls if c.get("center")
                 and any(k in (c.get("label") or "").lower() for k in keywords)), None)


# ─── the agentic sequencer ────────────────────────────────────────
async def qa_explore(page, monitor, ss_dir, region="ZA", caps=None):
    """
    OPERATE each feature like a QA (drive autoplay, drill the menu, page the paytable),
    verifying with ground-truth. Controls are re-detected fresh before EACH capability
    because operating one feature changes the screen. Returns {feature: result}.
    `caps`: iterable of enabled capabilities from {autoplay, menu, paytable} — None = all.
    Skipping a capability avoids its (sometimes heavy) vision work. Provider-independent.
    """
    on = (lambda k: True) if caps is None else (lambda k: k in caps)
    os.makedirs(ss_dir, exist_ok=True)
    await page.screenshot(path=os.path.join(ss_dir, "qa_base.png"))
    bar = T.detect_controls_merged(Image.open(os.path.join(ss_dir, "qa_base.png")), passes=2)
    print(f"  [QA] Features on bar: {[c.get('label') for c in bar]}")
    findings = {}

    if on("autoplay"):
        ap = await _fresh_find(page, ss_dir, "autoplay", "auto", "autospin")
        if not ap:   # fallback: tight-box icon finder (looping-arrows), so one flaky pass ≠ "skipped"
            shot = os.path.join(ss_dir, "qa_find_autoplay.png")
            if not os.path.exists(shot):
                await page.screenshot(path=shot)
            apc = locate_autoplay_button(shot)
            if apc:
                ap = {"label": "Autoplay", "center": apc}
        if ap:
            print(f"  [QA] Driving AUTOPLAY @ {ap['center']}...")
            findings["autoplay"] = await drive_autoplay(page, ap["center"], monitor, ss_dir)

    # EXAMINE the menu. Two layouts: (a) a unified menu button opens a panel of options — open it
    # and DFS-crawl; (b) NO menu button — the nav/settings icons sit directly on the base screen,
    # so discover them and crawl each root icon with the same observe->act->verify loop.
    mn = await _fresh_find(page, ss_dir, "menu", "menu", "setting", "hamburger") if on("menu") else None
    if not on("menu"):
        pass
    elif mn:
        print(f"  [QA] Examining MENU @ {mn['center']}...")
        # depth-1: examine each top-level menu option once (drilling INTO sub-views causes the
        # crawler to re-detect a shared info carousel's own tabs and loop — see Thor's Rage).
        pan = await examine_panel(page, "Menu", mn["center"], ss_dir, tag="menu", max_depth=1)
        if not pan.get("opened"):
            # The detected "menu" opened no real panel (e.g. it was a Home icon, or the nav is
            # flat). Examine the options sitting directly on the base screen instead.
            print("  [QA] Menu opener showed no panel — examining root-level on-screen options...")
            pan = await examine_root_options(page, ss_dir, tag="rootmenu")
        findings["menu"] = pan
    else:
        print("  [QA] No menu button — examining root-level on-screen options...")
        findings["menu"] = await examine_root_options(page, ss_dir, tag="rootmenu")

    # EXAMINE Buy Bonus / Feature Buy if present. This is the MONEY panel — LIST its options
    # (max_drill=0) but do NOT click into them: drilling a feature-buy panel is risky and slow, and
    # listing is enough for QA. Gated with the menu cap (panel exploration).
    bb = await _fresh_find(page, ss_dir, "buybonus", "buy", "bonus", "feature buy") if on("menu") else None
    if bb:
        print(f"  [QA] Examining BUY BONUS @ {bb['center']} (list-only, no clicks)...")
        findings["buybonus"] = await examine_panel(page, "Buy Bonus", bb["center"], ss_dir,
                                                   tag="buybonus", max_drill=0, max_depth=1)

    # PAYTABLE is a CAPABILITY that can live EITHER on the bar OR inside the menu (e.g. Thor's Rage
    # Menu -> "Pays"). If the menu we just examined already covered a paytable/pays/info option,
    # don't run a separate bar-level paytable pass — dedupe so it's tested once, not twice/missed.
    _PT_WORDS = ("pay", "paytable", "info", "rules", "symbol win", "how to")
    menu_opts = (findings.get("menu") or {}).get("options", [])
    menu_has_paytable = any(any(w in (o.get("label") or "").lower() for w in _PT_WORDS) for o in menu_opts)
    pt = await _fresh_find(page, ss_dir, "paytable", "paytable", "info", "pays", "help", "rules") \
        if (on("paytable") and not menu_has_paytable) else None
    if pt:
        print(f"  [QA] Paging PAYTABLE @ {pt['center']}...")
        findings["paytable"] = await page_through_paytable(page, pt["center"], ss_dir)
    elif on("paytable") and menu_has_paytable:
        print("  [QA] Paytable already covered inside the menu — skipping the separate paytable pass.")
    return findings


# ─── examine a panel: WHAT is each option, and what does it do ─────
def describe_panel_options(panel_path) -> list:
    """
    For each interactive option INSIDE an open panel, return a SEMANTIC description —
    not just its location, but WHAT IT IS and what it does. This is the QA's "open the
    panel and look at each option" step. Returns list of dicts with a pixel center.
    """
    prompt = """This screenshot shows a slot game with an OPEN panel / overlay. List the interactive
options INSIDE the panel. Return a JSON array; each item:
{"label": "short name",
 "type": "toggle|button|selector|slider|link|tab|value|close",
 "state": "on|off|selected|a value|null",
 "purpose": "one short sentence: what this option does",
 "opens_subpanel": true|false,
 "box_2d": [ymin,xmin,ymax,xmax]}

CRITICAL — output each PHYSICAL control EXACTLY ONCE:
- Do NOT list the same control under multiple names (e.g. a 'Free Spins' tile is ONE item, not both
  'FREE SPINS option' and 'Free Spins feature'; a stake '-' is ONE item, not 'minus button' AND
  'Decrease stake'). One box = one entry.
- Use a single short, generic label per control. If two candidate names describe the same on-screen
  element, pick one and emit it once.
- box_2d must be a TIGHT box around ONLY the clickable control (its icon + text), so its CENTRE is a
  reliable click point — NOT the whole row/panel width (that pushes the centre into empty space, a
  common miss on side-list menus). Normalize 0-1000.
Ignore the dimmed game behind the panel. Return [] if no panel is open.
Do NOT include the bottom CONTROL BAR (spin / bet +/- / coin +/- / autoplay / balance) or the clock."""
    try:
        # FULL-SCREEN detection at autoplay's fidelity: send the whole panel screenshot (NO crop/
        # zoom = no "pinch"), thumbnailed to 1280 to match parse_panel_context. box_2d is normalized
        # 0-1000 so the higher resolution sharpens detection without affecting coordinate mapping.
        api_img = Image.open(panel_path)
        api_img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        data = T.parse_gemini_json(T.gemini_call([api_img, prompt], _cfg()))
    except Exception:
        return []
    if not isinstance(data, list):
        data = [data]
    out, dropped = [], []
    for it in data:
        box = it.get("box_2d")
        # STEP 4: universal background-bleed filter. The menu/info overlays are centered cards;
        # anything sitting in the bottom band of the screen is the game's control bar — drop it so
        # it never skews menu navigation (the DFS crawler would otherwise try to drill spin/bet).
        ymax = box[2] if isinstance(box, (list, tuple)) and len(box) >= 4 else None
        if ymax is not None and ymax > _BOTTOM_BAND_YMAX:
            dropped.append(it.get("label"))
            continue
        # Deduplicate: if boxes overlap significantly (IoU > 0.2) or are horizontally adjacent with similar names
        is_dup = False
        if isinstance(box, (list, tuple)) and len(box) >= 4:
            lbl = (it.get("label") or "").lower()
            cy = (box[0] + box[2]) / 2.0
            for k in out:
                kbox = k.get("box_2d")
                if not isinstance(kbox, (list, tuple)) or len(kbox) < 4: continue
                iou = T._iou(box, kbox)
                klbl = (k.get("label") or "").lower()
                kcy = (kbox[0] + kbox[2]) / 2.0
                same_y = abs(cy - kcy) < 50
                name_match = (lbl in klbl or klbl in lbl) and len(lbl) > 2
                if iou > 0.2 or (same_y and name_match):
                    is_dup = True
                    break
        if is_dup:
            dropped.append(f"{it.get('label')} (duplicate)")
            continue
        it["center"] = _center(it.get("box_2d"))   # defensive -> None if bad/extra values
        out.append(it)
    if dropped:
        print(f"    [PANEL] filtered {len(dropped)} bottom-bar element(s) from menu options: {dropped}")
    return out


async def examine_panel(page, opener_label, opener_center, ss_dir, tag=None, max_drill=3, max_depth=2):
    """
    Open a panel and EXAMINE it like a QA. Thin wrapper over the DFS crawler (dfs_explore) that
    preserves the legacy contract — {panel, opened, options:[{label,type,state,purpose,leads_to,
    screenshot}], notes, shots}. Lists every discovered option (so the report shows the full menu)
    and enriches the SAFE ones that were drilled with where they lead + a screenshot; deeper
    sub-view discoveries are appended too. Money/exit options are never clicked.
    """
    out = await dfs_explore(page, opener_label, opener_center, ss_dir, tag=tag,
                            max_depth=max_depth, max_breadth=max_drill, pagination_max=4)
    return _shape_examined(out)


def _shape_examined(out):
    """Flatten a crawl result ({base_elements, flat, ...}) into the legacy examine_panel contract —
    {panel, opened, options:[{label,type,state,purpose,leads_to,screenshot}], notes, shots}.
    Lists every top-level option and enriches the SAFE ones that were drilled (where they lead +
    screenshot); deeper sub-view discoveries (depth > 0) are appended. Shared by examine_panel
    (opener-panel crawl) and examine_root_options (base-screen crawl)."""
    drilled = {(f.get("label") or "").strip().lower(): f for f in out["flat"] if f.get("label")}

    options = []
    seen = set()
    for b in out["base_elements"]:                       # full top-level listing
        key = (b.get("label") or "").strip().lower()
        seen.add(key)
        entry = {"label": b.get("label"), "type": b.get("type"),
                 "state": b.get("state"), "purpose": b.get("purpose")}
        d = drilled.get(key)
        if d:
            entry["screenshot"] = d.get("screenshot")
            entry["leads_to"] = d.get("leads_to")
        options.append(entry)
    for f in out["flat"]:                                # sub-view discoveries (depth > 0)
        key = (f.get("label") or "").strip().lower()
        if f.get("depth", 0) > 0 and key not in seen:
            seen.add(key)
            options.append({"label": f.get("label"), "type": f.get("type"),
                            "state": f.get("state"), "purpose": f.get("purpose"),
                            "leads_to": f.get("leads_to"), "screenshot": f.get("screenshot")})

    return {"panel": out["panel"], "opened": out["opened"], "options": options,
            "notes": out["notes"], "shots": out["shots"]}


# ─── capability: examine root-level options when there is NO menu button ─────
def _is_root_nav(label, ttype=None):
    """A base-screen control is a root navigation target only if its intent is safe_nav AND its
    label actually names a nav/info function (settings/info/rules/sound/history/…). Requiring a
    real nav token — not just type=='button' — keeps transactional/spin/autoplay/turbo buttons
    out (classify_target alone would let a bare 'button' through)."""
    if classify_target(label, ttype) != "safe_nav":
        return False
    return any(tok in (label or "").lower() for tok in _NAV_TOKENS)


async def examine_root_options(page, ss_dir, tag="rootmenu", max_options=5):
    """
    NO-MENU-BUTTON layout: many games flatten the hierarchy and place navigation/settings icons
    directly on the base game screen (e.g. grouped top-right) with no unified opener. Discover
    those root-level controls and run the SAME observe->act->verify DFS over them as if they were
    a panel's options — every interaction stays motion-verified and money/exit controls are still
    hard-blocked. Reuses the base-screen detector (detect_controls_merged), the DFS engine
    (_dfs_level -> _return_to -> describe_panel_options), and the examine_panel result shape.
    """
    os.makedirs(ss_dir, exist_ok=True)
    base = os.path.join(ss_dir, f"{tag}_base.png"); await page.screenshot(path=base)

    # Discover the scattered nav/settings icons. describe_panel_options labels small corner icons
    # better (gear -> "Settings", clock -> "Game History") than the bar detector, which can mislabel
    # them (gear -> "Turbo"); union both for recall, then keep only real navigation/info icons.
    # Transactional/spin/autoplay buttons are filtered out by _is_root_nav (never clicked).
    candidates = describe_panel_options(base) + T.detect_controls_merged(Image.open(base), passes=1)
    roots, seen = [], set()
    for c in candidates:
        label = (c.get("label") or "").strip()
        if not (label and c.get("center")) or label.lower() in seen:
            continue
        if _is_root_nav(label, c.get("type")):
            seen.add(label.lower()); roots.append(c)
    roots = roots[:max_options]

    out = {"panel": "On-screen options", "opened": bool(roots),
           "shots": {"panel": os.path.basename(base)}, "notes": [],
           "base_elements": [{"label": c.get("label"), "type": c.get("type"),
                              "state": c.get("state"), "purpose": c.get("purpose"),
                              "intent": "safe_nav"} for c in roots],
           "tree": [], "flat": []}
    if not roots:
        out["notes"].append("no menu button and no on-screen settings/info controls detected")
        return _shape_examined(out)
    print(f"  [root] no menu button — examining {len(roots)} on-screen control(s): "
          f"{[c.get('label') for c in roots]}")

    async def _noop_reopen():
        return   # nothing to reopen: the base game screen IS the root level

    # base_shot = the live game screen; each root icon is a depth-0 node. _dfs_level does, per icon:
    # click -> verify a sub-view opened (frame motion) -> describe/paginate -> _return_to (Escape /
    # detected Close) -> verify back at base -> next icon. Coords for siblings stay valid because
    # the base screen is unchanged after each clean return.
    # depth-1: click each root icon, verify/capture what opens, return — don't recurse into the
    # opened panel's internals (keeps it bounded and matches the per-icon QA loop).
    out["tree"] = await _dfs_level(page, ss_dir, tag, base, 0, 1, max_options, 4,
                                   _noop_reopen, elements=roots)

    def _flatten(ns):
        for n in ns:
            out["flat"].append({k: n.get(k) for k in
                                ("label", "type", "state", "purpose", "leads_to",
                                 "screenshot", "depth", "pages")})
            _flatten(n.get("children", []))
    _flatten(out["tree"])

    await page.keyboard.press("Escape"); await asyncio.sleep(0.6)
    return _shape_examined(out)


# ─── shared discovery helper (used by the DFS crawler) ────────────
def _box_iou(a, b):
    """Intersection-over-union of two box_2d [ymin,xmin,ymax,xmax]; 0 if either is unusable."""
    try:
        ay0, ax0, ay1, ax1 = a[:4]; by0, bx0, by1, bx1 = b[:4]
    except Exception:
        return 0.0
    iy0, ix0 = max(ay0, by0), max(ax0, bx0)
    iy1, ix1 = min(ay1, by1), min(ax1, bx1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0, (ay1 - ay0)) * max(0, (ax1 - ax0)) + max(0, (by1 - by0)) * max(0, (bx1 - bx0)) - inter
    return inter / ua if ua > 0 else 0.0


def _describe_merged(panel_path, passes=2, iou_dedupe=0.6):
    """Describe a screen's options, multi-pass-merged. Dedupe is BOTH by label AND SPATIALLY: the
    vision model often names the same physical control differently across passes (e.g. 'FREE SPINS
    option' vs 'Free Spins feature', 'minus button' vs 'Decrease stake'), so we also drop any element
    whose box overlaps an already-kept one (IoU > threshold). This keeps the option list clean and
    is generic across providers."""
    merged = {}
    for _ in range(passes):
        for o in describe_panel_options(panel_path):
            k = (o.get("label") or "").strip().lower()
            if k and k not in merged:
                merged[k] = o
    # spatial dedupe: collapse different labels that point at the same control
    out = []
    for o in merged.values():
        box = o.get("box_2d")
        if isinstance(box, (list, tuple)) and len(box) >= 4 and \
                any(_box_iou(box, k.get("box_2d")) > iou_dedupe for k in out):
            continue
        out.append(o)
    return out


async def agent_explore(page, opener_label, opener_center, ss_dir, tag=None, max_steps=7, max_depth=2):
    """
    Autonomous DFS explorer (replaces the old LLM-planner loop). Opens a panel and depth-first
    crawls its SAFE navigation targets, hard-blocking money/exit, paginating sub-views, and
    verifying every step by image delta. `max_steps` bounds the breadth per level. Returns the
    full DFS tree plus a flat visit trace.
    """
    out = await dfs_explore(page, opener_label, opener_center, ss_dir, tag=tag,
                            max_depth=max_depth, max_breadth=max_steps, pagination_max=4)
    trace = [{"depth": f.get("depth"), "clicked": f.get("label"), "type": f.get("type"),
              "leads_to": f.get("leads_to"), "pages": len(f.get("pages") or []),
              "shot": f.get("screenshot")} for f in out["flat"]]
    return {"panel": out["panel"], "opened": out["opened"], "trace": trace,
            "tree": out["tree"], "base_elements": out["base_elements"],
            "notes": out["notes"], "shots": out["shots"]}
