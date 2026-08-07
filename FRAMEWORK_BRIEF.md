# Slot Automation Framework — Technical Brief

> Vision-driven, network-verified, **provider-independent** QA automation for browser slot games.
> Scope of this brief: `test_spin_button.py`, `slot_spin.py`, `slot_explore.py`, `slot_agent.py`.
> Audience: an AI architect syncing execution frameworks. All signatures below are exact.

---

## 1. System Overview

```
                         python test_spin_button.py   (the __main__ entry)
                                      │  GAME RESOLVER → auth → resolve iframe URL
                                      ▼
                            run_tests(url, …)           ← owns the Playwright page + live VIEWPORT_*
            ┌──────────────┬──────────────────┬───────────────────────────┐
            ▼              ▼                  ▼                           ▼
   detect_controls_   spin_and_measure   qa_explore (slot_agent)   NetworkMonitor
   merged (vision)    (slot_spin)        ├ drive_autoplay          (endpoint discovery,
                                         ├ examine_panel / dfs       spin counting)
                                         └ page_through_paytable
```

**Module dependency graph** (all imports are module-level):

| Module | Imports from siblings | Role |
|---|---|---|
| `test_spin_button.py` | — (imports `slot_spin` lazily in tests) | **Foundation**: Gemini calls, vision detectors, OCR, highlight overlays, `NetworkMonitor`, the `run_tests` suite, and the `__main__` GAME RESOLVER entry. Owns the live `VIEWPORT_WIDTH/HEIGHT`. |
| `slot_spin.py` | `from test_spin_button import read_game_values, parse_amount, NOISE_PATTERNS` | Universal single-spin executor + completion/result measurement + `SpinNetMonitor`. |
| `slot_explore.py` | `import test_spin_button as T`, `import slot_spin` | Recursive control-tree mapper (open→describe→close each panel). Provides shared vision helpers (`_cfg`, `_thumb`, `_center`, `_safe`, `describe_panel`, `detect_panel_controls`). |
| `slot_agent.py` | `import test_spin_button as T`, `import slot_spin`, `from slot_explore import _thumb, _cfg, _safe, detect_panel_controls, describe_panel` | **Operating agent**: drive autoplay, DFS menu crawler, paytable paging. The most recently refactored module; defines its own double-import-safe scaling. |

**Design tenets**

- **Vision-first, provider-agnostic**: Gemini 2.5 Flash returns normalized `box_2d` boxes; no endpoint/JSON shape/layout is hard-coded.
- **Network-verified**: spins are confirmed/counted by intercepted fetch/xhr/WS traffic, with frame-motion as a fallback signal.
- **Verify-everything**: every click is followed by a screenshot + image-delta check (`frame_motion`).
- **Safety**: money/exit controls are hard-blocked; autoplay exposure is bounded by an aggressive early stop.

---

## 2. Coordinate-Scaling Pipelines (CRITICAL)

All vision output uses **`box_2d = [ymin, xmin, ymax, xmax]`, normalized 0–1000** (Gemini convention; note the **y-first** ordering). Converting to a CSS click pixel is `coord/1000 * VIEWPORT_DIM`. There are **two distinct scaling code paths**, and the difference between them caused a real production bug.

### 2.1 Live viewport source of truth

`run_tests` (in `test_spin_button.py`) launches a **fixed** desktop viewport `1366×768` with `device_scale_factor=1`, then rebinds the module globals:

```python
VIEWPORT_WIDTH = 1920   # module default at import time
VIEWPORT_HEIGHT = 1080
...
# inside run_tests(): global VIEWPORT_WIDTH, VIEWPORT_HEIGHT
VIEWPORT_WIDTH, VIEWPORT_HEIGHT = page.viewport_size["width"], page.viewport_size["height"]  # → 1366×768
```

`device_scale_factor=1` is deliberate so screenshot pixels == CSS pixels == click pixels.

### 2.2 Pipeline A — legacy/global (test_spin_button + slot_explore)

```python
center = ( int((xmin+xmax)/2/1000 * VIEWPORT_WIDTH),
           int((ymin+ymax)/2/1000 * VIEWPORT_HEIGHT) )
```

- `test_spin_button.detect_all_controls` (lines ~158-161) and `highlight_controls` read the **module-global** `VIEWPORT_*`.
- `slot_explore._center(box)` reads `T.VIEWPORT_WIDTH/HEIGHT` (i.e. the `test_spin_button` module object as seen by `slot_explore`).
- **No clamping**. Boxes are trusted as-is.

### 2.3 Pipeline B — standardized + clamped (slot_agent)

`slot_agent` routes **every** coordinate through one resolver and clamps to the screen:

```python
_live_viewport()  → (W, H)   # double-import-safe (see §2.4)
_norm_to_css(x_norm, y_norm)  → clamps to [0,W-1]×[0,H-1]; WARNS if it had to clamp
_clamp_point(x, y)            → hard clamp for any raw pixel target
_norm_rect(box)               → (l,t,r,b) CSS px, order-normalized
_center(box)                  → center of _norm_rect   (shadows slot_explore._center on purpose)
segment_click_point(box, target_index, total_segments, kind)  → sub-cell / slider-edge math
```

### 2.4 The double-import hazard (root cause of a fixed bug)

The entry point is **`python test_spin_button.py`** (it prints `GAME RESOLVER`, authenticates, resolves the iframe URL, then `asyncio.run(run_tests(...))`). Therefore:

- `__main__` **is** `test_spin_button`; `run_tests`'s `global VIEWPORT_*` updates **`__main__`'s** globals.
- `import test_spin_button as T` inside `slot_agent` / `slot_explore` / `slot_spin` loads a **separate second module object** whose `VIEWPORT_*` stays at the **1920×1080 default forever**.

Consequence: any scaling that reads `T.VIEWPORT_*` (Pipeline A via siblings) uses **stale 1920×1080**, producing off-screen clicks on the real 1366×768 surface (e.g. a Start button computed at `y≈859 > 768`).

`slot_agent._live_viewport()` is the fix — it prefers `__main__`'s live values when `__main__` is `test_spin_button.py`:

```python
def _live_viewport():
    main = sys.modules.get("__main__")
    if main is not None and main is not T and os.path.basename(getattr(main,"__file__","") or "")=="test_spin_button.py":
        w,h = getattr(main,"VIEWPORT_WIDTH",None), getattr(main,"VIEWPORT_HEIGHT",None)
        if w and h: return w, h
    return T.VIEWPORT_WIDTH, T.VIEWPORT_HEIGHT
```

> ⚠️ **Known sharp edge (not yet hardened):** `test_spin_button.detect_all_controls` and `slot_explore._center` do **not** use `_live_viewport()`. When reached via the sibling import path (e.g. `slot_agent` → `T.detect_controls_merged`, or the DFS crawler → `slot_explore.detect_panel_controls`), they scale against the **stale** second-instance viewport. Inside `run_tests`/`__main__` (Tests 1–8) they are correct. **Recommendation when syncing frameworks: route ALL scaling through a single shared `_live_viewport()`-style resolver, or eliminate the double import (make a thin launcher that `import`s `test_spin_button` instead of running it as `__main__`).**

### 2.5 `segment_click_point` (composite-selector math)

For a single box that visually contains a *row/grid/slider* of values:

- `kind` contains `"slider"` → click the **far-left edge** (`l + ~4% width`, mid-height) = lowest value.
- otherwise split by **aspect ratio**: `width ≥ height` → horizontal row, split width into `total_segments`; else vertical grid, split height. Click the center of `target_index` (0 = lowest/first).

---

## 3. File-by-File Reference (exact signatures)

### 3.1 `test_spin_button.py` — foundation + suite

**Module constants:** `API_KEYS` (from `api_keys.json` `single_key`/`key_list`), `WAIT_SECONDS=30`, `VIEWPORT_WIDTH=1920`, `VIEWPORT_HEIGHT=1080` (mutated to live size in `run_tests`), `MAX_RETRIES=5`, `AUTOPLAY_ONLY=True` (token-saving: skip all checks except autoplay), `SCREENSHOT_DIR`, `client` (genai client), `NOISE_PATTERNS` (substrings used to drop analytics/telemetry URLs), `current_key_idx`.

| Signature | Returns / Notes |
|---|---|
| `rotate_api_key()` | No-op in single-key mode; rotates `client` across `API_KEYS` otherwise. |
| `_ss(name)` | Path inside `SCREENSHOT_DIR`. |
| `parse_gemini_json(text: str)` | Strips ```` ```json ```` fences → `json.loads`. |
| `gemini_call(contents, config) -> str` | Calls `gemini-2.5-flash`; retries up to `MAX_RETRIES` on UNAVAILABLE/429/5xx/QUOTA with key rotation + backoff. |
| `detect_all_controls(image: Image.Image) -> list` | Vision detect of bar controls → `[{label, box_2d, center}]`. **Pipeline A scaling.** Thumbnails to 1024². |
| `detect_controls_merged(image, passes=2) -> list` | Unions `detect_all_controls` over N passes by lowercased label (reduces vision misses). |
| `read_game_values(image: Image.Image) -> dict` | Vision OCR → `{"balance": str, "bet": str}`. |
| `parse_amount(text: str) -> float \| None` | Currency string → float; handles EU/US separators. |
| `find_control(controls, *keywords) -> dict \| None` | First control whose label contains any keyword. |
| `_extract_path(url) -> str` / `_is_noise(url) -> bool` | URL path / noise filter via `NOISE_PATTERNS`. |
| `class TestResult(name, screenshot="")` | Fields: `passed`, `details`, `screenshot`, `video`, `video_start/end`. |
| `async draw_highlight(page, xmin, ymin, xmax, ymax, text, color="lime")` | Injects a labeled `<div.melon-highlight>` overlay. |
| `async highlight_controls(page, controls, duration=3.0)` | Boxes every detected control (Pipeline A). |
| `async flash_target(page, center, label, color="cyan", hold=0.7, radius=46)` | Briefly boxes a point about to be clicked. |
| `async clear_highlights(page)` | Removes overlays. |
| `class NetworkMonitor` | See §4.3. |
| `async auto_handle_startup(page)` | Gemini loop that dismisses loading/overlays until the game is "ready". |
| `_emit_report(results)` | Renders the test report. |
| `async run_tests(url, spin_center_override=None, mobile=False, default_bet="", min_bet="", region="ZA")` | The whole suite; sets live `VIEWPORT_*`; launches `1366×768` fixed viewport, `device_scale_factor=1`. TEST 9 calls `slot_agent.qa_explore`. |

### 3.2 `slot_spin.py` — universal spin executor

**Constants/helpers:** `frame_motion`, `_is_noise`, `_path`.

| Signature | Returns / Notes |
|---|---|
| `frame_motion(a_path, b_path) -> float` | **Mean abs grayscale diff (0–255)** of two frames downscaled to 160×320. THE universal motion/change signal used everywhere. |
| `class SpinNetMonitor` | `__init__()`; `attach(page)`; `async learn_idle(secs=6)`; `req_count() -> int`; `spin_request_since(idx) -> dict\|None`; `response_for(path, after_t) -> dict\|None`. Learns idle paths, then first non-idle/non-noise fetch/xhr/WS = the spin. |
| `parse_result_body(body: str) -> dict` | Provider-agnostic deep numeric scan → `{wager, payout, balance, win, feature, feature_name, found}`. Normalizes `…InCents`/100. Returns `None`-ish on failure. |
| `withholding_tax(payout, wager, region) -> float` | 15% on net win for `MZ`/`ZM`. |
| `async spin_and_measure(page, spin_center, monitor, ss_dir, tag="spin", region="ZA", use_network=True, settle_thresh=3.0, settle_need=3, hard_cap=25.0, result_timeout=12.0)` | One spin end-to-end. Returns `{shots, spin_fired, reels_moved, result_time, settle_time, values{…}, tax}`. **Result precedence: network-exact > visual-delta**; if no spin fired and no motion → `status="no_spin"` (never derives a payout). |

### 3.3 `slot_explore.py` — recursive control-tree mapper

**Constants:** `PANEL_TARGETS` (menu/info/paytable/setting/help/rules/auto/bonus/bank/history), `SKIP_WORDS` (word-boundary skip: spin/bet/max/balance/jackpot/display/turbo/…), `CLOSE_MOTION_THRESH=6.0`, `SPIN_MOTION_THRESH=5.0`.

| Signature | Returns / Notes |
|---|---|
| `_cfg()` | `GenerateContentConfig(json, thinking_budget=0)` — shared by all modules. |
| `_thumb(path)` | Opens + thumbnails to 1024² for API calls. |
| `_center(box)` | **Pipeline A** scaling via `T.VIEWPORT_*` (see §2.4 hazard). Defensive (≥4 values). |
| `_safe(label)` | Filename-safe slug (≤24 chars). |
| `_skip(label_low) -> bool` | Word-boundary match against `SKIP_WORDS`. |
| `describe_panel(before_path, after_path) -> dict` | Two-frame "did a panel open?" → `{opened, panel_type, title, buttons[], close_button}`. |
| `detect_panel_controls(panel_path) -> list` | In-panel controls → `[{label, center}]` (Pipeline A scaling). |
| `find_panel_control(controls, *keywords)` / `async click_panel_control(page, controls, *keywords) -> bool` | Lookup / click by keyword. |
| `async close_panel(page, base_path, ss_dir, tag, close_btn=None) -> bool` | X → Escape → safe-tap → re-detect close; verifies via `frame_motion < CLOSE_MOTION_THRESH`. |
| `async explore_control(page, label, center, base_path, ss_dir) -> dict` | Open one control with spin-safety guard (won't re-click if a spin fired). |
| `async map_control_tree(page, ss_dir, targets=PANEL_TARGETS) -> dict` | `{bar:[labels], panels:[node,…]}`. |
| _(also: `_is_closed`, `_ongoing_motion`, `_wait_settle`)_ | Internal verify helpers. |

### 3.4 `slot_agent.py` — operating agent (most refactored)

**Scaling (§2.3):** `_live_viewport()`, `_norm_to_css(x,y)`, `_clamp_point(x,y)`, `_norm_rect(box)`, `_center(box)`, `segment_click_point(box, target_index=0, total_segments=6, kind=None)`.

**Network:** `count_autospins(monitor, since_idx) -> (count, path)` — most-frequent non-idle path since index.

**Single-purpose vision locators:** `locate_autoplay_button(image_path) -> center`, `find_stop_control(running_path) -> {"stop_control": …}`, `locate_menu_icon(image_path) -> center`.

**Unified panel context (single cached vision call):**

| Signature | Returns / Notes |
|---|---|
| `parse_panel_context(image_path, force=False) -> PanelContext` | ONE Gemini call → full element map, cached by path in `_PANEL_CTX_CACHE`. Applies the **bottom-band filter** (`_BOTTOM_BAND_YMAX=800`) and **panel-bbox isolation** (`_PANEL_BBOX_PAD=60`) to drop background bleed, keeping `_CORE_PANEL_ROLES`. |
| `class PanelContext(elements, plan, image_path, error=None)` | `.clickable()`, `.by_role(*roles)`, `.find(predicate)`, `.panel_bbox()`, `.is_autoplay_menu()`, truthy iff non-empty. Each element carries `center`, `css_rect`, `_box_full_px`. |
| `_detect_spin_chooser(panel_path) -> dict` | **Secondary** focused call (`_SPIN_CHOOSER_PROMPT`) for dropdown/stepper/slider — only when the primary pass finds no visible chooser (kept separate to avoid degrading the common discrete-button case). |

Constants: `_PANEL_CONTEXT_PROMPT`, `_SPIN_CHOOSER_PROMPT`, `_SPIN_CHOOSER_ROLES`, `_CORE_PANEL_ROLES`, `_BOTTOM_BAND_YMAX=800`, `_PANEL_BBOX_PAD=60`.

**Dynamic minimum-spin selection (no hardcoded targets):**

| Signature | Returns / Notes |
|---|---|
| `_extract_int(text) -> int\|None` | Last digit-run in a string. |
| `spin_count_value(e) -> int\|None` | Int from element `value` then `label`. |
| `lowest_spin_count_element(elements) -> (el, val)` | Lowest integer among role `spin_count`. |
| `async _act_and_verify(page, point, label, ss_dir, tag, step, color="cyan", settle=1.4, thresh=6.0) -> (changed, after_path)` | Atomic observe→act→verify; clamps point. |
| `_region_motion(a_path, b_path, rect=None) -> float` | Cheap crop-region diff (stepper stability). |
| `async _select_via_dropdown(page, dd, ss_dir, tag) -> (ok, value)` | Expand → re-detect → click lowest. |
| `async _select_via_stepper(page, minus_el, value_el, ss_dir, tag, max_steps=12) -> (ok, presses)` | Click minus until value region stabilizes. |
| `async select_minimum_spins(page, ctx, panel_path, ss_dir, tag, spin_segments=6, spin_target_index=0) -> dict` | Dispatcher: discrete buttons → row/slider → dropdown → stepper. |

**Autoplay capability (state machine):**

```
async def drive_autoplay(page, autoplay_center, monitor, ss_dir, tag="autoplay",
                         confirm_spins=2, watch_s=16.0, spin_segments=6, spin_target_index=0) -> dict
```
States `OPEN → SELECT → START → RUN_STOP → VERIFY → DONE`. Returns
`{opened, started, spins_observed, stopped, notes[], shots{}, options[], plan[], spin_selection}`.
**Stop safety:** the moment `max(network_spins, motion_spins) >= confirm_spins`, it clicks the resolved `stop_control` (or the spin-as-stop fallback), then backstops with vision/Escape. Bounds real-money exposure to ~`confirm_spins`.

**Autonomous DFS menu crawler:**

| Signature | Returns / Notes |
|---|---|
| `classify_target(label, ttype=None) -> {'exit'\|'money'\|'close'\|'safe_nav'\|'other'}` | Intent gate; `exit`/`money` are **hard-blocked**, `close` reserved for reset. |
| `_find_close(elements)` / `async _paginate_subview(page, ss_dir, prefix, max_pages)` / `async _return_to(page, base_shot, ss_dir, tag, reopen)` | DFS helpers. Thresholds `CLOSE_DELTA=6.0`, `OPEN_DELTA=4.0`. |
| `async _dfs_level(page, ss_dir, tag, base_shot, depth, max_depth, max_breadth, pagination_max, reopen, elements=None) -> [node]` | One level: discover → drill safe_nav → paginate → recurse → return. Re-discovers coords if a reset overshoots. |
| `async dfs_explore(page, opener_label, opener_center, ss_dir, tag=None, max_depth=2, max_breadth=4, pagination_max=4, relocate_fn=None) -> dict` | Engine. `{panel, opened, shots, notes, base_elements, tree, flat}`. |
| `async drill_menu(page, menu_center, ss_dir, tag="menu", max_options=4) -> dict` | Legacy wrapper (depth-1) over `dfs_explore`; `relocate_fn=locate_menu_icon`. |
| `async examine_panel(page, opener_label, opener_center, ss_dir, tag=None, max_drill=3) -> dict` | Legacy wrapper (depth-2). Returns `{panel, opened, options[…], notes, shots}` for the report. |
| `async agent_explore(page, opener_label, opener_center, ss_dir, tag=None, max_steps=7, max_depth=2) -> dict` | DFS replacement of the old LLM-planner loop; returns `{panel, opened, trace, tree, base_elements, notes, shots}`. |
| `describe_panel_options(panel_path) -> list` / `_describe_merged(panel_path, passes=2) -> list` | Per-option semantic description (`type/state/purpose/opens_subpanel/center`), 2-pass merged. |
| `async page_through_paytable(page, pt_center, ss_dir, tag="paytable", max_pages=5) -> dict` | Pages via next/forward controls (uses `slot_explore.detect_panel_controls`). |
| `async _fresh_find(page, ss_dir, name, *keywords)` | Re-detect a bar control right before use (state drifts after each feature). |
| `async qa_explore(page, monitor, ss_dir, region="ZA") -> dict` | **Top-level sequencer** (called by `run_tests` TEST 9): autoplay → menu → buy-bonus → paytable. Returns `{feature: result}`. |

---

## 4. Cross-Module Contracts

### 4.1 Vision element (primary detection map — `parse_panel_context`)
```jsonc
{ "label": "spins_10", "role": "spin_count|spin_row_selector|spin_slider|start|stop|toggle|input|close|other",
  "value": "10", "segments": 6, "box_2d": [ymin,xmin,ymax,xmax],
  "center": [cssX, cssY], "css_rect": [l,t,r,b], "_box_full_px": [l,t,r,b] }
```
Roles `spin_dropdown|spin_minus|spin_plus|spin_value|slider` come from the **secondary** `_detect_spin_chooser` call.

### 4.2 `drive_autoplay` result (consumed by `run_tests` TEST 9 reporting)
`{opened, started, spins_observed, stopped, notes[], shots{panel,running,stopped}, options[], plan[], spin_selection{strategy,selected,value}}`

### 4.3 `NetworkMonitor` (test_spin_button) vs `SpinNetMonitor` (slot_spin)
Two separate monitors with overlapping intent — **a sync candidate**:

| | `NetworkMonitor` (test_spin_button) | `SpinNetMonitor` (slot_spin) |
|---|---|---|
| idle learn | `learn_idle(duration=8)` → `idle_post_paths` | `learn_idle(secs=6)` → `idle_paths` |
| spin discovery | `discover_spin_endpoint(page, spin_center)` → `spin_endpoint` | `spin_request_since(idx)` (no calibration click) |
| counting | `start_monitoring()`/`spin_count` property/`wait_for_spin_completion` | `req_count()` + caller-side `count_autospins` |
| WS handling | `@WS_SEND` path suffix | `@ws` path suffix |

`qa_explore` receives an external `SpinNetMonitor`-style monitor; `count_autospins` (slot_agent) reads `monitor.requests`, `monitor.idle_paths`, `monitor.req_count()` → it expects the **`SpinNetMonitor`** interface.

---

## 5. Key Thresholds & Tunables

| Constant | Value | Where | Meaning |
|---|---|---|---|
| `VIEWPORT_WIDTH/HEIGHT` | `1366×768` (live) | test_spin_button | Fixed render area; default 1920×1080 is a **stale trap** (§2.4). |
| `MAX_RETRIES` | 5 | test_spin_button | Gemini retry budget. |
| `AUTOPLAY_ONLY` | `True` | test_spin_button | Skips all checks except autoplay (token-saving). |
| `frame_motion` "changed" | `> 6.0` | slot_agent (`CLOSE_DELTA`), explore | Sub-view opened / panel closed. |
| panel "opened" | `> 4.0` (`OPEN_DELTA`) | slot_agent / drill | Click actually opened something. |
| spin motion | `> 5.0` | slot_agent / slot_spin | Reels visibly spinning. |
| `confirm_spins` | 2 | `drive_autoplay` | Early-stop threshold (real-money cap). |
| `watch_s` | 16.0 | `drive_autoplay` | Max confirm window. |
| `_BOTTOM_BAND_YMAX` / `_PANEL_BBOX_PAD` | 800 / 60 | slot_agent | Background-bleed filters (normalized). |
| `settle_thresh/need/hard_cap` | 3.0 / 3 / 25.0 | `spin_and_measure` | Reel-settle detection. |

---

## 6. Known Sharp Edges (for the sync)

1. **Double-import viewport hazard (§2.4).** Only `slot_agent` is double-import-safe. `test_spin_button.detect_all_controls` and `slot_explore._center` scale via the stale second-instance `VIEWPORT_*` when called through the sibling import path. *Fix direction: one shared `_live_viewport()` resolver, or a thin launcher that imports rather than runs `test_spin_button` as `__main__`.*
2. **Two network monitors** with divergent APIs (§4.3) — consider consolidating to one.
3. **Vision nondeterminism** on count selectors: the same row may return as N discrete `spin_count` buttons, one collapsed `spin_count`, a `spin_row_selector`, or (mis)read as a dropdown. The dynamic-min selection is therefore **best-effort**; the **aggressive network+motion stop** is the real exposure guarantee.
4. **Auth is out-of-scope here** but currently the upstream blocker (HTTP 401 from the Betway auth endpoint); `modules/auth_handler.py` now surfaces status+body. Automation can't run end-to-end until auth succeeds.
5. **`device_scale_factor=1` is load-bearing** — any change makes screenshot px ≠ click px and silently breaks every pipeline.
```
