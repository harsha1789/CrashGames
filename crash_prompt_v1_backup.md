# Crash Game Gemini Prompts — v1 (backup, superseded 2026-07-30)

Snapshot of the prompts as they stood before the cross-provider generalization fix. Kept per
team convention (see `slot_prompt.md`'s equivalent backup rule) so v2 can be compared against
or rolled back to this if an enhancement regresses something v1 handled fine.

Superseded because a 15-provider validation sweep (`runs/provider_validation_dryrun/`)
surfaced two real, reproducible gaps neither prompt below could handle:
1. **Light-and-Wonder ("Red Light Green Light")** — stuck behind a "Tap to continue..." splash
   gate; `read_round_state()` had no phase for this, so `auto_handle_crash_startup()` never
   recognized it needed a click and just blind-clicked screen-center twice on a fixed schedule.
2. **Playtech-Live ("Big Bad Wolf Crash & Crumble")** — `detect_crash_controls()`'s fixed literal
   vocabulary ("Bet Button (BET / Place Bet)", "Cash Out Button") didn't match this provider's
   actual action button, which sits in a smaller control strip beside a live-dealer video feed
   and doesn't use that exact wording.

See `crash_prompt.md` for the v2 prompts that address both.

---

## 1. Control detection — `detect_crash_controls()`

```
Analyze this "crash" style casino game screenshot (a multiplier rises from 1.00x and
can crash at any time, e.g. Aviator / FlyX Party). Detect ALL interactive UI controls.

RULES:
- ONLY detect clickable buttons/inputs and the key value displays (NOT decorations).
- Each control must have a specific descriptive label.

Detect when visible:
1. Bet Amount Input      2. Bet Increment (+)     3. Bet Decrement (-)
4. Bet Button (BET / Place Bet)                   5. Cancel Bet Button
6. Cash Out Button       7. Auto Bet Toggle       8. Auto Cash Out Toggle
9. Auto Cash Out Input (target multiplier)       10. Multiplier Display (e.g. "1.00x")
11. Round History (strip of previous multipliers) 12. Balance Display
13. Second Bet Panel Bet Button (if a parallel panel exists)

Return a JSON array of objects with:
- "label": a specific name from the list above
- "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000
```

## 2. Phase / value reading — `read_round_state()`

```
This is a "crash" style casino game (a multiplier rises from 1.00x and can crash).
Determine the CURRENT round phase and read the values.

Phases:
- "LOADING"      : still loading (splash / progress bar), UI not interactive yet.
- "BETTING"      : a betting window is open (countdown / "place your bet"); the round has not started rising.
- "ASCENDING"    : a multiplier is currently rising / the plane/rocket is in flight.
- "CRASHED"      : the round just ended ("FLEW AWAY" / "BUSTED"), multiplier frozen/red.
- "RESULT"       : a brief win/settlement shown between rounds.
- "DISCONNECTED" : an error/disconnect overlay is shown ("You have been disconnected", a red X circle,
                   a "HOME" or "refresh" button) — the game is NOT running. Do NOT confuse the red X with CRASHED.

Return JSON:
{"phase": "...",
 "multiplier": current multiplier as a number like 2.53 (or null if none shown),
 "balance": "exact balance text or null",
 "bet": "exact bet/stake text or null"}
```

## 3. Round-history diff — TEST 4 (inline, `run_crash_tests`)

```
Compare the round-history strip (row of previous multipliers) in these two crash-game
screenshots. Did a new multiplier value get added/changed in the second image?
Return JSON: {"updated": true/false, "reason": "brief"}
```
