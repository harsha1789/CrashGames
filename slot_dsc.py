"""
slot_dsc.py — DSC (Daily Sanity Check): the fast per-game check for the ~300-games/day sweep.
================================================================================
run_tests handles launch + startup, then hands over here. The flow is deliberately minimal —
one merged detection pass, floor EVERY stake stepper (coin-model games have several), one
spin through slot_spin.spin_and_measure (one retry if unverified) — and the outcome maps
onto the team's report:

    Launch     — the game reached a playable screen (spin control detected, or a full
                 control bar if vision missed the spin itself)
    Bet Placed — the spin verifiably executed: a parseable money result on the wire, OR
                 reel motion corroborated by an actual balance movement
    Tlogs      — STRICT network truth: the response parsed into wager/payout/balance
                 (source == "network"). A body on some endpoint is not a transaction —
                 honest-Fail beats fake-Pass in a daily sanity report.

Hard lessons baked in (2026-07-09): a "Spin Gifts" promo badge outranked the real spin
button and its campaign popup's XHR was reported as a completed spin; a coin-model game
(bet = level × coin × lines) spun at 2× the catalog minimum because only one of its two
steppers was floored; a provider's response field disagreed with the actual balance
movement, so the balance delta is the wager of record when they conflict.
"""
import os
import time
import asyncio
from datetime import datetime

from PIL import Image

# Same deferred-host pattern as slot_spin: this module is imported inside run_tests, so
# the host module is fully initialized by then (under either name, __main__ or import).
import test_spin_button as T
import slot_spin
import config_env

BET_FLOOR_CLICKS = 8      # Decrease presses per stepper to reach its floor
BET_CLICK_GAP = 0.25      # tighter than the full suite's 0.4s — DSC optimizes for speed
STAKE_EPS = 0.011         # money-comparison tolerance (matches _evaluate_spin's)

# Promo/feature controls whose labels contain "spin" but are NOT the spin button.
_SPIN_EXCLUDE = ("auto", "turbo", "fast", "speed", "count",
                 "gift", "buy", "bonus", "free", "respin", "prize")
_DEC_KEYWORDS = ("decrement", "decrease", "bet -", "bet down", "coin -", "level -",
                 "minus", "lower")


def _pick_spin(controls):
    """The main spin button. Exact label first — keyword matching alone let the 'Spin
    Gifts' promo badge outrank 'Spin Button' on Thor's Rage."""
    for c in controls:
        if (c.get("label") or "").strip().lower() in ("spin button", "spin") and c.get("center"):
            return c
    # "Hold for Turbo Spin" family (3 Oaks et al.): the hint text is printed ON the main
    # spin button, so detection labels it a turbo control. A label with both "spin" and
    # "hold" IS the spin button — hold-to-turbo only ever annotates the real one.
    for c in controls:
        label = (c.get("label") or "").lower()
        if "spin" in label and "hold" in label and c.get("center") \
                and not any(x in label for x in ("gift", "buy", "bonus", "free", "respin",
                                                 "prize", "auto")):
            return c
    for c in controls:
        label = (c.get("label") or "").lower()
        if "spin" in label and c.get("center") \
                and not any(x in label for x in _SPIN_EXCLUDE):
            return c
    return None


_SPIN_REASK = """This is a screenshot of a slot game. Identify the MAIN SPIN button — the large
button (usually round, with circular/curved arrows or a play triangle) that starts ONE spin when
clicked. It may have hint text like "Hold for Turbo Spin" attached — that is still the spin button.
Do NOT return: an autoplay button, a small turbo/fast-speed toggle, promo badges (e.g. "Spin
Gifts"), buy-bonus/feature-buy, or menu icons. If there is genuinely no spin button (not a slot
game), say so.
Return JSON: {"found": true/false, "box_2d": [ymin, xmin, ymax, xmax]} (box normalized 0-1000)."""


def _find_spin_by_reask(shot_path, controls):
    """Last-resort targeted vision pass when the merged detection produced no usable spin
    label. One cheap, single-purpose question beats skipping a real slot as 'possibly not a
    slot game'. A result landing on a known promo/purchase control is rejected."""
    try:
        img = Image.open(shot_path)
        img.thumbnail([1280, 1280], Image.Resampling.LANCZOS)
        cfg = T.types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=T.types.ThinkingConfig(thinking_budget=0))
        data = T.parse_gemini_json(T.gemini_call([img, _SPIN_REASK], cfg)) or {}
    except Exception:
        return None
    box = data.get("box_2d") if data.get("found") else None
    center = config_env.norm_box_center(box) if box else None
    if not center:
        return None
    for c in controls:
        lbl = (c.get("label") or "").lower()
        if c.get("center") and any(k in lbl for k in ("gift", "buy", "bonus", "prize")) \
                and abs(center[0] - c["center"][0]) < 30 and abs(center[1] - c["center"][1]) < 30:
            return None
    return {"label": "Spin Button (re-ask)", "center": center, "box_2d": box}


def _pick_spin_relaxed(controls):
    """Last-resort spin pick: allows turbo/fast-labeled controls. On many desktop slots
    (3 Oaks: 'Hold for Turbo Spin' tooltip ON the main button) the REAL spin control
    gets labeled 'Turbo Spin'. Never autoplay/promo controls."""
    hard_exclude = ("auto", "count", "gift", "buy", "bonus", "free", "respin", "prize")
    for c in controls:
        label = (c.get("label") or "").lower()
        if "spin" in label and c.get("center") \
                and not any(x in label for x in hard_exclude):
            return c
    return None


def _retry_target(controls, failed_center):
    """Spin target for the one bounded retry, from a FRESH detection. The first target
    can be a look-alike symbol (3 China Pots: a round blue '1.00' coin ON THE REELS was
    labeled 'Spin Button'; the real control was labeled 'Turbo Spin'). Prefer a strict
    pick away from the failed spot, then a relaxed pick away, and only re-click the same
    spot when nothing better exists (the popup-swallowed-click case)."""
    fx, fy = failed_center

    def near(c):
        return abs(c["center"][0] - fx) <= 30 and abs(c["center"][1] - fy) <= 30
    away = [c for c in controls if c.get("center") and not near(c)]
    cand = _pick_spin(controls)
    if cand and near(cand):
        cand = _pick_spin(away) or _pick_spin_relaxed(away) or cand
    elif cand is None:
        cand = _pick_spin_relaxed(away)
    return cand


def _decrement_controls(controls):
    """EVERY stepper that lowers the stake. Coin-model games (e.g. Habanero: bet =
    level × coin × lines) expose several; flooring only one leaves the stake above the
    real minimum."""
    # Centers of stake-RAISING controls ("+", Max Bet): a "decrement" whose box lands on one
    # of these is the same physical button double-labeled — vision does this when the real "-"
    # renders disabled/dim at min stake (Thor's Rage 2026-07-09: both "Bet Decrement" entries
    # sat on the "+" and 16 blind clicks pumped the stake to 300).
    ups = [c["center"] for c in controls
           if c.get("center") and any(k in (c.get("label") or "").lower()
                                      for k in ("increment", "increase", "+", "max bet"))]
    out = []
    for c in controls:
        label = (c.get("label") or "").lower()
        if c.get("center") and any(k in label for k in _DEC_KEYWORDS) \
                and not any(x in label for x in ("increment", "increase", "+", "max")):
            cx, cy = c["center"]
            if any(abs(cx - ux) < 25 and abs(cy - uy) < 25 for ux, uy in ups):
                continue
            # Deduplicate if another control has essentially the same center
            if not any(abs(cx - o["center"][0]) < 20 and abs(cy - o["center"][1]) < 20 for o in out):
                out.append(c)
    return out[:3]


_BET_MENU_PROMPT = """This is a slot game with an OPEN bet/stake selection menu — a panel or list
of selectable bet values (e.g. 0.10 0.20 0.50 1.00 ...). Find the SMALLEST selectable bet value
option in that menu. Do NOT return: the balance, a win amount, spin counts, max-bet, or the
currently displayed total-bet readout — only the smallest clickable VALUE OPTION.
Return JSON: {"found": true/false, "value": "<the number>", "box_2d": [ymin, xmin, ymax, xmax]}
(box normalized 0-1000). If no bet menu is open in the screenshot, return {"found": false}."""


def _min_bet_in_menu(shot_path):
    """Targeted single-purpose vision pass over an OPEN bet menu (same pattern as
    _find_spin_by_reask). Not slot_agent.parse_panel_context: its bottom-band filter drops
    non-core elements in the lower 20% of the screen, which is exactly where bottom-sheet
    bet menus put their value chips."""
    try:
        img = Image.open(shot_path)
        img.thumbnail([1280, 1280], Image.Resampling.LANCZOS)
        cfg = T.types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=T.types.ThinkingConfig(thinking_budget=0))
        data = T.parse_gemini_json(T.gemini_call([img, _BET_MENU_PROMPT], cfg)) or {}
    except Exception:
        return None
    box = data.get("box_2d") if data.get("found") else None
    center = config_env.norm_box_center(box) if box else None
    if not center:
        return None
    return {"value": T.parse_amount(str(data.get("value") or "")), "center": center}


def _bet_menu_opener(controls):
    """The control that opens the bet/stake menu on games with no on-screen '-' stepper —
    a 'Bet' / 'Stake' button or a coins/chips icon. Readout labels ('Bet Display',
    'Balance …') are text, not buttons — clicking them does nothing (Bison Prime
    2026-07-16), so they are excluded."""
    bad = ("max", "auto", "spin", "buy", "bonus", "free", "increment", "increase",
           "decrement", "decrease", "minus", "plus", "display", "balance", "win",
           "paylines")
    return next((c for c in controls if c.get("center")
                 and any(k in (c.get("label") or "").lower()
                         for k in ("bet", "stake", "coin", "chip"))
                 and not any(x in (c.get("label") or "").lower() for x in bad)), None)


_BET_OPENER_PROMPT = """This is a screenshot of a slot game. Find the control that OPENS the
bet/stake selection menu — commonly a coins/chips-stack icon, a '$' button, or a BET button,
usually near the spin button. Clicking it opens a panel with +/- steppers or a list of bet
values. Do NOT return: the spin button, autoplay, turbo/lightning, menu/hamburger,
info/paytable, settings-gear-with-no-bet-purpose, or the bet AMOUNT text readout itself.
Return JSON: {"found": true/false, "box_2d": [ymin, xmin, ymax, xmax]} (box normalized 0-1000).
If no such control exists, return {"found": false}."""


def _find_bet_opener_by_reask(shot_path):
    """Targeted pass for the bet-menu opener when no control LABEL suggests one — Bison
    Prime's coins icon was labeled 'Settings' while the only bet-labeled control was the
    inert 'Bet Display' readout. Same pattern as _find_spin_by_reask."""
    try:
        img = Image.open(shot_path)
        img.thumbnail([1280, 1280], Image.Resampling.LANCZOS)
        cfg = T.types.GenerateContentConfig(
            response_mime_type="application/json",
            thinking_config=T.types.ThinkingConfig(thinking_budget=0))
        data = T.parse_gemini_json(T.gemini_call([img, _BET_OPENER_PROMPT], cfg)) or {}
    except Exception:
        return None
    box = data.get("box_2d") if data.get("found") else None
    center = config_env.norm_box_center(box) if box else None
    if not center:
        return None
    return {"label": "Bet Menu (re-ask)", "center": center, "box_2d": box}


async def _floor_via_bet_menu(page, controls, stake, ss_dir, errors, expected=None,
                              base_shot=None):
    """No '-' stepper detected on the BASE screen: some games set the stake through a bet
    button that opens a menu (reported 2026-07-15/16). The menu itself varies by provider —
    Bison Prime's coins icon opens a panel with its own +/- steppers; others show a grid of
    selectable value chips. Steppers get the same closed-loop flooring as the base screen;
    otherwise click the SMALLEST chip. Close the menu, re-read the stake. Returns
    (stake, handled, at_floor); handled False means no usable opener was found and the caller
    should report the original no-stepper error. at_floor True means the achieved stake is the
    game minimum (smallest chip, or the in-menu stepper bottomed out).
    ponytail: chip path picks from the visible chips only — a scrollable/paginated menu
    whose true minimum is off-screen selects the smallest VISIBLE value; add scroll
    handling if a real game needs it."""
    opener = _bet_menu_opener(controls)
    if not opener and base_shot:
        opener = _find_bet_opener_by_reask(base_shot)
        if opener:
            # Never click a re-asked box that landed on a hazardous control.
            hazards = _hazard_boxes(controls, None, ("auto", "spin", "max", "turbo"))
            if any(_point_in_box(opener["center"], c["box_2d"]) for c in hazards):
                print(f"  [DSC] Re-asked bet opener {opener['center']} collides with a "
                      f"spin/autoplay control — ignored")
                opener = None
    if not opener:
        return stake, False, False
    print(f"  [DSC] No stepper — trying bet menu via '{opener.get('label')}' at {opener['center']}")
    try:
        await T.flash_target(page, opener["center"], "bet menu", "cyan", hold=0.4)
    except Exception:
        pass
    await page.mouse.click(*opener["center"])
    await asyncio.sleep(1.4)
    shot = os.path.join(ss_dir, "dsc_betmenu.png")
    await page.screenshot(path=shot)
    decs = _decrement_controls(T.detect_controls_merged(Image.open(shot), passes=1))
    if decs:
        print(f"  [DSC] Bet menu has {len(decs)} stepper(s) — flooring inside the menu")
        at_floor = False
        for i, dec in enumerate(decs, 1):
            if stake is not None and expected is not None and stake <= expected + STAKE_EPS:
                at_floor = True
                break
            stake, trusted, at_floor = await _probe_and_floor(page, dec, stake, ss_dir, errors,
                                                              f"m{i}", expected=expected)
            if not trusted:
                break   # mislabeled increment inside the menu — don't ride the ladder up
        await T._dismiss_overlays(page)
        new = await _read_stake(page, ss_dir, "betmenu")
        return (new if new is not None else stake), True, at_floor
    pick = _min_bet_in_menu(shot)
    if not pick:
        errors.append(f"'{opener.get('label')}' opened no recognizable bet menu "
                      f"(no steppers, no value chips); spun at current stake")
        await T._dismiss_overlays(page)
        return stake, True, False
    val = pick["value"]
    print(f"  [DSC] Bet menu: selecting lowest value "
          f"{f'{val:g}' if val is not None else '?'} at {pick['center']}")
    await page.mouse.click(*pick["center"])
    await asyncio.sleep(0.8)
    await T._dismiss_overlays(page)      # close the panel if selecting didn't
    new = await _read_stake(page, ss_dir, "betmenu")
    if new is not None and stake is not None and new > stake + STAKE_EPS:
        errors.append(f"bet-menu selection RAISED the stake ({stake:g} → {new:g})")
    # The smallest value in the bet menu IS the game minimum — a chip menu shows every
    # selectable stake at once, so the lowest chip is definitionally the floor.
    return (new if new is not None else stake), True, True


async def _read_stake(page, ss_dir, tag):
    """Current on-screen stake from a fresh screenshot, or None if unreadable."""
    shot = os.path.join(ss_dir, f"dsc_stake_{tag}.png")
    await page.screenshot(path=shot)
    try:
        vals = T.read_game_values(Image.open(shot)) or {}
    except Exception:
        return None
    return T.parse_amount(vals.get("bet") or "")


async def _probe_and_floor(page, dec, stake, ss_dir, errors, tag, expected=None):
    """CLOSED-LOOP stepper flooring: one probe click, re-read the stake, and commit the
    remaining clicks only if the stake did not RISE. A stepper that raises the stake is a
    mislabeled increment — abandon it immediately instead of riding the ladder up.
    Returns (new_stake, trusted, at_floor). at_floor=True means the stepper hit its own
    floor (value stopped moving) or reached the catalog target — i.e. this IS the game
    minimum. at_floor=False means we stopped for another reason (unreadable, or the round
    cap while the value was still dropping), so the minimum may not be reached."""
    label = dec.get("label")
    print(f"  [DSC] Flooring '{label}': probe + verified rounds at {dec['center']}")
    try:
        await T.flash_target(page, dec["center"], f"{label} (to min)", "cyan", hold=0.4)
    except Exception:
        pass
    await page.mouse.click(*dec["center"])
    await asyncio.sleep(0.8)
    probed = await _read_stake(page, ss_dir, f"probe_{tag}")
    if stake is not None and probed is not None and probed > stake + STAKE_EPS:
        errors.append(f"'{label}' RAISED the stake ({stake:g} → {probed:g}) — "
                      f"mislabeled increment, abandoned after one click")
        print(f"  [DSC] ⚠️ '{label}' raised the stake {stake:g} → {probed:g}; abandoning it")
        return probed, False, False
    # Commit clicks in ROUNDS, re-reading between them: a stake parked far above the minimum
    # (e.g. persisted server-side at yesterday's 300) needs more than one batch of 8. Stop at
    # the catalog min, when the value stops moving (this stepper's floor), or after 4 rounds.
    cur = probed if probed is not None else stake
    for rnd in range(4):
        for _ in range(BET_FLOOR_CLICKS - (1 if rnd == 0 else 0)):
            await page.mouse.click(*dec["center"])
            await asyncio.sleep(BET_CLICK_GAP)
        new = await _read_stake(page, ss_dir, f"floored_{tag}_r{rnd}")
        if new is None:
            return cur, True, False              # unreadable — keep best-known, floor unknown
        if expected is not None and new <= expected + STAKE_EPS:
            print(f"  [DSC] Stake floored to the catalog minimum ({new:g})")
            return new, True, True
        if cur is not None and new >= cur - STAKE_EPS:
            print(f"  [DSC] Stepper bottomed out at {new:g} — this is the game minimum")
            return new, True, True               # stopped moving — this stepper's UI floor
        cur = new
    return cur, True, False                       # round cap hit while still dropping


def _evaluate_spin(rep, expected_min_text="", at_min_floor=False):
    """Pure verdict logic over a spin_and_measure report — kept side-effect-free so the
    Thor's Rage / Hot Hot failure shapes stay replayable in offline tests.
    at_min_floor: True when flooring reached the game's TRUE minimum (stepper bottomed out or
    smallest bet-menu chip). When True, the achieved stake IS the minimum and is NOT faulted
    against the (often per-line/stale) catalog min.
    Returns {bet_placed, tlogs, wager, wager_effective, payout, source, errors}."""
    v = rep.get("values", {})
    status = v.get("status", "ok")
    errors = []

    net_ok = v.get("source") == "network"
    bb, ba = v.get("balance_before"), v.get("balance_after")
    bal_moved = bb is not None and ba is not None and abs(ba - bb) > 0.001

    # Money-movement invariant (3 China Pots 2026-07-13 false Pass): a "network" result
    # whose own numbers show nothing moved — balance unchanged, no payout, reels still —
    # is a state/heartbeat echo, not a transaction. slot_spin now rejects these at the
    # source; this mirror keeps the pure verdict safe against any regression there.
    if net_ok and bb is not None and ba is not None and not bal_moved \
            and not rep.get("reels_moved") and v.get("payout") is None:
        net_ok = False
        errors.append("network data carried no money movement — not accepted as a spin")

    # Balance movement corroborated by EITHER action signal (reel motion or a captured
    # spin request — 3 China Pots' reels start too late for the motion check, but its
    # idle-multiplexed spin POST is caught by the allow_idle fallback).
    acted = bool(rep.get("reels_moved")) or bool(rep.get("spin_fired"))
    bet_placed = status == "ok" and (net_ok or (acted and bal_moved))
    if status == "insufficient_funds":
        errors.append("insufficient funds — bet not placed")
    elif status == "stake_cap":
        errors.append(f"stake {v.get('wager'):g} exceeds the safety cap "
                      f"{config_env.MAX_STAKE:g} — spin refused")
    elif status == "no_spin":
        errors.append("spin click produced no network request or reel motion")
    elif status == "unverified":
        errors.append("balance unchanged and no verifiable result on the wire — spin likely did not happen")
    elif not bet_placed:
        errors.append("spin not corroborated (no parseable network result, no balance movement)")

    tlogs = net_ok
    if bet_placed and not tlogs:
        # NOT a failure: many providers are opaque to live capture (WebSocket-only frames,
        # encrypted/unparseable bodies). The spin was already confirmed visually (reel motion
        # + balance movement), which is the correct ceiling when there's no monitorable
        # backend. The wire truth still gets checked later against the site's transaction
        # history (phase-2). So this is informational, not a flag.
        errors.append("no monitorable wire data — bet verified visually; "
                      "Tlogs deferred to transaction-history validation")

    # Effective wager: the balance movement is the wager of record when the response
    # field disagrees (Hot Hot: response said 1.0, balance moved by 1.50).
    wager = v.get("wager")
    payout = v.get("payout")
    if bet_placed and bb is not None and ba is not None and payout is not None:
        delta_wager = round(bb - ba + payout, 2)
        if wager is None:
            wager = delta_wager
        elif delta_wager > 0 and abs(delta_wager - wager) > 0.011:
            # The delta corrects small response-field lies (Hot Hot: said 1.0, moved 1.50),
            # but a delta MANY times the response wager is a broken balance read (Bison
            # Prime 2026-07-16: phantom R372 from a 0.00 misread) — flag, don't adopt.
            if delta_wager <= wager * 3 + 1:
                errors.append(f"response wager {wager:g} contradicts balance movement "
                              f"{delta_wager:g}; reporting {delta_wager:g}")
                wager = delta_wager
            else:
                errors.append(f"balance movement {delta_wager:g} implausible vs response "
                              f"wager {wager:g} — balance read distrusted, wager kept")

    # Minimum-bet check. The catalog min is only a hint (often per-line or stale — 10 Crown
    # Hot's real minimum chip is 0.40 vs a catalog 0.10), so a CONFIRMED UI floor wins: when
    # flooring bottomed the stepper or picked the smallest menu chip, the achieved stake IS
    # the minimum and is never faulted. Only flag when we did NOT reach a floor AND the stake
    # sits above the catalog hint — i.e. flooring genuinely fell short.
    expected = T.parse_amount(expected_min_text) if expected_min_text else None
    if bet_placed and not at_min_floor and expected is not None and wager is not None \
            and wager > expected + 0.001:
        errors.append(f"stake {wager:g} may be above the game minimum "
                      f"(flooring did not reach a floor; catalog hint {expected:g})")

    # Defense in depth: if a wager above the cap EXECUTED despite the pre-spin gates, that is
    # a safety breach and must fail the report row loudly, not pass with a footnote.
    if bet_placed and wager is not None and wager > config_env.MAX_STAKE:
        errors.append(f"SAFETY BREACH: spun at {wager:g}, above the "
                      f"{config_env.MAX_STAKE:g} cap")
        bet_placed = False

    return {"bet_placed": bet_placed, "tlogs": tlogs, "wager": v.get("wager"),
            "wager_effective": wager, "payout": payout, "source": v.get("source"),
            "errors": errors}


def _point_in_box(pt, box, margin=6):
    """Is a CSS click point inside a detected control's box_2d (normalized 0-1000)?"""
    r = config_env.norm_rect(box)
    return bool(r) and (r[0] - margin) <= pt[0] <= (r[2] + margin) \
        and (r[1] - margin) <= pt[1] <= (r[3] + margin)


def _hazard_boxes(controls, spin, keywords):
    """Discrete auto/turbo/max controls whose box a spin click must not land in. A box
    MUCH larger than a real button is a detection artifact — a sloppy CLUSTER box around a
    whole control rail (Bison Prime 2026-07-16: 'Turbo / Fast Spin' engulfed the right rail)
    or an over-grabbed autoplay box (10 Crown Hot 2026-07-16: 'Autoplay' drawn at 3.4% of the
    screen, 5x the real spin button, vetoing a spinnable game). A veto only makes sense for a
    genuine, button-sized control, so a box is ignored when it is oversized by EITHER measure:
    >3x the spin's own box, OR >2.5% of the viewport in absolute terms. A real (small)
    autoplay button still vetoes, so runaway protection is intact; the post-result halt is the
    backstop regardless."""
    vp = config_env.VIEWPORT_WIDTH * config_env.VIEWPORT_HEIGHT
    abs_cap = 0.025 * vp
    sr = config_env.norm_rect((spin or {}).get("box_2d") or [])
    sa = (sr[2] - sr[0]) * (sr[3] - sr[1]) if sr else None
    cap = min(3 * sa, abs_cap) if sa else abs_cap
    out = []
    for c in controls:
        if c is spin or not c.get("box_2d"):
            continue
        if not any(k in (c.get("label") or "").lower() for k in keywords):
            continue
        r = config_env.norm_rect(c["box_2d"])
        if r and (r[2] - r[0]) * (r[3] - r[1]) <= cap:
            out.append(c)
    return out


async def _halt_runaway(page, controls, spin_center, ss_dir, errors, monitor=None):
    """Post-result guard: reels STILL cycling after the verdict means an autoplay was
    engaged (2026-07-13 3 China Pots: a drifted spin box landed the click on the autoplay
    toggle and 8 rounds wagered before the browser closed). Click the autoplay control
    (an ACTIVE autoplay button acts as STOP), then the main button (it shows STOP while
    auto-spinning) as fallback, and verify the motion dies."""
    a, b = os.path.join(ss_dir, "dsc_post_a.png"), os.path.join(ss_dir, "dsc_post_b.png")
    await page.screenshot(path=a)
    await asyncio.sleep(1.2)
    await page.screenshot(path=b)
    if slot_spin.frame_motion(a, b) <= 3.0:
        return
    # Motion alone is NOT autoplay: games pulse ambient/win animations on a settled screen
    # for minutes. The ONLY trustworthy autoplay signature is MONEY DRAINING on the wire:
    # repeated wager-bearing responses on the learned spin path whose balances DIFFER.
    # Anything weaker (a follow-up POST, a heartbeat echoing the last wager with an
    # unchanged balance) is normal post-result traffic and must NOT draw a stop-click —
    # clicking an IDLE autoplay button is how autospins get STARTED (Gold Blitz
    # 2026-07-16: a post-result POST tripped the old any-request check).
    if monitor is None or not monitor.spin_path:
        print("  [DSC] Motion after the result but no spin path to verify against — leaving it")
        return
    idx_resp = len(monitor.responses)
    bals = []
    draining = False
    deadline = time.time() + 12.0        # autoplay rounds land every ~2-6s
    while time.time() < deadline and not draining:
        await asyncio.sleep(1.5)
        for r in monitor.responses[idx_resp:]:
            idx_resp += 1
            if r["path"] != monitor.spin_path:
                continue
            net = slot_spin.parse_result_body(r["body"]) or {}
            if net.get("wager") is not None and net.get("balance") is not None:
                bals.append(net["balance"])
        draining = len(bals) >= 2 and any(abs(bals[i + 1] - bals[i]) > 0.001
                                          for i in range(len(bals) - 1))
    if not draining:
        print("  [DSC] Motion after the result but no repeated wagers draining the balance "
              "— ambient animation/feature, not autoplay")
        return
    print("  [DSC] ⚠️ Reels still cycling after the result — stopping suspected autoplay")
    targets = [c["center"] for c in controls
               if c.get("center") and "auto" in (c.get("label") or "").lower()]
    if spin_center:
        targets.append(spin_center)
    for pt in targets[:3]:
        await page.mouse.click(*pt)
        await asyncio.sleep(1.6)
        await page.screenshot(path=a)
        await asyncio.sleep(1.2)
        await page.screenshot(path=b)
        if slot_spin.frame_motion(a, b) <= 3.0:
            await page.keyboard.press("Escape")   # clear a menu if the click opened one
            errors.append("autoplay was engaged by the spin click — stopped after the result")
            print("  [DSC] Autoplay stopped")
            return
    errors.append("reels still cycling after the result — autoplay may still be running")
    print("  [DSC] ⚠️ Could not confirm the reels stopped")


async def _spin_once(page, spin_center, monitor, ss_dir, tag, region):
    rep = await slot_spin.spin_and_measure(page, spin_center, monitor, ss_dir,
                                           tag=tag, region=region)
    print(f"  [DSC] spin[{tag}]: endpoint={monitor.spin_endpoint} "
          f"fired={rep.get('spin_fired')} moved={rep.get('reels_moved')} "
          f"status={rep.get('values', {}).get('status')} source={rep.get('values', {}).get('source')}")
    return rep


# Words that CLOSE a dialog without spending. "buy/purchase/confirm/yes/start/bet" are excluded
# — clicking those SPENDS money or commits a feature buy (Island Desire's Buy Feature panel).
_CLOSER_KEYWORDS = ("cancel", "close", "dismiss", "no thanks", "not now", "back", "later",
                    "got it", "ok button", "×", "x button")
_CLOSER_EXCLUDE = ("buy", "purchase", "confirm", "yes", "start", "bet", "spin", "play")


async def _clear_blocking_dialog(page, ss_dir, tag):
    """A mis-aimed spin click can open a modal (Buy Feature / confirm / promo) that Escape
    does not close and that then swallows every further click (Island Desire 2026-07-16:
    the spin was mis-placed on the buy-feature cart, the panel opened, and both spin attempts
    hit the panel). Escape first, then detect and click a Cancel/Close/No control — NEVER a
    Buy/Confirm (that would spend). Best-effort; returns nothing."""
    await T._dismiss_overlays(page)
    try:
        shot = os.path.join(ss_dir, f"dsc_modal_{tag}.png")
        await page.screenshot(path=shot)
        ctrls = T.detect_controls_merged(Image.open(shot), passes=1)
    except Exception:
        return
    closer = next((c for c in ctrls if c.get("center")
                   and any(k in (c.get("label") or "").lower() for k in _CLOSER_KEYWORDS)
                   and not any(b in (c.get("label") or "").lower() for b in _CLOSER_EXCLUDE)),
                  None)
    if closer:
        print(f"  [DSC] Closing blocking dialog via '{closer.get('label')}'")
        try:
            await page.mouse.click(*closer["center"])
            await asyncio.sleep(1.0)
        except Exception:
            pass


async def _place_spin(page, monitor, ss_dir, region, spin, errors, attempts=4):
    """Place ONE verified spin, ROBUSTLY — DSC's core job. Reaching the min bet and then
    failing to spin is the worst outcome, so we do NOT give up after a single try: as long as
    NO money has moved (nothing spent → no double-spend risk), keep re-locating the spin and
    clicking. Each retry clears any dialog a mis-hit opened, re-detects fresh, and prefers the
    targeted re-ask (the label pick already proved wrong). Stop the instant a spin is confirmed
    (status 'ok' or the balance moved) — that guarantees at most one real wager. Runaway
    autoplay from any mis-hit is handled by the post-result halt. Returns (rep, spin_used)."""
    rep = None
    for i in range(attempts):
        tag = "dsc" if i == 0 else f"dsc{i + 1}"
        if await T.refresh_viewport(page, f"spin {i + 1}"):
            errors.append("window resized mid-check — coordinates may have drifted")
        print(f"  [DSC] Spin attempt {i + 1}/{attempts}: '{spin.get('label')}' at {spin['center']}")
        rep = await _spin_once(page, spin["center"], monitor, ss_dir, tag, region)
        rep["_tag"] = tag
        v = rep.get("values", {})
        bb, ba = v.get("balance_before"), v.get("balance_after")
        bal_moved = bb is not None and ba is not None and abs(ba - bb) > 0.001
        # STOP the moment a spin is real: status 'ok' (network- or balance-corroborated) or a
        # visible balance drop. Retrying past this would double-spend. Only the genuine
        # "nothing happened" states (no_spin/unverified with a flat balance) are retried.
        if bal_moved or v.get("status") not in ("no_spin", "unverified"):
            if i:
                print(f"  [DSC] Spin confirmed on attempt {i + 1} (status={v.get('status')})")
            return rep, spin
        if i == attempts - 1:
            break
        print(f"  [DSC] Attempt {i + 1} produced no spin — clearing dialogs and re-locating")
        await _clear_blocking_dialog(page, ss_dir, tag)
        try:
            shot = os.path.join(ss_dir, f"dsc_relocate_{i + 1}.png")
            await page.screenshot(path=shot)
            ctrls = T.detect_controls_merged(Image.open(shot), passes=1)
            # Targeted re-ask locates the spin most reliably; fall back to a pick AWAY from the
            # dead spot, then any pick. Keep the current target only if nothing better appears.
            cand = _find_spin_by_reask(shot, ctrls) or _retry_target(ctrls, spin["center"]) \
                or _pick_spin(ctrls) or _pick_spin_relaxed(ctrls)
            if cand and cand.get("center"):
                spin = cand
        except Exception as e:
            print(f"  [DSC] Re-location failed ({e}) — re-clicking the same target")
    return rep, spin


async def run_dsc(page, monitor, ss_dir, region="ZA", startup_ok=True, expected_min="",
                  non_slot=None):
    """Run the fast check on an already-launched game. Returns an outcome dict consumed by
    to_test_results() (UI report) and to_report_row() (the Excel sheet).
    `non_slot`: reason string when the game is classified as not-a-slot (live/table/crash)
    — Launch is still verified, but the bet flow is skipped: roulette/live games have
    their own spin-like buttons and clicking them without a table bet proves nothing."""
    out = {"launch": False, "bet_placed": False, "tlogs": False, "attempted": False,
           "startup_ok": startup_ok, "controls": 0, "errors": [], "tag": "dsc",
           "wager": None, "wager_effective": None, "payout": None, "source": None,
           "endpoint": None, "spin_at": None, "balance_before": None, "balance_after": None,
           # Wire reconciliation keys for the transaction-history check (stable schema —
           # present even when the spin never happened, so downstream consumers can rely
           # on the fields existing).
           "round_id": None, "tnum": None, "server_time": None,
           "balance_at_start": None, "balance_after_bet": None, "balance_at_end": None,
           "not_attempted_reason": "no spin control"}

    print(f"\n{'='*70}\n  DSC: launch → min bet → spin → verify\n{'='*70}")
    t_start = time.time()

    # A dialog at launch (LOW BALANCE, campaign popup) covers the controls and poisons the
    # detection pass — clear anything dismissible first (Escape; harmless when nothing is open).
    await T._dismiss_overlays(page)

    # Sync the coordinate scale to the CURRENT window before detecting — a resized/
    # snapped worker window otherwise clicks with the stale setup-time scale.
    await T.refresh_viewport(page, "dsc detection")
    shot = os.path.join(ss_dir, "dsc_controls.png")
    await page.screenshot(path=shot)
    controls = T.detect_controls_merged(Image.open(shot), passes=2)
    out["controls"] = len(controls)
    print(f"  [DSC] {len(controls)} controls detected: {[c.get('label') for c in controls]}")

    if non_slot:
        # Launch check still applies (the iframe loaded a playable screen or it didn't);
        # any detected control is proof of a rendered UI for a non-slot layout.
        out["launch"] = startup_ok and out["controls"] >= 1
        out["not_attempted_reason"] = "non-slot game"
        out["errors"].append(f"non-slot game ({non_slot}) — bet flow skipped, verify manually")
        print(f"  [DSC] Non-slot game ({non_slot}) — launch verified only, no bet attempt")
        return out

    spin = _pick_spin(controls)

    # No spin but a Close/OK control? A dialog Escape couldn't clear (LOW BALANCE has only a
    # CLOSE button) is still covering the game — click it and re-detect once.
    if not spin:
        closer = next((c for c in controls if c.get("center") and any(
            k in (c.get("label") or "").lower()
            for k in ("close", "dismiss", "got it", "continue", "ok button"))), None)
        if closer:
            print(f"  [DSC] No spin control but '{closer.get('label')}' detected — "
                  f"dismissing the dialog and re-detecting")
            await page.mouse.click(*closer["center"])
            await asyncio.sleep(1.2)
            await page.screenshot(path=shot)
            controls = T.detect_controls_merged(Image.open(shot), passes=1)
            out["controls"] = len(controls)
            spin = _pick_spin(controls)

    # Still nothing usable in the labels? One targeted re-ask before writing the game off —
    # providers that bake hint text into the button (3 Oaks "Hold for Turbo Spin") otherwise
    # get skipped as "possibly not a slot game".
    if not spin:
        spin = _find_spin_by_reask(shot, controls)
        if spin:
            print(f"  [DSC] Spin button recovered by targeted re-ask at {spin['center']}")

    # Final label fallback: the main button labeled 'Turbo / Fast Spin' (10 Crown Hot,
    # 3 Oaks family) IS the spin when no plain 'Spin' label exists. _pick_spin_relaxed
    # allows turbo/fast labels (never autoplay/promo), so it recovers these before the
    # game is written off as "no spin control".
    if not spin:
        spin = _pick_spin_relaxed(controls)
        if spin:
            print(f"  [DSC] Spin button recovered via relaxed pick "
                  f"('{spin.get('label')}') at {spin['center']}")

    # Autoplay-overlap CORRECTION (never a skip): DSC's job is to place the bet, so we never
    # refuse to spin. If the spin target happens to sit inside a real (small) autoplay box,
    # try to correct it to a cleaner target via the re-ask — but if that fails, we STILL spin.
    # Runaway autoplay from any mis-hit is contained downstream: the stake is floored to the
    # minimum first, the MAX_STAKE gate blocks any high-stake click, and the post-result halt
    # (network-verified) stops autospins within ~1-2 min-stake rounds. Reaching the min bet
    # and then skipping the spin is a worse outcome than a couple of stopped min-stake rounds.
    if spin:
        hazards = _hazard_boxes(controls, spin, ("auto",))
        bad = next((c for c in hazards if _point_in_box(spin["center"], c["box_2d"])), None)
        if bad:
            print(f"  [DSC] Spin target {spin['center']} overlaps '{bad.get('label')}' — "
                  f"re-asking for a cleaner spin target")
            fresh = _find_spin_by_reask(shot, controls)
            if fresh and not any(_point_in_box(fresh["center"], c["box_2d"]) for c in hazards):
                spin = fresh
                print(f"  [DSC] Spin target corrected to {spin['center']}")
            else:
                out["errors"].append(f"spin target near '{bad.get('label')}'; spinning anyway "
                                     f"with the post-result autoplay halt as the safety net")
                print(f"  [DSC] No cleaner target — proceeding; post-result halt will catch "
                      f"any autoplay")

    # Launch = playable screen. A visible spin button is proof; a full control bar (>=3
    # controls) still counts if one flaky vision pass missed the spin label itself.
    out["launch"] = bool(spin) or (startup_ok and out["controls"] >= 3)
    if not startup_ok and not spin:
        out["errors"].append("game did not reach a playable screen (startup loop maxed out)")
    if not spin:
        out["errors"].append("spin control not detected"
                             + (" — possibly not a slot game; verify manually"
                                if out["controls"] >= 3 else ""))
        return out

    # ── Bet flooring, CLOSED-LOOP. Read the stake first: a game that launches already at the
    # catalog minimum needs no flooring at all — and at min stake the "-" renders disabled/dim,
    # which is exactly the state where vision mislabels the "+" as "Bet Decrement".
    expected = T.parse_amount(expected_min) if expected_min else None
    stake = await _read_stake(page, ss_dir, "before")
    print(f"  [DSC] Stake before flooring: {stake if stake is not None else 'unreadable'}"
          + (f" (catalog min {expected:g})" if expected is not None else ""))

    # at_min_floor: did we land on the game's TRUE minimum? True when the stepper bottomed out,
    # the smallest bet-menu chip was chosen, or the stake was already at/below the catalog
    # target. The catalog min is often per-line or stale (10 Crown Hot 2026-07-17: catalog
    # 0.10 but the real minimum chip is 0.40), so a confirmed UI floor is trusted over it.
    at_min_floor = False
    if stake is not None and expected is not None and stake <= expected + STAKE_EPS:
        print(f"  [DSC] Already at the catalog minimum — skipping flooring")
        at_min_floor = True
    else:
        decs = _decrement_controls(controls)
        if not decs:
            stake, handled, at_min_floor = await _floor_via_bet_menu(
                page, controls, stake, ss_dir, out["errors"], expected=expected, base_shot=shot)
            if not handled:
                out["errors"].append("no bet-decrease control detected; spun at current stake")
        for i, dec in enumerate(decs, 1):
            if stake is not None and expected is not None and stake <= expected + STAKE_EPS:
                at_min_floor = True
                break
            stake, trusted, at_min_floor = await _probe_and_floor(
                page, dec, stake, ss_dir, out["errors"], f"d{i}", expected=expected)
            if trusted:
                continue
            # Recovery, once: off the floor BOTH steppers render enabled, so a fresh detection
            # sees the real "-". Trust only candidates strictly LEFT of the impostor "+".
            bad_x = dec["center"][0]
            shot = os.path.join(ss_dir, "dsc_refind.png")
            await page.screenshot(path=shot)
            fresh = _decrement_controls(T.detect_controls_merged(Image.open(shot), passes=1))
            cands = [c for c in fresh if c["center"][0] < bad_x - 20]
            if cands:
                stake, _, at_min_floor = await _probe_and_floor(
                    page, cands[0], stake, ss_dir, out["errors"], f"r{i}", expected=expected)
            else:
                out["errors"].append("could not re-find a trustworthy bet-decrease control")
            break  # after one mislabel, the remaining original candidates are not trustworthy

    # ── HARD SAFETY GATE: never click spin above the cap, period (team rule 2026-07-09).
    # spin_and_measure re-checks this on its own pre-read; this earlier gate reports it as
    # a clean "not attempted" instead of a failed spin.
    if stake is not None and stake > config_env.MAX_STAKE:
        out["errors"].append(f"stake {stake:g} exceeds the safety cap "
                             f"{config_env.MAX_STAKE:g} — spin aborted")
        out["not_attempted_reason"] = "stake above safety cap"
        print(f"  [DSC] 🛑 Stake {stake:g} > cap {config_env.MAX_STAKE:g} — NOT spinning")
        return out

    # A promo/campaign popup can swallow the spin click — clear anything open first.
    await T._dismiss_overlays(page)

    out["attempted"] = True
    out["spin_at"] = datetime.now().astimezone().isoformat()
    # Robust spin: keep re-locating and clicking until a spin is confirmed or we run out of
    # bounded attempts — but stop the instant money moves (never double-spend). This is DSC's
    # core promise: once the min bet is set, actually place it.
    rep, spin = await _place_spin(page, monitor, ss_dir, region, spin, out["errors"])
    out["tag"] = rep.get("_tag", out["tag"])

    out["endpoint"] = monitor.spin_endpoint
    v = rep.get("values", {})
    out["balance_before"] = v.get("balance_before")
    out["balance_after"] = v.get("balance_after")
    for k in ("round_id", "tnum", "server_time",
              "balance_at_start", "balance_after_bet", "balance_at_end"):
        out[k] = v.get(k)
    verdict = _evaluate_spin(rep, expected_min, at_min_floor=at_min_floor)
    out["errors"].extend(verdict.pop("errors"))
    out.update(verdict)

    # If the reels are still cycling, an autoplay is running — stop it before returning
    # (every extra round is an unplanned wager).
    try:
        await _halt_runaway(page, controls, spin["center"], ss_dir, out["errors"],
                            monitor=monitor)
    except Exception as e:
        print(f"  [DSC] runaway check failed: {e}")

    print(f"  [DSC] launch={out['launch']} bet_placed={out['bet_placed']} tlogs={out['tlogs']} "
          f"wager={out['wager_effective']} payout={out['payout']} source={out['source']} "
          f"({round(time.time() - t_start, 1)}s)")
    return out


def to_test_results(out):
    """Three rows for the standard UI report, mirroring the Excel columns."""
    tag = out.get("tag", "dsc")
    r1 = T.TestResult("DSC: Launch", "dsc_controls.png")
    r1.passed = out["launch"]
    r1.details = f"{out['controls']} controls detected" \
                 + ("" if out["startup_ok"] else " (startup loop maxed out)")

    r2 = T.TestResult("DSC: Bet placed (minimum stake)", f"{tag}_spinning.png")
    r3 = T.TestResult("DSC: Tlogs (verified transaction on the wire)", f"{tag}_result.png")
    if out["attempted"]:
        r2.passed = out["bet_placed"]
        r2.details = (f"wager={out['wager_effective']}, payout={out['payout']}, "
                      f"source={out['source']}")
        # Live Tlogs is a BONUS, never a hard fail: PASS when the wire gave a parseable
        # transaction, otherwise NEUTRAL (passed=None). An opaque backend (WS-only/
        # unparseable) verified visually is not a failure — the real Tlogs verdict comes
        # from the phase-2 transaction-history validation regardless.
        if out["tlogs"]:
            r3.passed = True
            r3.details = f"money result parsed from the spin response ({out['endpoint']})"
        elif out["bet_placed"]:
            r3.passed = None
            r3.details = ("no monitorable wire data — bet verified visually; "
                          "deferred to transaction-history validation")
        else:
            r3.passed = None
            r3.details = "no verified bet to check on the wire"
    else:
        r2.details = r3.details = f"not attempted ({out.get('not_attempted_reason', 'no spin control')})"
    for e in out["errors"]:
        r1.actions.append(f"note: {e}")
    return [r1, r2, r3]


def to_report_row(out, meta):
    """One Excel row in the team's DSC format."""
    def pf(x):
        return "Pass" if x else "Fail"
    return {
        "Sr. No.": meta.get("srNo", ""),
        "Provider": meta.get("provider") or "Unknown",
        "Game Name": meta.get("gameName", ""),
        "Launch": pf(out["launch"]),
        "Bet Placed": pf(out["bet_placed"]) if out["attempted"] else "NA",
        # Tlogs is the TRANSACTION-HISTORY verdict, which only exists after the phase-2
        # validation run — the sweep leaves it Pending (network truth stays in the
        # records JSONL and the UI details for the validator to cross-check).
        "Tlogs": "Pending" if out["attempted"] else "NA",
        "Error": "; ".join(out["errors"]) or "NA",
        "Evidence": meta.get("evidence", ""),
    }
