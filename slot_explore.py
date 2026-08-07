"""
slot_explore.py — PROVIDER-INDEPENDENT recursive control-tree explorer.
================================================================================
Slot UIs are a tree, not a flat bar: the Menu / Info / Autoplay buttons open
PANELS that contain more controls (paytable, settings, autoplay config, bet
selector, rules, history). This maps that tree for ANY game from ANY provider:

  for each panel-style control on the bar:
      click it  ->  screenshot  ->  ask Gemini "did a panel open? describe it"
      capture it as evidence  ->  close it (X/Back -> Esc -> safe tap)  ->  verify closed

Everything is vision-driven (Gemini detects/describes; pixel-diff verifies close),
so nothing is hard-coded to a provider's layout or coordinates.
"""
import os
import re
import asyncio
from PIL import Image
from google.genai import types

import test_spin_button as T   # gemini_call, parse_gemini_json, detect_all_controls
import slot_spin               # frame_motion
import config_env              # single source of truth for viewport + clamped scaling

# Bar controls worth opening (semantic, not provider-specific).
PANEL_TARGETS = ("menu", "info", "paytable", "setting", "help", "rules",
                 "auto", "bonus", "bank", "history")
# Whole-WORD skip tokens — never click these while mapping (cost money / mutate bet /
# tested elsewhere). Word-boundary matching so "spin" skips "Spin Button" but NOT
# "Autospin", and "bet" skips "Bet Display" but not unrelated labels.
SKIP_WORDS = ("spin", "bet", "max", "balance", "jackpot", "display", "turbo", "increase", "decrease")


def _skip(label_low: str) -> bool:
    return any(re.search(rf"\b{w}\b", label_low) for w in SKIP_WORDS)

CLOSE_MOTION_THRESH = 6.0   # below this vs base = panel gone (ambient motion is ~1-2)


def _cfg():
    return types.GenerateContentConfig(response_mime_type="application/json",
                                       thinking_config=types.ThinkingConfig(thinking_budget=0))


def _thumb(path):
    im = Image.open(path)
    im.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
    return im


def _center(box):
    """Box [ymin,xmin,ymax,xmax] (0-1000) -> CLAMPED pixel center via the shared scaler.
    Routes through config_env so it uses the live viewport and never returns an off-screen point."""
    return config_env.norm_box_center(box)


def _safe(label):
    return "".join(c for c in label if c.isalnum() or c in " _-").strip().replace(" ", "_")[:24] or "ctrl"


def describe_panel(before_path, after_path) -> dict:
    """Ask Gemini whether a panel opened between two frames, and describe it."""
    prompt = """You are shown two screenshots of a slot game: the FIRST is BEFORE clicking a
control, the SECOND is AFTER. Did a panel / overlay / popup / menu OPEN in the AFTER image
(e.g. paytable, settings, autoplay config, bet or coin selector, rules, info, game history)?

Return JSON:
{"opened": true|false,
 "panel_type": "paytable|settings|autoplay|bet_selector|rules|info|history|other|none",
 "title": "short title or ''",
 "buttons": ["short labels of the interactive items inside the opened panel"],
 "close_button": {"label": "X|Back|Close|OK", "box_2d": [ymin,xmin,ymax,xmax]} or null}

Only include close_button if a clear way to close the panel is visible. Normalize box_2d to 0-1000."""
    try:
        return T.parse_gemini_json(T.gemini_call([_thumb(before_path), _thumb(after_path), prompt], _cfg()))
    except Exception as e:
        return {"opened": False, "panel_type": "none", "error": str(e)}


def detect_panel_controls(panel_path) -> list:
    """
    Detect the interactive controls INSIDE an open panel/overlay (buttons, toggles,
    number options, +/- steppers, tabs, close/confirm) WITH pixel centers, so they
    can be clicked. Vision-driven — works for any panel from any provider.
    Returns [{"label": str, "center": (x, y)}].
    """
    prompt = """This screenshot shows a slot game with an OPEN panel / overlay (e.g. autoplay
config, settings, paytable, bet or coin selector, rules, history). Detect ONLY the interactive
controls INSIDE that panel/overlay — buttons, toggles/switches, selectable number options,
+/- steppers, sliders, tabs, and close/back/confirm buttons. IGNORE the dimmed game behind it.

Return a JSON array; each item:
{"label": "specific name (e.g. 'Autoplay 25 spins', 'Sound on/off', 'Start', 'Close', 'Next page')",
 "box_2d": [ymin, xmin, ymax, xmax]}
box_2d must be a TIGHT box around ONLY the clickable control itself (its icon + its text label), so
its CENTRE is a reliable click point — do NOT include the surrounding row whitespace, separators, or
the whole panel width, which would push the centre off the control (common with side-list menus).
Normalize box_2d to 0-1000. Return [] if no panel controls are visible."""
    try:
        # FULL-SCREEN detection at 1280 (matches the autoplay panel reader): whole screenshot,
        # no crop/zoom ("pinch"). box_2d is normalized 0-1000 so resolution doesn't shift coords.
        api_img = Image.open(panel_path)
        api_img.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        data = T.parse_gemini_json(T.gemini_call([api_img, prompt], _cfg()))
    except Exception:
        return []
    if not isinstance(data, list):
        data = [data]
    out = []
    for it in data:
        ctr = _center(it.get("box_2d"))   # defensive: handles bad/extra values -> None
        if ctr:
            out.append({"label": it.get("label", "?"), "center": ctr})
    return out


def find_panel_control(controls, *keywords):
    """First panel control whose label contains any keyword (case-insensitive)."""
    for c in controls:
        low = (c.get("label") or "").lower()
        if any(k.lower() in low for k in keywords):
            return c
    return None


async def click_panel_control(page, controls, *keywords) -> bool:
    """Click the first in-panel control matching any keyword. Returns True if clicked."""
    c = find_panel_control(controls, *keywords)
    if c and c.get("center"):
        await page.mouse.click(*c["center"])
        await asyncio.sleep(1.5)
        return True
    return False


async def _is_closed(page, base_path, ss_dir, tag) -> bool:
    chk = os.path.join(ss_dir, f"close_chk_{tag}.png")
    await page.screenshot(path=chk)
    return slot_spin.frame_motion(base_path, chk) < CLOSE_MOTION_THRESH


async def close_panel(page, base_path, ss_dir, tag, close_btn=None) -> bool:
    """Generic, provider-independent close: detected X/Back -> Escape -> safe top tap."""
    # 1) explicit close button if Gemini found one
    cb_ctr = _center(close_btn.get("box_2d")) if close_btn else None
    if cb_ctr:
        await page.mouse.click(*cb_ctr); await asyncio.sleep(1.5)
        if await _is_closed(page, base_path, ss_dir, tag):
            return True
    # 2) Escape
    await page.keyboard.press("Escape"); await asyncio.sleep(1.0)
    if await _is_closed(page, base_path, ss_dir, tag):
        return True
    # 3) tap a usually-empty safe spot (top-centre), away from the bar/spin
    await page.mouse.click(*config_env.clamp_point(config_env.VIEWPORT_WIDTH * 0.5,
                                                    config_env.VIEWPORT_HEIGHT * 0.06))
    await asyncio.sleep(1.0)
    if await _is_closed(page, base_path, ss_dir, tag):
        return True
    # 4) last resort: ask Gemini to re-locate a close control on the CURRENT screen
    cur = os.path.join(ss_dir, f"reclose_{tag}.png"); await page.screenshot(path=cur)
    info = describe_panel(base_path, cur)
    cb = info.get("close_button")
    cb2_ctr = _center(cb.get("box_2d")) if cb else None
    if cb2_ctr:
        await page.mouse.click(*cb2_ctr); await asyncio.sleep(1.0)
        if await _is_closed(page, base_path, ss_dir, tag):
            return True
        await page.keyboard.press("Escape"); await asyncio.sleep(1.0)
    return await _is_closed(page, base_path, ss_dir, tag)


SPIN_MOTION_THRESH = 5.0   # ongoing motion after a click => reels are spinning


async def _ongoing_motion(page, ss_dir, tag):
    """Two quick frames ~1.2s apart; high diff => something is still animating (a spin)."""
    a = os.path.join(ss_dir, f"mv_a_{tag}.png"); await page.screenshot(path=a)
    await asyncio.sleep(1.2)
    b = os.path.join(ss_dir, f"mv_b_{tag}.png"); await page.screenshot(path=b)
    return slot_spin.frame_motion(a, b), b


async def _wait_settle(page, ss_dir, tag, last, cap=14.0):
    """After an accidental spin, wait for the reels to settle so later state isn't corrupted."""
    t = 0.0
    while t < cap:
        s = os.path.join(ss_dir, f"st_{tag}_{t:04.1f}.png"); await page.screenshot(path=s)
        if slot_spin.frame_motion(last, s) < 3.0:
            return
        last = s; await asyncio.sleep(0.8); t += 0.8


async def explore_control(page, label, center, base_path, ss_dir) -> dict:
    """
    Open one control with a reliability GUARD, describe/capture the panel, then close.
      - verify the panel actually opened (vision),
      - SPIN-SAFETY: if the click instead started a spin (ongoing reel motion), record it
        and DO NOT re-click (re-clicking would cost another real spin),
      - RETRY: only when the click did nothing (static, no spin) — safe to click again.
    """
    node = {"label": label, "opened": False, "closed": None, "triggered_spin": False,
            "panel_type": None, "title": "", "controls": [], "buttons": [], "screenshot": ""}
    tag = _safe(label)

    for attempt in (1, 2):
        # Visibly box the control we're about to click (so a watcher sees it's real).
        try:
            await T.flash_target(page, center, label, "orange")
        except Exception:
            pass
        await page.mouse.click(*center); await asyncio.sleep(2.0)
        after = os.path.join(ss_dir, f"panel_{tag}.png"); await page.screenshot(path=after)
        node["screenshot"] = os.path.basename(after)
        info = describe_panel(base_path, after)

        if info.get("opened"):
            controls = detect_panel_controls(after)   # in-panel buttons, with centers
            node.update({
                "opened": True,
                "panel_type": info.get("panel_type"),
                "title": info.get("title", ""),
                "controls": controls,
                "buttons": [c["label"] for c in controls] or info.get("buttons", []),
            })
            node["closed"] = await close_panel(page, base_path, ss_dir, tag, info.get("close_button"))
            return node

        # Not opened — did we accidentally trigger a spin?
        mv, last = await _ongoing_motion(page, ss_dir, f"{tag}{attempt}")
        if mv > SPIN_MOTION_THRESH:
            node["triggered_spin"] = True
            await _wait_settle(page, ss_dir, tag, last)   # let it finish; protect later tests
            return node                                   # never re-click after a spin

        # Static and not opened => the click missed; a re-click is safe (no spin fired).
        if attempt == 1:
            continue
    return node


async def map_control_tree(page, ss_dir, targets=PANEL_TARGETS) -> dict:
    """
    Detect the bar, then open/describe/close each panel-style control.
    Returns {"bar": [...labels], "panels": [node, ...]}. Provider-independent.
    """
    os.makedirs(ss_dir, exist_ok=True)
    base = os.path.join(ss_dir, "tree_base.png")
    await page.screenshot(path=base)
    controls = T.detect_controls_merged(Image.open(base), passes=2)  # 2-pass merge reduces misses
    tree = {"bar": [c.get("label") for c in controls], "panels": []}

    seen = set()
    for c in controls:
        label = (c.get("label") or "")
        low = label.lower()
        if not c.get("center"):
            continue
        if _skip(low):
            continue
        if not any(t in low for t in targets):
            continue
        if low in seen:
            continue
        seen.add(low)
        node = await explore_control(page, label, c["center"], base, ss_dir)
        tree["panels"].append(node)
        # Re-baseline in case closing left the UI slightly different.
        await asyncio.sleep(1.0)
        await page.screenshot(path=base)
    return tree
