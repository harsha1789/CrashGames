Repo: Canvas-Game-Automation-dsc-auto-sweep (Flask dashboard app.py + crash_auto.py +
test_spin_button.py / slot_dsc.py / modules/*). Implement three changes. Read the relevant
files fully before editing — don't guess at surrounding logic.

## 1. Auto min-bet per game (stop relying on UI-entered/sheet-provided values)

Current state:
- Crash: app.py's launch_crash_sweep() (~line 1157) takes ONE operator-typed stake
  (`data['bet']`) and applies it to EVERY selected game via
  `cmd += ["--live", "--bet", str(live_bet)]` (~line 1225). crash_auto.py's place_bet()
  (~line 489) just wagers whatever `stake` it's given — there's no in-game floor detection.
  BUT modules/provider_sweep.py's list_crash_games() (~line 59) already returns
  `minBetAmount` per game from the casino's own catalog — it's fetched and just never used.
- Slot: slot_dsc.py already has _floor_via_bet_menu()/_min_bet_in_menu() (~line 169-282)
  which drives the in-game bet menu down to its actual floor automatically — this is the
  right pattern. But confirm it doesn't still take a UI/sheet-supplied `min_bet` as its
  starting/expected value anywhere in that call chain (test_spin_button.py `min_bet` arg,
  templates/index.html line 655's `mbet` sourced from an imported sheet column). If it does,
  remove that dependency so the floor is always discovered in-game, not seeded from the sheet.

Desired behavior:
- Crash: each game in a crash sweep/single launch uses ITS OWN minimum bet automatically —
  either (a) the `minBetAmount` already returned by list_crash_games/crash-sweep-plan, passed
  per-worker instead of one shared `live_bet`, or (b) build a floor-detection routine for the
  crash bet input analogous to slot_dsc's, if the bet input allows arbitrary typed values and
  there's no reliable API-provided minimum for a given launch context. Prefer (a) since the
  data already exists — only fall back to (b) if minBetAmount proves unreliable/missing.
- Slot: keep the existing in-game floor-detection as the source of truth; remove any leftover
  dependency on a manually typed or sheet-imported min-bet value for actually placing the bet.
- Both: the operator should no longer need to type a stake into the dashboard UI to run a
  live/DSC pass — the automation resolves it per game. Keep config_env.MAX_STAKE as a hard
  safety cap regardless of where the stake number comes from (crash_auto.py ~line 501,
  place_bet already checks this — preserve it).
- Update templates/index.html to stop requiring/showing a manual stake field for these flows
  (or keep it only as an optional override/cap, clearly labeled as such).

## 2. Job scheduling for N games (slot + crash) — add BOTH manual and time-based

Current state: /launch-batch (slot, Excel-driven) and /launch-crash-sweep (crash, picks[])
already run a batch of N games immediately when called — that manual trigger must keep working
exactly as-is.

Add: a scheduler so the same batch definitions (slot batch upload + accounts; crash sweep
picks + account) can instead be scheduled to run at a future time, or on a recurring cadence
(e.g. daily at HH:MM), without a human clicking launch at that moment.
- New persistent store (e.g. a JSON file like accounts.json's pattern, or a small SQLite table
  alongside dsc_history.db) holding scheduled jobs: {id, type: "slot"|"crash", payload (same
  shape /launch-batch or /launch-crash-sweep expects), run_at or cron-like recurrence, enabled,
  last_run_at, last_run_id}.
- A background thread (same pattern as app.py's existing `scheduler()` closure in _start_fleet,
  ~line 229 — reuse the daemon-thread-with-poll-loop idiom already established there, don't
  introduce a new dependency like APScheduler unless it's already in requirements.txt) that
  wakes periodically, finds due jobs, and calls the SAME internal logic /launch-batch and
  /launch-crash-sweep use (refactor their bodies into callable functions if they're currently
  only reachable as Flask view functions) to actually start the run.
- New endpoints: create/list/update/delete scheduled jobs, and toggle enabled/disabled. Surface
  them in templates/index.html — a simple "Schedule" panel next to the existing manual
  batch/sweep launch controls, showing upcoming/recurring jobs and letting the user cancel one.
- Respect all existing safety gates (one crash worker at a time / same-account cooldown logic
  in _start_fleet, MAX_STAKE cap, "select at least one account" validation) — a scheduled run
  must go through the exact same validation a manual run does, just triggered by time instead
  of a click.
- Persist schedules across an app restart (reload from the JSON/DB file on startup).

## 3. Evidence screenshots only for FAILED games, not PASSED ones

Current state: both test_spin_button.py and crash_auto.py call `page.screenshot(...)`
unconditionally at every test step and attach the path to each TestResult.screenshot
regardless of outcome (see test_spin_button.py TestResult class ~line 361, and the dozens of
`await page.screenshot(...)` call sites through both files). results.json and the
dashboard's report UI currently show/keep every screenshot for every step, pass or fail.

Desired behavior: after each test/game's pass/fail verdict is known, only KEEP (persist to the
run folder / reference in results.json / show in the dashboard report) the screenshots
belonging to steps where `passed is False` (or the overall game result is a failure). Passed
steps' screenshots should not be written to disk (or should be deleted immediately after the
verdict is determined, whichever is simpler given the code already calls page.screenshot
before the verdict is computed in most cases).
- Implement this at the point each TestResult's `passed` field is finalized (or at the end of
  run_tests()/run_crash_tests() when assembling the final `results` list / results.json), not
  by trying to predict pass/fail before taking the shot.
- Do this for BOTH slot (test_spin_button.py, and its DSC path via slot_dsc.py) and crash
  (crash_auto.py) result assembly.
- Update wherever results.json / the Excel-Crash-Report / dashboard report renderer
  (app.py's CRASH_REPORT_COLUMNS section ~line 528, and the report template JS in
  templates/index.html) reads `TestResult.screenshot` / `shots{}` so it doesn't dangle on a
  deleted/never-written path for a passed step — it should simply show no screenshot for a pass.
- Don't touch unrelated captures (e.g. recordings/, the network-dump JSON, or the
  detect-controls screenshot used for control detection itself) — this is specifically about
  the per-test-step evidence screenshots tied to a pass/fail verdict.

Work through these in order (1, 2, 3), and after each one, run the existing manual flows
(single launch, slot batch, crash sweep) to confirm nothing that currently works regresses.
