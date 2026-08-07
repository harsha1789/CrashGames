# Slot Game Gemini Prompts

Baseline snapshot (2026-07-29) of the Gemini prompts in `test_spin_button.py` that feed the
report table (`Provider | Game Name | Launch | Bet Placed | Tlogs | Error | Evidence`). All run
against `gemini-2.5-flash` with `thinking_config=types.ThinkingConfig(thinking_budget=0)`.

This file is the **backup/reference copy** — before any future enhancement to a slot prompt,
copy its current text here (or into a dated `slot_prompt_vN.md`) first, so the prior version is
always recoverable.

---

## 1. Control detection — `detect_all_controls()`

Feeds: general control-interaction for every downstream check (Spin, Bet +/-, Autoplay, Turbo,
Menu, etc.) — the basis for "Launch"/"Bet Placed" actually being able to act on the game.

```
Analyze this slot/casino game screenshot. Detect EVERY interactive UI control
(buttons, toggles, icons, +/- steppers, value displays). IGNORE the reels, symbols, and background.

Label each control by what it ACTUALLY is — READ the icon/text, do not force it into a category.
Reference vocabulary for how to recognize common controls:
- Spin Button (the large central play button)
- Bet Increment ("+") and Bet Decrement ("-") next to the bet value
- Bet Display (the stake value), Balance Display, Win Display
- Autoplay / Autospin — CIRCULAR LOOPING ARROWS (looping arrows, or two curved arrows forming a loop) or the
  word "AUTO"; starts repeated automatic spins. NOT a speedometer/gauge.
- Turbo / Fast Spin — a SPEED icon: a speedometer/gauge dial, a lightning bolt, a fast-forward (>>),
  or a stopwatch. NOT looping arrows.
- (The two round buttons beside the Spin button are usually ONE Turbo and ONE Autoplay — tell them
  apart by the ICON above; do NOT assume which side is which.)
- Sound Toggle (speaker), Settings (gear), Menu (hamburger)
- Home / Lobby (a HOUSE icon), Game History (a CLOCK or circular-history icon),
  Help (a QUESTION MARK "?"), Info / Paytable (an "i" or "pays")
- Buy Bonus / Feature Buy (if present)

CRITICAL:
- A clock/history icon is "Game History", a "?" is "Help", a house is "Home" — do NOT mislabel
  these as "Autoplay" or "Info / Paytable". Only label a control Autoplay/Info if it truly is.
- Distinguish the two round buttons by Spin: looping-arrows/"AUTO" = Autoplay; speedometer/gauge/
  lightning = Turbo. Do not swap them or assume a side.
- Each PHYSICAL control appears EXACTLY ONCE. Never output one control under two names, and never
  repeat a control (e.g. don't add a second "Autoplay" in a corner if the autoplay button is by Spin).
- box_2d must be a TIGHT box around JUST that control, normalized 0-1000.

Return a JSON array of objects: {"label": "...", "box_2d": [ymin, xmin, ymax, xmax]}.
```

## 2. Balance/bet reading — `read_game_values()`

Feeds: "Bet Placed" (wager verified against what the game shows) and balance-delta checks.

```
Read the BALANCE and BET amounts shown in this slot game screenshot.
Return JSON: {"balance": "exact text shown", "bet": "exact text shown"}
If you can't find either value, use null.
```

## 3. Startup/launch detection — `auto_handle_startup` (inline prompt)

Feeds: "Launch" — this is THE gate that decides whether the game genuinely loaded, distinct
from a title screen, a recoverable error, or a hard regional block.

```
Analyze this slot game screenshot during startup. Games often chain
SEVERAL popups before play (loading splash -> intro/feature screen -> rules -> age/sound prompt).

Return JSON: {"state": "wait"|"click"|"ready"|"blocked"|"reload", "popup": "short description or 'none'",
"box_2d": [ymin, xmin, ymax, xmax] or null}

- "wait"  = still loading (progress bar, blank screen, spinning loader).
- "click" = ANY non-gameplay screen that has a control to proceed: a TITLE / ATTRACT screen
  (game logo + a big PLAY / START button, often with "MAX WIN" or jackpot art), an intro /
  feature preview, rules, age / sound / promo overlay, OR a popup with X / back-arrow to close.
  Provide box_2d of the PROCEED control ("Play", "Start", "Continue", "OK", "I Accept", a right-arrow,
  or the X / back-arrow). IMPORTANT: never target a checkbox/toggle like "Don't show again".
- "ready" = ACTUAL GAMEPLAY is visible: a grid/reels of MULTIPLE symbols AND a round spin button
  AND a numeric bet & balance in a bottom bar, with NO title screen or overlay in the centre.
  A game logo or character art with a PLAY/START button is NOT ready — that is "click".
- "blocked" = a RESTRICTION page instead of a game: "location restriction", "not available in
  your region/country", a permanent geo/eligibility block. Neither clicking nor reloading fixes
  these.
- "reload" = a RECOVERABLE error dialog that asks to refresh/retry: "Something went wrong",
  "Please refresh to continue playing", "Connection lost", "An error occurred, try again". A
  page reload fixes these.

Normalize box_2d to 0-1000.
```

## 4. Bet-overlay comparison — `try_overlay()` (inline prompt)

Feeds: "Bet Placed" — used while probing the minimum-bet flow to detect a bet-selection
overlay and pick a different stake value.

```
Compare these two slot game screenshots.
Did a bet selection overlay/popup/panel appear in the second image?
If yes, find a DIFFERENT bet value than the current one.
Return JSON: {"overlay": true/false, "different_bet": {"label": "text", "box_2d": [ymin, xmin, ymax, xmax]} or null}
Normalize box_2d to 0-1000.
```

---

**Not included here (out of scope for the Launch/Bet Placed/Tlogs table):** the menu/paytable/
autoplay-exploration prompts in `slot_explore.py` and `slot_agent.py` — those feed checklist
items 9-16 (Auto Play, Menu, History, Audio, etc.), not the core report table, per the
"only touch what's in the table" scoping rule.
