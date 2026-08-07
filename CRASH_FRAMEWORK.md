# Crash Game Automation — Flow & Architecture

> Companion to `FRAMEWORK_BRIEF.md` (the slot framework). Scope of this brief: `crash_auto.py`,
> its dashboard integration (`app.py`, `templates/index.html`), and the catalog-discovery helper
> (`modules/provider_sweep.py`). Audience: whoever picks this up next.

---

## 0. Plain-English walkthrough (start here if you're new to this)

Skip the diagrams for a second — here's the whole pipeline in one pass, start to finish.

**1. Finding the game and its URL.** We never hardcode game URLs. We call the casino's own
catalog API (the `Categories` endpoint), which returns a `crashgames` bucket — every crash title
with its id and provider. That powers both the typeahead search box and the Crash Sweep
"Discover" button. Once a game is picked, we take its id, log in, and call the casino's "pick
game" API with that id — the response is the *real* iframe launch URL for that session. That's
`resolve_launch_url()` (§8 file map, `crash_auto.py`) — it returns `(launch_url, provider)`.

**2. Opening it.** A real Playwright-driven Chrome navigates to that URL and waits for the
game's iframe to actually render (`auto_handle_crash_startup`, §4). Self-healing reload is built
in here: a transient Spribe config-fetch CORS failure gets retried with a page reload before we
give up for real.

**3. Identifying the controls (bet button, cash-out, autoplay, etc.).** Cheapest-first:
- **DOM-first** (`detect_crash_controls_dom`) — real CSS selectors reverse-engineered from a
  live session. Free, instant, exact, when it matches.
- **Vision fallback** (`detect_controls_vision`) — a screenshot goes to Gemini with "where's the
  bet button / cash-out button?" Covers any provider skin we haven't mapped the DOM for, at the
  cost of a small API call (~$0.0002 each, measured).
`detect_controls()` is the wrapper: try DOM, only pay for vision if DOM comes back empty. Full
detail in §3.

**4. Reading what the game is doing.** `CrashObserver` (§4) repeatedly samples the screen and
classifies the phase: `LOADING → BETTING → ASCENDING → CRASHED → RESULT` (or `DISCONNECTED`).
This is what tells the test *when* it's safe to bet (only during BETTING) and when to expect a
payout.

**5. Placing the actual bet.** During BETTING we type the stake and click Bet (control found in
step 3). During ASCENDING we either wait for the target multiplier or click Cash Out. Balance is
read before and after to compute the real win/loss.

**6. Don't trust the screen alone — check the network.** The casino's backend sends real JSON
for every bet placed. We intercept that response (`slot_spin.parse_result_body()`) and treat it
as ground truth over what we *saw* on screen — a screenshot can lag or mis-render; the server's
own response can't.

**7. The final check — real transaction logs (Tlogs).** For live bets, the wager gets logged as
a pending record (stake, payout, balances) via `dsc_report.append_record()`. Minutes later, once
the casino's back-office transaction history is queryable, `tlogs_validate.py` cross-checks our
record against that real ledger — proof the bet we *think* we placed is the bet the casino
*actually* processed and paid.

**8. Reporting.** All of the above collapses into one Excel row per game:
`Provider | Game Name | Launch | Bet Placed | Tlogs | Remark | Evidence` — Launch = did controls
appear at all (step 3), Bet Placed = did step 5 succeed, Tlogs = did step 7 confirm it against
real casino records.

Throughline: **discover URL via real API → open a real browser → find controls (DOM cheap,
vision as backup) → watch phase → bet → verify via network response → verify again later via
real transaction history.** Nothing about "is this game live" is ever guessed — it's always
confirmed by the DOM, the server's own response, or the casino's ledger.

---

## 1. Why crash needed its own framework, not just a slot config

Slots: one discrete action (spin) → one settlement. Crash: a continuous, time-sensitive round —
**BETTING → ASCENDING → CRASHED → RESULT** — where the only real decision is *when* to cash out.
That difference forces three things slots never needed:

1. A **phase state machine** (`CrashObserver`), not a before/after screenshot diff.
2. **Re-detection on every phase transition** — in Spribe's Aviator, the Bet button and Cash Out
   button are the *same DOM element*, relabeled the instant a round leaves BETTING. A control
   map taken pre-bet does not describe the ASCENDING-phase UI.
3. Timing-sensitive wager tests (bet rejected mid-flight, cancel-before-start, double-click) that
   slots have no equivalent of.

Everything else — Gemini vision, network verification via `slot_spin`, the report/screenshot
plumbing — is reused from the slot stack unchanged.

---

## 2. System architecture

```
                    CLI                              DASHBOARD (app.py)
        python crash_auto.py --game Aviator      POST /launch {game_type:'crash'}
              --username --password                POST /launch-crash-sweep
                       │                                      │
                       ▼                                      ▼
              resolve_launch_url()             api_crash_sweep_plan() ──► provider_sweep
              (auth → search → launch URL)     launch_crash_sweep()        .list_crash_games()
                       │                          (spawns N crash_auto.py        │
                       ▼                           subprocesses, ONE AT A       ONE call to the
              run_crash_tests(url, …)             TIME — see §6)           Categories API's
                       │                                      │             curated "crashgames"
                       ▼                                      ▼                  bucket
              ┌────────────────────────────────────────────────────┐
              │                  run_crash_tests()                 │
              │  owns the Playwright page, the UnifiedGameMonitor,  │
              │  the CrashObserver, and the TEST 1-4 / WAGER 1-7    │
              │  sequence (§5)                                      │
              └──────┬───────────────┬───────────────┬──────────────┘
                     ▼               ▼               ▼
            detect_controls*   CrashObserver    place_bet / cash_out
            (§3)               (§4)             (§5, --live only)
```

**Module dependency graph** (mirrors `FRAMEWORK_BRIEF.md §1`):

| Module | Imports from siblings | Role |
|---|---|---|
| `crash_auto.py` | `config_env`, `test_spin_button as T`, `slot_spin` | Everything below. |
| `config_env` | — | Live `VIEWPORT_WIDTH/HEIGHT` + `norm_box_center()` (0–1000 → clamped CSS px) + `MAX_STAKE`. |
| `test_spin_button` (T) | — | Gemini client (`gemini_call`, `parse_gemini_json`), `TestResult`, `_ss`, `find_control`, `annotate_controls`, `finalize_media`, `_emit_report` — the **same report contract** the slot suite uses. |
| `slot_spin` | — | `UnifiedGameMonitor` (fetch/xhr/WS capture), `frame_motion`, `parse_result_body` (provider-agnostic field-by-meaning scan — works unchanged on a crash bet/cashout response). |
| `modules/provider_sweep.py` | `modules.utils` | `list_crash_games()` — dashboard-only, one-call catalog discovery (§7). |
| `app.py` / `templates/index.html` | `crash_auto` (as a subprocess, not imported) | Dashboard UI: single-game typeahead, Crash Sweep, Slot/Crash mode toggle (§7). |

---

## 3. Control detection: DOM-first, vision-fallback

```
                     detect_controls(page, ss_dir, tag)
                                  │
                                  ▼
                   detect_crash_controls_dom(page)
                (page.locator() against SPRIBE_DOM_SELECTORS —
                 real CSS classes recon'd from a live session,
                 e.g. button.bet.btn-success)
                                  │
                    ┌─────────────┴─────────────┐
                    ▼                             ▼
         found a Bet or Cash Out control      found NEITHER
                    │                             │
                    ▼                             ▼
        use DOM result (fast, free,      detect_crash_controls(image)
        no Gemini call, no box_2d        (Gemini vision call — the
        → CSS scaling risk)              provider-agnostic fallback
                                          for anything DOM didn't
                                          recon or doesn't cover)
```

Two entry points, used deliberately for different phases:

- **`detect_controls()`** — DOM-first + vision-fallback. Safe for **BETTING-phase** snapshots:
  recon confirmed the pre-bet markup (stake stepper, quick-bet chips, the Bet button itself).
- **`detect_controls_vision()`** — vision-only, no DOM shortcut. Used for **everything
  post-bet/ASCENDING**. Recon never confirmed whether the morphed Bet→Cash Out control keeps the
  same CSS classes — if it does, DOM matching would keep reporting "Bet Button" no matter what
  the control actually says, silently masking the real state. Vision reads what's actually on
  screen instead of trusting stale assumptions about markup.

`SPRIBE_DOM_SELECTORS` only covers Spribe/Aviator's pre-bet markup today. Any other provider (or
Spribe's post-bet state) falls through to vision automatically — the DOM path is a pure
optimization, never a hard dependency.

---

## 4. Round-state observer (`CrashObserver`)

```
        ┌─────────┐   BETTING seen    ┌───────────┐  ASCENDING seen  ┌─────────┐
        │ LOADING │ ────────────────► │  BETTING  │ ───────────────► │ASCENDING│
        └─────────┘                   └───────────┘                  └────┬────┘
             │                              ▲                             │
             │ DISCONNECTED                 │        RESULT/next round    │ CRASHED
             ▼                              └─────────────────────────────┤
        ┌─────────────┐                                                   ▼
        │ DISCONNECTED│◄──────────────────────────────────────────  ┌──────────┐
        └──────┬──────┘         (auto_handle_crash_startup           │ CRASHED  │
               │                  reload-retries here, see below)    └──────────┘
               ▼
     reload_retries exhausted → real abort (_abort_session)
```

Each `sample()` call: screenshot → `read_round_state()` (Gemini vision phase classifier) →
overlay any WS frames drained since the last sample via `parse_round_frame()` (network truth
wins over vision, same precedence `slot_spin` gives spins) → corroborate with `frame_motion`
(a rising multiplier animates; BETTING/CRASHED are comparatively still).

**Self-healing reload** (`auto_handle_crash_startup`, `reload_retries=2`): a DISCONNECTED read
is not always a dead session. The dominant real-world cause (confirmed 2026-07-27 across many
live attempts) is Spribe's own client-config fetch (`app-config.spribegaming.com/aviator/
<operator>.json` — a **static per-operator file, not scoped to the session token**)
intermittently failing CORS and giving up for good on *that* page load
("Retry limit of client config exceeded!"). A `page.reload()` of the identical URL succeeds
roughly half the time with no other change — safe to retry because the failing resource carries
no token, so a reload can't burn a single-use launch token the way re-authenticating would.
This does **not** cover a genuine spent/reused-token disconnect ("session held by another
tab") — `reload_retries` just caps the blast radius before the caller aborts for real.

**`parse_round_frame()` is still a placeholder.** Recon confirmed the transport (SignalR
pushhub, topic `vpb.message`) but never captured a live payload to map fields against. Every WS
frame `CrashObserver` sees is logged to `round_frames.jsonl` in the run dir regardless — the
next run against a live token gives real payloads to finish this function against. Until then,
phase/multiplier truth is vision-only (corroborated by motion), not network-verified the way a
slot spin's payout is.

---

## 5. Test sequence (`run_crash_tests`)

```
 SETUP: goto(url) → auto_handle_crash_startup (self-healing reload) → learn_idle(8s)
        → pre_check (reconfirm live phase — startup's "ready" can go stale during learn_idle)
   │
   ├─ TEST 1  Crash UI controls detected            (detect_controls — DOM-first)
   ├─ TEST 2  Round lifecycle: BETTING→ASCENDING→CRASHED   (CrashObserver.wait_for_phase ×3)
   ├─ TEST 3  Multiplier ascends monotonically       (CrashObserver.observe_ascending)
   ├─ TEST 4  Round history strip updates after crash (two-frame Gemini diff)
   │
   └─ if --dry-run (default): WAGER TESTS 1-7 all print SKIP, stop here.
      if --live (+ --bet, capped by config_env.MAX_STAKE):
        ├─ WAGER TEST 1   Bet during BETTING → exactly 1 place-bet request
        ├─ WAGER TEST 2/3 Cash out mid-flight → payout≈bet×multiplier; balance reconciles
        ├─ WAGER TEST 4   Auto cash-out fires at configured target
        ├─ WAGER TEST 5   Bet click mid-ASCENDING is rejected (re-detects via *_vision first!)
        ├─ WAGER TEST 6   Cancel before round start refunds the stake
        └─ WAGER TEST 7   Rapid double-click Bet = still exactly 1 bet
```

Every wager action (`place_bet`, `cash_out`) is verified the same way `slot_spin` verifies a
spin: first non-idle POST/WS request after the click, reconciled via
`slot_spin.parse_result_body()` (network truth), never inferred from vision alone.

---

## 6. Safety model

| Gate | Mechanism |
|---|---|
| Default dry-run | `--live` required to run WAGER TESTs at all; otherwise all 7 print SKIP. |
| Stake cap | `config_env.MAX_STAKE` — `place_bet()` refuses to click if `stake > MAX_STAKE`. |
| Same-account concurrency | Dashboard's Crash Sweep runs picks **one at a time**
(`_start_fleet(specs, max_parallel=1, [], cooldown=20)`) — every game in a sweep authenticates
with the *same* account, and a real concurrent second login on one account is exactly the
"session held by another tab" disconnect this framework already has to detect. The 20s cooldown
(added 2026-08-03, see §9) holds the next launch back until the casino backend's session lock
for that account has had time to release, instead of firing the instant the previous browser
process exits. No parallel-browsers knob is offered for crash the way Auto Sweep has for slots
(which spreads across *different* accounts). |
| DSC compliance scope | Crash is explicitly **out** of the slot DSC weekly-rotation pass
(`SLOT_TEST_CHECKLIST.md`: "handled outside the slot UI pass"). Nothing in the crash path writes
to `dsc_history_db` or generates a DSC Excel report — `/launch-crash-sweep` passes an empty
`group_meta` to the fleet scheduler specifically to skip that recording path. |

---

## 7. Dashboard integration

```
  index.html
  ┌─────────────────────────────────────────────────────────────┐
  │ TEST TYPE  [ Slot ] [ Crash ]  ◄── #modeToggle               │
  │   toggles which sections show; launch-dock (the big          │
  │   "Start Auto Sweep" button) is slot-only, hidden in Crash    │
  │   mode since crash has its own inline Run buttons.            │
  ├─────────────────────────────────────────────────────────────┤
  │ Slot mode:  Auto Sweep card  (unchanged, pre-existing)        │
  ├─────────────────────────────────────────────────────────────┤
  │ Crash mode:                                                   │
  │   ┌─ Crash game · dry-run ─────────────────────────────────┐  │
  │   │ Game name [typeahead ──► POST /api/games/search]        │  │
  │   │   …or a direct launch URL                                │
  │   │ [Run crash test (dry-run)] ──► POST /launch              │
  │   │              {game_type:'crash', game, username, ...}   │
  │   └───────────────────────────────────────────────────────┘  │
  │   ┌─ Crash sweep · dry-run ────────────────────────────────┐  │
  │   │ [Discover crash games] ──► POST /api/crash-sweep-plan    │
  │   │        └─► provider_sweep.list_crash_games(brand,region) │
  │   │             (ONE call — reads the Categories API's       │
  │   │              curated "crashgames" bucket directly,       │
  │   │              not a per-provider walk like slots need)    │
  │   │ [checklist: 34 titles, checkboxes, select all/none]      │
  │   │ [Run N selected (dry-run)] ──► POST /launch-crash-sweep   │
  │   │        └─► _start_fleet(specs, max_parallel=1, [])        │
  │   │             one crash_auto.py subprocess per pick,        │
  │   │             run serially (§6)                             │
  │   └───────────────────────────────────────────────────────┘  │
  └─────────────────────────────────────────────────────────────┘
```

`list_crash_games()` is the one piece of dashboard-side logic that *doesn't* mirror the slot
sweep's approach on purpose: the slot Auto Sweep (`provider_sweep.pick_game`) must walk all ~47
providers one API call each, because it needs the *current* rotation-eligible pick per provider.
Crash discovery just needs the full title list once, and the Categories API already returns
that in a single `categories[].name == "crashgames"` bucket — so `list_crash_games()` is one
HTTP call total, not forty-seven.

---

## 8. File map

| File | What lives there |
|---|---|
| `crash_auto.py` | Everything in §2–6: detection, `CrashObserver`, wager actions, `run_crash_tests`, `resolve_launch_url`, the `__main__` CLI. |
| `modules/provider_sweep.py` | `list_crash_games()` (+ the pre-existing `get_providers`/`pick_game`/`_is_slot` for the slot sweep, untouched). |
| `app.py` | `/api/crash-sweep-plan`, `/launch-crash-sweep`, the existing `/launch` `game_type=='crash'` branch, `/api/games/search` (shared typeahead endpoint, used by both slot and crash). |
| `templates/index.html` | `#modeToggle` (Slot/Crash), the crash game-name typeahead (`#crashGameSuggest`), the Crash Sweep card (`#crashSweepSection`). |
| `runs/<timestamp>_*/` | Per-run screenshots, video, `results.json`, and (once a live token is captured) `round_frames.jsonl` — the raw WS-frame log `parse_round_frame()` still needs to be finished against. |

---

## 9a. Session/launch-token reliability (2026-08-03 fixes)

A 15-provider validation sweep (`runs/provider_validation_dryrun/`) found MOST outright failures
were **not** detection problems (§3's GATE-phase + functional-wording fixes resolved the two real
detection gaps found there — Light-and-Wonder, Playtech-Live) but **session drops**: "DISCONNECTED"
/ "session held by another tab" aborts on Aviatrix, Betway, Games-Global, Playtech,
Pragmatic-Play, Spribe, and Light-and-Wonder (on a different attempt). `resolve_launch_url()`
fetches a genuinely fresh launch URL every call — no client-side token caching/reuse — so this
points at the casino backend's own wallet/game-session lock for that account not having released
yet, most likely because a *previous* run on the same account ended without a clean in-game exit
(a timeout, an abort, or the browser just closing). Two mitigations, both cheap and low-risk:

1. **`_start_fleet`'s `cooldown` param** (§6, §7) — the fleet scheduler used to start the next
   sweep game the INSTANT the previous game's subprocess exited (zero gap). Crash Sweep now holds
   each finished slot for 20s before the next launch, giving the backend's lock time to expire.
2. **One reload-retry on the post-startup `pre_check`** (`run_crash_tests`, right after
   `learn_idle`) — previously a single DISCONNECTED/timeout here aborted immediately, even though
   `auto_handle_crash_startup`'s OWN reload-retry logic (just a few seconds earlier in the same
   run) already proves a reload recovers roughly half of these. Applied the same pattern here.

Neither mitigation is proven to fully close the gap — they weren't re-verified against a live
sweep after landing (that's the natural next step for whoever picks this up). They also do NOT
help a genuine spent/reused-token disconnect (a real second tab/session) — only the transient
class both already have evidence for.

---

## 9. Known sharp edges (for whoever picks this up next)

1. **`parse_round_frame()` is a placeholder** (§4) — phase/multiplier truth is vision-only until
   a live `vpb.message` payload gets captured and mapped.
2. **DOM recon covers exactly one provider's pre-bet state.** `SPRIBE_DOM_SELECTORS` was
   captured from one live Aviator session; Cash Out / Cancel Bet / Auto Cash Out selectors were
   never observed (recon never reached ASCENDING with a live bet) and stay vision-only until
   captured.
3. **No multi-account parallelism for crash sweeps** — by design (§6), but it means a sweep of
   34 titles runs fully serially. Spreading across multiple accounts (like slot Auto Sweep does)
   is future work, not built.
4. **Geo-gating + IP-reputation blocking both apply, from different layers.** Betway's own auth
   geo-gates by IP (a ZA account from a non-ZA IP gets HTTP 401). Separately, Spribe's own
   config CDN appears to intermittently reject requests from some VPN exit IPs regardless of
   region-correctness — the two failures look different (an HTML redirect to
   `block.sse.cisco.com` vs. a CORS/"Retry limit" error) and need to be told apart when
   diagnosing a stuck run.
