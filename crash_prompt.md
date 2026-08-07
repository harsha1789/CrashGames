# Crash Game Gemini Prompts

All three run against `gemini-2.5-flash` via `T.gemini_call()`, with
`thinking_config=types.ThinkingConfig(thinking_budget=0)` (see `crash_auto.py::_cfg()`).
Source: `crash_auto.py`. **v1 kept verbatim in `crash_prompt_v1_backup.md`.**

## v2 changelog (2026-07-30)

A 15-provider validation sweep (`runs/provider_validation_dryrun/`, one representative game per
provider from the live casino catalog) found two real, reproducible cross-provider gaps in v1:

1. **Light-and-Wonder ("Red Light Green Light")** — got stuck behind a "Tap to continue..."
   splash. v1's `read_round_state()` phase list had no name for this state, so it silently read
   as `LOADING` forever; the only mitigation was a blind screen-center click on a fixed schedule
   (attempts 6/12 of `auto_handle_crash_startup`), which doesn't reliably land on an off-center
   gate button. TEST 1 then found exactly one control: `["Tap to continue..."]` — the model
   correctly saw *something* clickable but had no vocabulary to classify or act on it.
2. **Playtech-Live ("Big Bad Wolf Crash & Crumble")** — a live-dealer-style skin whose action
   button sits in a small control strip beside a video feed and never says "BET"/"CASH OUT"
   verbatim. v1's fixed literal-wording vocabulary found the peripheral controls (bet input,
   balance, round history — 10 total) but never the action button itself, so TEST 1 failed
   (`core = bet_btn + cashout_btn + multiplier_disp >= 2` never reached 2).

Fixes, both prompt-level plus one code-level:
- **`read_round_state()`**: added a `GATE` phase (splash/consent/tap-to-play, distinct from
  `LOADING`) with a `gate_target` box so the caller can click the *actual* element instead of
  guessing screen-center.
- **`auto_handle_crash_startup()`** (code, not just prompt): reacts to `GATE` on every poll —
  clicks `gate_target`'s real screen position (or falls back to center if none given) — instead
  of only two blind fixed-schedule clicks. Handles providers that stack several gates in a row
  (e.g. cookie consent -> tap to play).
- **`detect_crash_controls()`**: Bet/Cash Out descriptions changed from fixed literal wording to
  a functional description (any wording/icon that acts as "place a wager" or "lock in a payout"
  normalizes to the same canonical label), plus an explicit `Continue/Start Gate` label so a
  still-visible gate control (if TEST 1 ever runs while one is up) is named correctly instead of
  inventing an ad-hoc label outside the vocabulary — which is exactly what produced
  `"Tap to continue..."` in the v1 failure above.

Verification: see `runs/provider_validation_dryrun/gate_fix_verification.json` (re-run of these
same two providers against v2) for whether this actually closed the gap — check that file rather
than assuming the prompt change alone fixed it.

---

## 1. Control detection — `detect_crash_controls()`

Used only as the vision fallback when the DOM-first path (`detect_crash_controls_dom`,
Spribe-only pre-bet selectors) finds neither a Bet nor a Cash Out control.

```
Analyze this "crash" style casino game screenshot (a multiplier rises from 1.00x and
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
- "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000
```

## 2. Phase / value reading — `read_round_state()`

The workhorse — called on every `CrashObserver.sample()` poll and every startup check.

```
This is a "crash" style casino game (a multiplier rises from 1.00x and can crash).
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
   dismiss the GATE (or null if phase is not GATE)}
```

## 3. Round-history diff — TEST 4 (inline, `run_crash_tests`)

Two-image comparison, no separate helper function. Unchanged from v1 — no provider gap found here.

```
Compare the round-history strip (row of previous multipliers) in these two crash-game
screenshots. Did a new multiplier value get added/changed in the second image?
Return JSON: {"updated": true/false, "reason": "brief"}
```
