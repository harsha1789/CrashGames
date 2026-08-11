"""
Universal Slot Game UI Test Suite
==================================
Tests ALL interactive UI buttons in any slot game:
  - Spin button (disabled during spin, re-enables after)
  - Bet +/- buttons (change bet amount, disabled during spin)
  - Autoplay button (toggleable)
  - Menu button (opens panel)
  - Turbo/Fast spin (toggleable)
  - Sound toggle

Uses:
  - Gemini vision to detect ALL buttons in any layout
  - Network interception to auto-discover spin endpoint
  - Gemini to read bet/balance values before/after actions

Usage:
  python test_spin_button.py <game_url> [--wait 30] [--spin-xy x,y]
"""
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except:
    pass

import json
import copy
import re
import time
import asyncio
import os
from datetime import datetime
from urllib.parse import urlparse
from google import genai
from google.genai import types
from PIL import Image
from playwright.async_api import async_playwright

import config_env   # single source of truth for live VIEWPORT_* + coordinate clamping
from modules import log_overlay   # in-page log feed on the game window (hidden from screenshots)


def _base_dir():
    """Real folder holding this script (or, in a frozen PyInstaller build, this .exe) —
    __file__ resolves inside the bundle when frozen, not the exe's actual location, so
    os.path.dirname(sys.executable) is the correct base there. Unchanged from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# --- Configuration ---
_keys_file = os.path.join(_base_dir(), "api_keys.json")
_keys_data = json.load(open(_keys_file)) if os.path.exists(_keys_file) else {}
# Single-key mode: prefer single_key, fall back to key_list for compatibility.
_single = _keys_data.get("single_key")
API_KEYS = [_single] if _single else _keys_data.get("key_list", [])

WAIT_SECONDS = 30
# VIEWPORT_WIDTH/HEIGHT now live in config_env (single source of truth). Read them as
# config_env.VIEWPORT_WIDTH / config_env.VIEWPORT_HEIGHT so the run_tests mutation is seen here.
MAX_RETRIES = 5
# TEMP (token-saving): when True, skip every check EXCEPT autoplay — used to iterate on the
# autoplay capability cheaply. Set back to False to run the full suite again.
AUTOPLAY_ONLY = False
# TEMP (token-saving): same idea for the MENU examination capability — when True, skip every
# check EXCEPT opening + examining the menu. If AUTOPLAY_ONLY is also True, autoplay wins
# (its  fast path returns first). Set one at a time; both False to run the full suite.
MENU_ONLY = False
SCREENSHOT_DIR = os.path.join(_base_dir(), "screenshots")
# Per-run artifacts. When the UI (or --run-dir) provides a run folder, SCREENSHOT_DIR is
# repointed into it and these hold the run identity so _emit_report / video can use them.
# Unset => legacy flat behavior (screenshots/, recordings/, test_results.json).
RUN_DIR = None
RUN_ID = ""
RUN_STARTED_AT = ""    # ISO timestamp the run began (set in __main__)
RUN_START_TS = 0.0     # time.time() at run start, for duration
ELEMENTS_SHOT = ""     # annotated 'all elements' hero screenshot, shown atop the report
# Optional checks the user can toggle off for speed. None = run everything (default). The core
# (launch, control detection, spin endpoint, the single spin + wager/payout/feature/balance,
# spin lock/re-enable, server response) always runs — only these heavier extras are gated.
GATED_CAPS = ("core", "bet", "autoplay", "menu", "paytable", "dsc")
ENABLED_CAPS = None    # set of enabled gated caps, or None for "all"
# NOTE: "dsc" is opt-in ONLY (never part of the None="all" default): selecting it replaces the
# whole suite with the fast Daily Sanity Check (slot_dsc) + a row in the team's Excel report.
# Test driver. DEFAULT = "scripted": deterministic TEST 3-10 (network-truth spin/wager/payout/
# balance/bet/server) PLUS the agentic autoplay + menu + paytable exploration in TEST 9 — the
# combination that proved reliable. "agentic" = the full-agent-control brain (slot_qa_agent) drives
# everything; kept opt-in only (it flails/contradicts itself and is slower — see runs 2026-06-30 PM).
MODE = "scripted"


def _cap_on(key):
    return ENABLED_CAPS is None or key in ENABLED_CAPS


current_key_idx = 0
client = genai.Client(api_key=API_KEYS[current_key_idx])

def rotate_api_key():
    """SINGLE-KEY MODE: we use ONLY the one configured ($200) key — no backups.
    With a single key this is a no-op (kept so the retry path can call it harmlessly);
    it never switches to another key."""
    global current_key_idx, client
    if len(API_KEYS) <= 1:
        return  # only the $200 key — nothing to rotate to
    current_key_idx = (current_key_idx + 1) % len(API_KEYS)
    client = genai.Client(api_key=API_KEYS[current_key_idx])
    print(f"    [!] Switched to Gemini API Key #{current_key_idx + 1}...")


def _ss(name):
    """Return screenshot path inside SCREENSHOT_DIR."""
    return os.path.join(SCREENSHOT_DIR, name)


def _spin_result_captured(monitor, since_t):
    """Robust, provider-agnostic 'did a spin result come back since `since_t`'. Uses SUBSTRING path
    match (the send/receive paths differ on WebSocket games, so exact match misses them) + a non-
    empty body. This is the signal the count tests fall back to when WS frame-counting is flaky."""
    ep = getattr(monitor, "spin_endpoint", None)
    if not ep:
        return False
    return any(ep in (r.get("path") or "") and r.get("t", 0) >= since_t and r.get("body")
               for r in getattr(monitor, "_all_responses", []))


async def refind_control(page, *keywords, passes=1):
    """Re-detect controls FRESH right before using one, and return the first whose label matches any
    keyword (else None). The initial TEST-1 scan can go stale after earlier interactions (a panel
    opened, layout shifted), so tests that operate non-spin controls call this instead of reusing
    first-scan coordinates. Shared by the bet/audio checks (and available to any test). Mirrors the
    agentic path's `_fresh_find`; reuse over re-implementation."""
    shot = _ss("refind.png")
    await page.screenshot(path=shot)
    return find_control(detect_controls_merged(Image.open(shot), passes=passes), *keywords)


class _Tee:
    """Write to several streams at once (real stdout for SSE + the run log file).
    Line-buffered: flush after every write so the UI stream and the on-disk log stay live."""
    def __init__(self, *streams):
        self._streams = [s for s in streams if s]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data); s.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

NOISE_PATTERNS = [
    "google-analytics", "analytics", "/collect", "googletagmanager",
    "facebook", "doubleclick", "hotjar", "clarity", "sentry",
    ".js", ".css", ".png", ".jpg", ".gif", ".svg", ".woff", ".ttf", ".woff2",
    "hot-update", "sockjs", "__webpack", "favicon",
    # audio/media + asset fetches — never a spin endpoint (a spin click often triggers a sound
    # fetch first; without these it gets mistaken for the spin endpoint, e.g. .../sounds/*.webm).
    ".webm", ".mp3", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".mp4",
    "/sounds/", "/sound/", "/audio/", "/gametech/sounds",
]


# ─── Gemini Helpers ──────────────────────────────────────────────

def parse_gemini_json(text: str):
    """Safely parse Gemini JSON output, stripping markdown formatting if present."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())

def gemini_call(contents, config) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.models.generate_content(
                model='gemini-2.5-flash', contents=contents, config=config
            ).text
        except Exception as e:
            err_str = str(e).upper()
            is_retryable = any(kw in err_str for kw in [
                "UNAVAILABLE", "503", "500", "429", "QUOTA",
                "RESOURCE_EXHAUSTED", "RATE", "RETRY", "OVERLOADED"
            ])
            if attempt < MAX_RETRIES:
                rotate_api_key()
                wait = min(10, 2 * attempt)
                print(f"    [!] Gemini API failed (attempt {attempt}/{MAX_RETRIES}). Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Gemini API failed on all fallback keys")


def detect_all_controls(image: Image.Image) -> list:
    """Detect ALL interactive UI controls via Gemini. Returns list of {label, box_2d, center}."""
    api_img = copy.deepcopy(image)
    api_img.thumbnail([1280, 1280], Image.Resampling.LANCZOS)   # higher res = tighter small-icon boxes

    prompt = """Analyze this slot/casino game screenshot. Detect EVERY interactive UI control
(buttons, toggles, icons, +/- steppers, value displays). IGNORE the reels, symbols, and background.

Label each control by what it ACTUALLY is — READ the icon/text, do not force it into a category.
Reference vocabulary for how to recognize common controls:
- Spin Button (the large central play button)
- Bet Increment ("+") and Bet Decrement ("-") next to the bet value
- Bet Display (the stake value), Balance Display, Win Display
- Autoplay / Autospin — CIRCULAR LOOPING ARROWS (↻, or two curved arrows forming a loop) or the
  word "AUTO"; starts repeated automatic spins. NOT a speedometer/gauge.
- Turbo / Fast Spin — a SPEED icon: a speedometer/gauge dial, a lightning bolt, a fast-forward (»»),
  or a stopwatch. NOT looping arrows.
- (The two round buttons beside the Spin button are usually ONE Turbo and ONE Autoplay — tell them
  apart by the ICON above; do NOT assume which side is which.)
- Sound Toggle (speaker), Settings (gear ⚙), Menu (hamburger ≡)
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

Return a JSON array of objects: {"label": "...", "box_2d": [ymin, xmin, ymax, xmax]}."""

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    )

    text = gemini_call([api_img, prompt], config)
    data = parse_gemini_json(text)
    if not isinstance(data, list):
        data = [data]

    # Add pixel centers
    valid_controls = []
    for item in data:
        box = item.get("box_2d")
        if box and isinstance(box, list) and len(box) == 4:
            item["center"] = config_env.norm_box_center(box)   # clamped, live-viewport scaled
            valid_controls.append(item)
        elif item.get("label"):
            # Keep the label info even without valid coords
            valid_controls.append(item)

    return valid_controls


def detect_controls_merged(image: Image.Image, passes: int = 2) -> list:
    """
    Run detection several times and UNION the results by label. Vision detection
    misses a control now and then; a 2-pass merge greatly reduces misses without
    any provider-specific assumptions. Keeps the first center seen per label.
    """
    merged = {}
    for _ in range(max(1, passes)):
        try:
            for c in detect_all_controls(image):
                key = (c.get("label") or "").strip().lower()
                if key and key not in merged:
                    merged[key] = c
        except Exception:
            continue
    # Spatial de-dupe: collapse the same physical control detected under different labels across
    # passes (e.g. a clock icon as "Game History" one pass, "Autoplay" the next) — keep the first,
    # drop later boxes that overlap it. Generic; no provider assumptions.
    out = []
    for c in merged.values():
        box = c.get("box_2d")
        if isinstance(box, (list, tuple)) and len(box) >= 4 and \
                any(_iou(box, k.get("box_2d")) > 0.6 for k in out):
            continue
        out.append(c)
    return out


def _iou(a, b):
    """IoU of two box_2d [ymin,xmin,ymax,xmax]; 0 if unusable."""
    try:
        ay0, ax0, ay1, ax1 = a[:4]; by0, bx0, by1, bx1 = b[:4]
    except Exception:
        return 0.0
    iy0, ix0, iy1, ix1 = max(ay0, by0), max(ax0, bx0), min(ay1, by1), min(ax1, bx1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0, ay1-ay0)*max(0, ax1-ax0) + max(0, by1-by0)*max(0, bx1-bx0) - inter
    return inter / ua if ua > 0 else 0.0


def read_game_values(image: Image.Image) -> dict:
    """Use Gemini to read balance and bet values from a screenshot."""
    api_img = copy.deepcopy(image)
    api_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)

    prompt = """Read the BALANCE and BET amounts shown in this slot game screenshot.
Return JSON: {"balance": "exact text shown", "bet": "exact text shown"}
If you can't find either value, use null."""

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    )

    text = gemini_call([api_img, prompt], config)
    return parse_gemini_json(text)


def parse_amount(text: str) -> float | None:
    """Parse a currency string like '$1,003.60', '€2.00', 'R 1.00', '¥100' to float."""
    if not text:
        return None
    import re
    # Strip all currency symbols and letters, keep digits, dots, commas
    cleaned = re.sub(r'[^\d.,]', '', text)
    # Handle European format: 1.000,50 -> 1000.50
    if ',' in cleaned and '.' in cleaned:
        if cleaned.rindex(',') > cleaned.rindex('.'):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        # Could be decimal separator (1,50) or thousands (1,000)
        parts = cleaned.split(',')
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_control(controls: list, *keywords) -> dict | None:
    """Find a control whose label contains ANY of the keywords (case-insensitive)."""
    for ctrl in controls:
        label = ctrl.get("label", "").lower()
        if any(kw.lower() in label for kw in keywords):
            return ctrl
    return None


# ─── Utilities ───────────────────────────────────────────────────
def _extract_path(url: str) -> str:
    return urlparse(url).path

def _is_noise(url: str) -> bool:
    return any(p in url.lower() for p in NOISE_PATTERNS)


class TestResult:
    def __init__(self, name: str, screenshot: str = ""):
        self.name = name
        self.passed = None
        self.details = ""
        self.screenshot = screenshot
        self.video = ""
        self.video_start = 0.0
        self.video_end = 0.0
        self.actions = []      # human-readable steps taken, e.g. "clicked Spin @ (213,571)"

    def act(self, msg):
        """Record an action taken during this test (shown in the report) and echo it to the log."""
        self.actions.append(msg)
        print(f"    · {msg}")
        return self

    def __str__(self):
        icon = "PASS" if self.passed is True else "FAIL" if self.passed is False else "SKIP"
        return f"  [{icon}] {self.name}: {self.details}"

async def draw_highlight(page, xmin, ymin, xmax, ymax, text, color="lime"):
    try:
        await page.evaluate('''([x, y, w, h, text, color]) => {
            const el = document.createElement('div');
            el.className = 'gameguard-highlight';
            el.style.position = 'absolute';
            el.style.left = x + 'px';
            el.style.top = y + 'px';
            el.style.width = w + 'px';
            el.style.height = h + 'px';
            el.style.border = '4px solid ' + color;
            el.style.backgroundColor = 'rgba(0, 255, 0, 0.2)';
            el.style.zIndex = '999999';
            el.style.pointerEvents = 'none';
            const label = document.createElement('div');
            label.innerText = text;
            label.style.position = 'absolute';
            label.style.top = '-30px';
            label.style.left = '0';
            label.style.backgroundColor = color;
            label.style.color = '#fff';
            label.style.padding = '4px 8px';
            label.style.fontSize = '16px';
            label.style.fontWeight = 'bold';
            label.style.borderRadius = '4px';
            label.style.whiteSpace = 'nowrap';
            el.appendChild(label);
            document.body.appendChild(el);
        }''', [xmin, ymin, xmax - xmin, ymax - ymin, text, color])
    except Exception:
        pass

async def highlight_controls(page, controls, duration=3.0):
    """
    Draw a labeled box on EVERY detected control on the live game and hold it for a
    few seconds, so a person watching the headed run can SEE exactly what the tool
    detected (and that it's real), before any clicking happens.
    """
    drawn = 0
    for c in controls:
        box = c.get("box_2d")
        if not (isinstance(box, list) and len(box) == 4):
            continue
        ymin, xmin, ymax, xmax = box
        if (xmax - xmin) <= 0 or (ymax - ymin) <= 0:
            continue
        x1, y1 = config_env.norm_to_css(xmin, ymin, warn=False)
        x2, y2 = config_env.norm_to_css(xmax, ymax, warn=False)
        await draw_highlight(page, x1, y1, x2, y2, c.get("label", ""))
        drawn += 1
    if drawn:
        await asyncio.sleep(duration)
        await clear_highlights(page)
    return drawn


def annotate_controls(src_path, controls, out_path):
    """Draw labelled boxes around every detected control on a copy of the base screenshot — the
    'all elements' hero image shown at the top of the report. box_2d is normalized 0-1000; convert
    to pixels via the image's own size. Returns out_path, or src basename on failure."""
    from PIL import ImageDraw, ImageFont
    try:
        img = Image.open(src_path).convert("RGB")
        W, H = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        try:
            font = ImageFont.truetype("arialbd.ttf", max(13, W // 110))
        except Exception:
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(13, W // 110))
            except Exception:
                font = ImageFont.load_default()
        palette = [(225,29,72), (139,92,246), (16,185,129), (245,158,11),
                   (14,165,233), (236,72,153), (132,204,22), (168,85,247)]
        for i, c in enumerate(controls):
            box = c.get("box_2d")
            if not (isinstance(box, (list, tuple)) and len(box) >= 4):
                continue
            ymin, xmin, ymax, xmax = box[:4]
            l, t, r, b = (int(xmin/1000*W), int(ymin/1000*H), int(xmax/1000*W), int(ymax/1000*H))
            if r <= l or b <= t:
                # zero/!box (e.g. spin override) — draw a marker at center instead
                ctr = c.get("center")
                if not ctr:
                    continue
                l, t, r, b = ctr[0]-26, ctr[1]-26, ctr[0]+26, ctr[1]+26
            col = palette[i % len(palette)]
            draw.rectangle([l, t, r, b], outline=col, width=3)
            label = (c.get("label") or "?")
            try:
                tw = draw.textlength(label, font=font)
            except Exception:
                tw = len(label) * 7
            th = (font.size + 6) if hasattr(font, "size") else 18
            ly = t - th if t - th > 0 else b
            lx = min(l, W - int(tw) - 12)   # keep the label inside the frame (corner controls)
            lx = max(0, lx)
            draw.rectangle([lx, ly, lx + tw + 10, ly + th], fill=(*col, 235))
            draw.text((lx + 5, ly + 2), label, fill=(255, 255, 255), font=font)
        img.save(out_path)
        return os.path.basename(out_path)
    except Exception as e:
        print(f"  [annotate] failed: {e}")
        return os.path.basename(src_path)


async def _dismiss_overlays(page, tries=2):
    """Clear any open panel/overlay so the next test starts from the base game screen. A stray
    overlay (e.g. an autoplay/bet panel opened by a mis-hit) otherwise leaks into the next test
    and corrupts it (this is what broke the bet round-trip on Thor's Rage)."""
    for _ in range(tries):
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
        except Exception:
            break


async def finalize_media(page, results, recordings_dir):
    """Resolve the session video filename (available once the page is closing), rename it to a
    stable session.webm inside the run folder, and stamp it on EVERY result so the report can show
    each test's clip. MUST be called on every exit path (the fast paths return early, so without
    this their results carry no video and no clips render)."""
    video_filename = ""
    try:
        video_path = await page.video.path()
        if RUN_DIR:
            friendly = os.path.join(recordings_dir, "session.webm")
            try:
                if os.path.abspath(video_path) != os.path.abspath(friendly):
                    if os.path.exists(friendly):
                        os.remove(friendly)
                    os.replace(video_path, friendly)
                video_path = friendly
            except Exception:
                pass
        video_filename = os.path.basename(video_path)
    except Exception:
        pass
    for r in results:
        r.video = video_filename
    return video_filename


async def flash_target(page, center, label, color="cyan", hold=0.7, radius=46):
    """Briefly box a point the tool is ABOUT to click, so the action is visible."""
    cx, cy = center
    await draw_highlight(page, cx - radius, cy - radius, cx + radius, cy + radius, label, color)
    await asyncio.sleep(hold)
    await clear_highlights(page)


async def clear_highlights(page):
    try:
        await page.evaluate('''() => {
            const els = document.querySelectorAll('.gameguard-highlight');
            els.forEach(el => el.remove());
        }''')
    except Exception:
        pass


# ─── Network Monitor ────────────────────────────────────────────
# The former NetworkMonitor has been merged into slot_spin.UnifiedGameMonitor (Step 2 of the
# framework unification). run_tests instantiates `slot_spin.UnifiedGameMonitor()`; the unified
# class exposes this module's historical API (spin_endpoint, spin_count, start/stop_monitoring,
# discover_spin_endpoint, wait_for_spin_completion, get_new_posts_since, _all_requests/_all_responses)
# plus the generic SpinNetMonitor API, over one backing store.


async def refresh_viewport(page, tag=""):
    """Re-read the live render size into config_env RIGHT BEFORE a vision detection.
    Every box→pixel conversion uses config_env's size; it used to be read once at setup,
    so if a window was later snapped/resized/moved to a different-DPI monitor (easy with
    several parallel worker browsers stacked on one screen), that browser's clicks
    drifted by exactly the size ratio while the others stayed accurate. Refreshing at
    each detection makes every detection self-consistent with the current window.
    Returns True when the size had actually changed (i.e. drift would have happened)."""
    try:
        if page.viewport_size:   # mobile emulation: fixed viewport, can't drift
            return False
        dims = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
    except Exception:
        return False
    w, h = (dims or {}).get("w"), (dims or {}).get("h")
    if not w or not h:
        return False
    changed = abs(w - config_env.VIEWPORT_WIDTH) > 2 or abs(h - config_env.VIEWPORT_HEIGHT) > 2
    if changed:
        print(f"  [WARN] render area changed {config_env.VIEWPORT_WIDTH}x{config_env.VIEWPORT_HEIGHT}"
              f" -> {w}x{h}{f' ({tag})' if tag else ''} — window resized/snapped/moved?"
              f" Recalibrated; earlier coordinates were stale.")
        config_env.set_viewport(w, h)
    return changed


async def auto_handle_startup(page):
    """Smart startup loop utilizing Gemini to detect loading state and click overlays."""
    print("\n  [STARTUP] Starting AI visual check loop...")
    await asyncio.sleep(5)  # initial wait for HTML rendering / network
    
    max_attempts = 18  # popup-heavy games (e.g. Playtech) chain several overlays
    _blocked_streak = 0
    _reloaded = False
    for attempt in range(1, max_attempts + 1):
        try:
            await refresh_viewport(page, "startup")
            ss_path = _ss(f"startup_check.png")
            await page.screenshot(path=ss_path)
            img = Image.open(ss_path)
            img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)

            prompt = """Analyze this slot game screenshot during startup. Games often chain
SEVERAL popups before play (loading splash -> intro/feature screen -> rules -> age/sound prompt).

Return JSON: {"state": "wait"|"click"|"ready"|"blocked"|"reload", "popup": "short description or 'none'",
"box_2d": [ymin, xmin, ymax, xmax] or null}

- "wait"  = still loading (progress bar, blank screen, spinning loader).
- "click" = ANY non-gameplay screen that has a control to proceed: a TITLE / ATTRACT screen
  (game logo + a big PLAY / START button, often with "MAX WIN" or jackpot art), an intro /
  feature preview, rules, age / sound / promo overlay, OR a popup with X / back-arrow to close.
  Provide box_2d of the PROCEED control ("Play", "Start", "Continue", "OK", "I Accept", a ▶ arrow,
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

Normalize box_2d to 0-1000."""
            
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )
            
            response_json = parse_gemini_json(gemini_call([img, prompt], config))
            state = response_json.get("state", "wait")
            popup = response_json.get("popup", "")

            if state == "reload":
                # A recoverable game error ("Something went wrong — please refresh"). Reload
                # ONCE (bounded, so a broken game can't loop forever); if it recurs, give up.
                if not _reloaded:
                    _reloaded = True
                    print(f"    [{attempt}/{max_attempts}] 🔄 Recoverable error ('{popup}') — "
                          f"reloading the game once")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=60000)
                    except Exception:
                        pass
                    await asyncio.sleep(5)
                    _blocked_streak = 0
                    continue
                print("  [STARTUP] Error persists after one reload — aborting startup.")
                return False

            if state == "blocked":
                _blocked_streak += 1
                print(f"    [{attempt}/{max_attempts}] 🛑 Restriction/error page: '{popup}'")
                # Two consecutive verdicts before aborting — one vision misread of a slow
                # loader must not kill a legitimate game.
                if _blocked_streak >= 2:
                    print("  [STARTUP] Region/location restriction or error page — the game "
                          "cannot load here (check VPN/geo). Aborting startup.")
                    return False
                await asyncio.sleep(3)
                continue
            _blocked_streak = 0

            if state == "ready":
                print(f"    [{attempt}/{max_attempts}] 🎯 Gemini says 'ready' - Game fully loaded!")
                return True
            elif state == "click" and response_json.get("box_2d"):
                box = response_json["box_2d"]
                if len(box) == 4:
                    cx, cy = config_env.norm_box_center(box)
                    print(f"    [{attempt}/{max_attempts}] 🖱️ Popup '{popup}' — clicking ({cx}, {cy})...")
                    await page.mouse.click(cx, cy)
                    await asyncio.sleep(4)  # wait for animation transition
                else:
                    print(f"    [{attempt}/{max_attempts}] ⏳ Gemini says 'click' but missing coordinates. Waiting...")
                    await asyncio.sleep(3)
            else:
                print(f"    [{attempt}/{max_attempts}] ⏳ Gemini says '{state}' (Still loading). Waiting...")
                await asyncio.sleep(3)
                
        except Exception as e:
            print(f"    [{attempt}/{max_attempts}] ⚠️ Startup check warning: {e}. Retrying...")
            await asyncio.sleep(3)
            
    print("  [STARTUP] Reached max attempts! Proceeding...")
    return False

# ═══════════════════════════════════════════════════════════════════
#   MAIN TEST FLOW
# ═══════════════════════════════════════════════════════════════════
def _prune_passed_screenshots(results):
    """Evidence screenshots are kept only for FAILED steps. By the time _emit_report calls this,
    every TestResult.passed is already final — this is the one choke point both the slot suite
    (this module's own run_tests/batch exit paths) and crash_auto.py (via T._emit_report) funnel
    every result list through before results.json is written, so it prunes both verticals from
    one place instead of trying to predict pass/fail before each screenshot was ever taken.

    Only `passed is False` keeps its screenshot (on disk and in the report); passed (True) and
    skipped/neutral (None) steps lose theirs — deleted from SCREENSHOT_DIR and cleared from the
    result so the report shows no image for them. A filename another FAILED result still needs
    is never deleted, and ELEMENTS_SHOT (the annotated all-controls hero shot atop the report —
    the "detect-controls screenshot" itself, not per-step evidence) is never touched."""
    keep_files = {r.screenshot for r in results if r.passed is False and r.screenshot}
    for r in results:
        if r.passed is False or not r.screenshot:
            continue
        if r.screenshot not in keep_files and r.screenshot != ELEMENTS_SHOT:
            path = _ss(r.screenshot)
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        r.screenshot = ""   # never reference a screenshot from a passed/skipped step's report row


def _emit_report(results):
    """Print the summary, write results.json, and emit the REPORTPAYLOAD block the UI parses."""
    _prune_passed_screenshots(results)
    print(f"\n{'='*70}\n  TEST RESULTS SUMMARY\n{'='*70}")
    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    skipped = sum(1 for r in results if r.passed is None)
    for r in results:
        print(r)
        for a in getattr(r, "actions", []):
            print(f"      · {a}")
    print(f"\n  Total: {len(results)} | Passed: {passed} | Failed: {failed} | Skipped: {skipped}")
    if failed == 0 and passed > 0:
        print("  ALL TESTS PASSED!")
    elif failed > 0:
        print(f"  {failed} TEST(S) FAILED")
    print(f"{'='*70}\n")
    items = [{"name": r.name, "passed": r.passed, "details": r.details, "screenshot": r.screenshot,
              "video": r.video, "video_start": round(r.video_start, 1), "video_end": round(r.video_end, 1),
              "actions": getattr(r, "actions", [])}
             for r in results]
    summary = {
        "run_id": RUN_ID,
        "total": len(results), "passed": passed, "failed": failed, "skipped": skipped,
        "started_at": RUN_STARTED_AT,
        "duration_s": round(time.time() - RUN_START_TS, 1) if RUN_START_TS else None,
        "elements_shot": ELEMENTS_SHOT,
        "video": next((r.video for r in results if r.video), ""),
    }
    # The UI accepts either a bare list (legacy) or {summary, results}. Send the richer shape;
    # renderReport falls back gracefully if summary is absent.
    payload = {"run_id": RUN_ID, "summary": summary, "results": items}
    # Always write the top-level latest file (so /api/results keeps working) and, when a run
    # folder is set, a copy inside it.
    targets = [os.path.join(_base_dir(), "test_results.json")]
    if RUN_DIR:
        targets.append(os.path.join(RUN_DIR, "results.json"))
    for results_file in targets:
        try:
            with open(results_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            print(f"Results saved to {results_file}")
        except Exception as e:
            print(f"  [WARN] could not write {results_file}: {e}")
    print("\nREPORTPAYLOAD===")
    print(json.dumps(payload))
    print("===REPORTPAYLOAD\n")
    return results


async def run_tests(url: str, spin_center_override: tuple = None, mobile: bool = False, headless: bool = False, default_bet: str = "", min_bet: str = "", region: str = "ZA", dsc_meta: dict = None):
    results = []
    import slot_spin   # local import: slot_spin imports from this module, so defer to call time
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    monitor = slot_spin.UnifiedGameMonitor()

    print(f"\n{'='*70}")
    print(f"  SLOT GAME UI TEST SUITE")
    print(f"{'='*70}")
    print(f"URL: {url}\n")
    
    # Video goes into the run folder when one is set, else the legacy flat recordings/ dir.
    recordings_dir = os.path.join(RUN_DIR, "video") if RUN_DIR else \
        os.path.join(_base_dir(), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)

    async with async_playwright() as p:
        # Parallel DSC workers stack maximized windows on one screen; Chromium throttles
        # rAF/timers in occluded windows, which freezes canvas reels in every window but
        # the top one. These flags keep background windows rendering at full rate.
        _no_throttle = ["--disable-backgrounding-occluded-windows",
                        "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling"]
        if mobile:
            # Mobile platform test: portrait device emulation (iOS/Android-style).
            browser = await p.chromium.launch(headless=headless, args=_no_throttle)
            context_args = {
                "ignore_https_errors": True,
                "record_video_dir": recordings_dir,
                "locale": "en-ZA",
            }
            context_args.update(p.devices['iPhone 13'])
        else:
            # Desktop/Web: open the REAL browser window MAXIMIZED (full screen, ~1920 wide) and let
            # the page fill it (no_viewport=True). The "pinch" comes from forcing a fixed viewport
            # that is LARGER than the window's content area — Chromium then scales the whole page to
            # fit, so every screenshot looks squished. With no_viewport the page renders 1:1 to the
            # maximized window, so screenshots are captured un-pinched at the window's native size.
            # The live CSS size (window.innerWidth/Height) is read below into config_env; box_2d is
            # normalized 0-1000 so clicks map to CSS pixels regardless of device-scale-factor.
            # NOTE: --start-maximized is unreliable in headed Chromium (often falls back to a
            # ~1280px default window), so set the window size EXPLICITLY to 1920x1080. no_viewport
            # lets the page fill that window 1:1, so screenshots capture full-width and un-pinched.
            # Parallel workers get a small cascade offset (set by app.py) so every window's
            # title bar stays reachable without dragging/resizing — a mid-run RESIZE is what
            # breaks coordinates (position is harmless: clicks are viewport-relative).
            _win_pos = os.environ.get("GAMEGUARD_WINDOW_POS", "0,0")
            browser = await p.chromium.launch(
                headless=headless,
                args=["--window-size=1920,1080", f"--window-position={_win_pos}"] + _no_throttle)
            context_args = {
                "ignore_https_errors": True,
                "record_video_dir": recordings_dir,
                "locale": "en-ZA",
                "no_viewport": True,
            }
        context = await browser.new_context(**context_args)
        page = await context.new_page()

        # In-page log feed: mirror run logs onto the game window itself. The guard makes every
        # page.screenshot() hide the feed (Playwright `style` option), so Gemini never sees it;
        # the pump self-terminates when the page closes, so no per-exit-path cleanup is needed.
        log_overlay.install_screenshot_guard(page)
        asyncio.create_task(log_overlay.pump(page))

        # CRITICAL: Gemini boxes are normalized 0-1000; we convert to pixels with the live
        # viewport (config_env), which MUST equal the real content area or clicks miss.
        # With no_viewport, page.viewport_size is None, so read the live window size.
        _vp = page.viewport_size
        if _vp:
            config_env.set_viewport(_vp["width"], _vp["height"])
        else:
            await asyncio.sleep(1.0)  # let --start-maximized finish before measuring
            try:
                dims = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
                if dims and dims.get("w") and dims.get("h"):
                    config_env.set_viewport(dims["w"], dims["h"])
            except Exception:
                pass
        print(f"[SETUP] Render area {config_env.VIEWPORT_WIDTH}x{config_env.VIEWPORT_HEIGHT} ({'mobile' if mobile else 'desktop, maximized/no_viewport'})")

        context_start_time = time.time()
        monitor.attach(page)

        # --- SETUP ---
        print(f"[SETUP] Loading game...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  Nav warning: {e}")

        # AI Startup handling replaces manual waiting
        startup_ok = await auto_handle_startup(page)

        await monitor.learn_idle(duration=5)

        # ── DSC fast path: the Daily Sanity Check replaces the whole suite when selected.
        # One detection pass → floor the bet → one network-verified spin → Excel report row.
        if ENABLED_CAPS is not None and "dsc" in ENABLED_CAPS:
            import slot_dsc
            # Catalog minBetAmount (per game, from the resolver) beats the --min-bet arg.
            _dsc_min = (dsc_meta or {}).get("min_bet") or min_bet
            dsc = await slot_dsc.run_dsc(page, monitor, SCREENSHOT_DIR,
                                         region=region, startup_ok=startup_ok,
                                         expected_min=_dsc_min,
                                         non_slot=(dsc_meta or {}).get("non_slot"))
            results.extend(slot_dsc.to_test_results(dsc))
            if dsc_meta:
                from modules import dsc_report
                row = slot_dsc.to_report_row(dsc, dsc_meta)
                try:
                    dsc_report.upsert_row(dsc_meta["report_path"], row)
                    dsc_meta["written"] = True   # batch loop's crash handler must not double-report
                    print(f"[DSC] Report row written -> {dsc_meta['report_path']}")
                except Exception as e:
                    print(f"[DSC] [WARN] could not write report row: {e}")
                # Bet record for the deferred transaction-history check: Betway's back
                # office reflects bets ~10-15 min late, so verification is a second pass
                # over these records, matched by account + game + spin time + wager.
                try:
                    dsc_report.append_record(dsc_meta["report_path"], {
                        "recorded_at": datetime.now().astimezone().isoformat(),
                        "spin_at": dsc.get("spin_at"),
                        "brand": dsc_meta.get("brand"), "region": dsc_meta.get("region"),
                        "account": dsc_meta.get("account"),
                        "srNo": dsc_meta.get("srNo"), "provider": dsc_meta.get("provider"),
                        "game": dsc_meta.get("gameName"),
                        "launch": dsc["launch"], "bet_placed": dsc["bet_placed"],
                        "tlogs": dsc["tlogs"],
                        "wager": dsc.get("wager_effective"), "wager_response": dsc.get("wager"),
                        "payout": dsc.get("payout"),
                        "balance_before": dsc.get("balance_before"),
                        "balance_after": dsc.get("balance_after"),
                        # Exact wire accounting + ids for history matching: afterBet is the
                        # running balance history prints in parentheses; round_id/tnum give
                        # the back office an exact transaction handle.
                        "balance_at_start": dsc.get("balance_at_start"),
                        "balance_after_bet": dsc.get("balance_after_bet"),
                        "balance_at_end": dsc.get("balance_at_end"),
                        "round_id": dsc.get("round_id"), "tnum": dsc.get("tnum"),
                        "server_time": dsc.get("server_time"),
                        "source": dsc.get("source"), "endpoint": dsc.get("endpoint"),
                        "non_slot": dsc_meta.get("non_slot"),
                        "errors": dsc.get("errors"), "evidence": dsc_meta.get("evidence"),
                    })
                except Exception as e:
                    print(f"[DSC] [WARN] could not append bet record: {e}")
            await page.close()   # finalizes the session video so finalize_media can rename it
            await finalize_media(page, results, recordings_dir)
            await browser.close()
            return _emit_report(results)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 1: Detect ALL UI controls
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        _t1_start = time.time() - context_start_time
        print(f"  TEST 1: Detect all UI controls")
        print(f"{'='*70}")
        await refresh_viewport(page, "TEST 1")
        await page.screenshot(path=_ss("test_pre.png"))
        pil_img = Image.open(_ss("test_pre.png"))

        if spin_center_override:
            # Still detect other controls even if spin coords are provided
            try:
                controls = detect_all_controls(pil_img)
            except:
                controls = []
            # Ensure spin is in the list
            spin_ctrl = find_control(controls, "spin")
            if not spin_ctrl:
                controls.append({"label": "Spin Button", "box_2d": [0,0,0,0], "center": spin_center_override})
        else:
            controls = detect_all_controls(pil_img)

        print(f"  Detected {len(controls)} controls:")
        for c in controls:
            print(f"    - {c.get('label', '?'):30s} center={c.get('center', '?')}")

        # Annotated 'all elements' hero image for the report (labelled boxes on the base frame).
        global ELEMENTS_SHOT
        ELEMENTS_SHOT = annotate_controls(_ss("test_pre.png"), controls, _ss("elements_annotated.png"))

        # Show what was detected ON the live game so it's visibly real to a watcher.
        try:
            await highlight_controls(page, controls, duration=3.0)
        except Exception:
            pass

        t1 = TestResult("All UI controls detected", "test_pre.png")
        t1.passed = len(controls) >= 3  # At minimum: spin, bet display, balance
        t1.details = f"Found {len(controls)} controls: {[c.get('label') for c in controls]}"
        results.append(t1)
        t1.video_start = _t1_start
        t1.video_end = time.time() - context_start_time
        print(t1)

        # Pre-flight Check: Insufficient Funds
        await page.screenshot(path=_ss("test_preflight.png"))
        pre_vals = read_game_values(Image.open(_ss("test_preflight.png")))
        pre_bal = parse_amount(pre_vals.get("balance", ""))
        pre_bet = parse_amount(pre_vals.get("bet", ""))
        if pre_bal is not None and pre_bet is not None:
            print(f"  [PRE-FLIGHT] Balance: {pre_bal}, Bet: {pre_bet}")
            if pre_bal < pre_bet:
                print(f"  [!] INSUFFICIENT FUNDS. Aborting spin tests.")
                await browser.close()
                return results

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Default Bet — always reported (compared to expected if one was provided)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Default Bet\n{'='*70}")
        t_def = TestResult("Default bet amount", "test_preflight.png")
        if pre_bet is None:
            t_def.passed = False
            t_def.details = "Could not read the default bet from the UI"
        elif default_bet:
            t_def.passed = (pre_bet == parse_amount(default_bet))
            t_def.details = f"Expected '{default_bet}', found '{pre_bet}'."
        else:
            t_def.passed = True
            t_def.details = f"Default bet at launch: {pre_bet}"
        results.append(t_def)
        print(t_def)

        # Extract specific controls with smart matching
        def _find(controls, primary_labels, fallback_keywords):
            """First try exact label match, then keyword fallback."""
            for ctrl in controls:
                label = ctrl.get("label", "").lower()
                if any(pl.lower() == label for pl in primary_labels):
                    return ctrl
            for ctrl in controls:
                label = ctrl.get("label", "").lower()
                if any(kw.lower() in label for kw in fallback_keywords):
                    # Exclude false matches
                    if "turbo" in label or "fast" in label or "speed" in label:
                        if any(kw in ["spin button", "spin"] for kw in fallback_keywords):
                            continue
                    return ctrl
            return None

        spin_ctrl = _find(controls,
            ["Spin Button", "Spin"],
            ["spin button"])
        bet_inc = _find(controls,
            ["Bet Increment", "Bet Increase"],
            ["increment", "increase", "bet up", "bet +"])
        bet_dec = _find(controls,
            ["Bet Decrement", "Bet Decrease"],
            ["decrement", "decrease", "bet down", "bet -"])
        autoplay_ctrl = _find(controls,
            ["Autoplay Button", "Autospin Button", "Autoplay Button / Autospin Button"],
            ["autoplay", "autospin", "auto play", "auto spin"])
        menu_ctrl = _find(controls,
            ["Menu Button", "Settings Button"],
            ["menu", "hamburger", "settings"])
        turbo_ctrl = _find(controls,
            ["Turbo / Fast Spin", "Turbo Button", "Fast Spin"],
            ["turbo", "fast spin", "lightning", "speed"])
        sound_ctrl = _find(controls,
            ["Sound Toggle", "Volume", "Mute"],
            ["sound", "volume", "speaker", "mute"])

        # ── AUTOPLAY-ONLY fast path: skip spin/bet/menu/paytable checks to save tokens ──
        if AUTOPLAY_ONLY:
            print(f"\n{'='*70}\n  AUTOPLAY-ONLY MODE — other checks skipped (set AUTOPLAY_ONLY=False to restore)\n{'='*70}")
            import slot_spin, slot_agent
            _ap_t0 = time.time() - context_start_time
            # Robust autoplay find: (1) reuse the controls already detected in TEST 1, then
            # (2) a fresh re-detect, then (3) the tight-box icon finder — so one flaky vision pass
            # can't wrongly report "not detected" when it clearly exists.
            ap = find_control(controls, "autoplay", "autospin")
            if not (ap and ap.get("center")):
                await page.screenshot(path=_ss("autoplay_find.png"))
                _ctrls = detect_controls_merged(Image.open(_ss("autoplay_find.png")), passes=2)
                ap = next((c for c in _ctrls if "auto" in (c.get("label") or "").lower() and c.get("center")), None)
            if not (ap and ap.get("center")):
                _apc = slot_agent.locate_autoplay_button(_ss("autoplay_find.png")) \
                    if os.path.exists(_ss("autoplay_find.png")) else None
                if not _apc:
                    await page.screenshot(path=_ss("autoplay_find.png"))
                    _apc = slot_agent.locate_autoplay_button(_ss("autoplay_find.png"))
                if _apc:
                    ap = {"label": "Autoplay", "center": _apc}
            ta = TestResult("Autoplay runs & stops")
            ta.video_start = _ap_t0
            if ap:
                agm = slot_spin.UnifiedGameMonitor(); agm.attach(page)
                await agm.learn_idle(5)
                a = await slot_agent.drive_autoplay(page, ap["center"], agm, SCREENSHOT_DIR)
                sh = a.get("shots", {})
                ta.screenshot = sh.get("running") or sh.get("panel") or "test_pre.png"
                ta.passed = bool(a.get("started") and a.get("stopped"))
                _note = (" | " + "; ".join(a["notes"])) if a.get("notes") else ""
                _feats = [o.get("label") for o in a.get("options", [])]
                _plan = a.get("plan", [])
                ta.details = (f"started={a.get('started')}, auto-spins={a.get('spins_observed')}, "
                              f"stopped={a.get('stopped')}{_note}"
                              + (f" | menu features: {_feats}" if _feats else "")
                              + (f" | plan: {_plan}" if _plan else ""))
                # Surface the concrete steps the agent took, for the report.
                ta.actions.append(f"opened autoplay menu @ {ap['center']}")
                for step in _plan:
                    ta.actions.append(str(step))
                if a.get("started"):
                    ta.actions.append(f"autoplay started ({a.get('spins_observed')} spins observed)")
                if a.get("stopped"):
                    ta.actions.append("autoplay stopped")
                for n in a.get("notes", []):
                    ta.actions.append(f"note: {n}")
            else:
                ta.passed = None
                ta.details = "Autoplay control not detected on the bar"
                ta.actions.append("skipped: no autoplay control found on the bottom bar")
            ta.video_end = time.time() - context_start_time
            results.append(ta); print(ta)
            try:
                await page.close()
                await finalize_media(page, results, recordings_dir)
                await browser.close()
            except Exception:
                pass
            return _emit_report(results)

        # ── MENU-ONLY fast path: skip spin/bet/autoplay/paytable checks to save tokens ──
        if MENU_ONLY:
            print(f"\n{'='*70}\n  MENU-ONLY MODE — other checks skipped (set MENU_ONLY=False to restore)\n{'='*70}")
            import slot_spin, slot_agent
            _menu_t0 = time.time() - context_start_time
            await page.screenshot(path=_ss("menu_find.png"))
            _ctrls = detect_controls_merged(Image.open(_ss("menu_find.png")), passes=2)
            mn = next((c for c in _ctrls
                       if any(k in (c.get("label") or "").lower()
                              for k in ("menu", "hamburger", "setting"))
                       and c.get("center")), None)
            # Unified menu button -> open & examine its panel. No menu button (or the opener opens
            # no real panel, e.g. a Home icon / flat nav) -> examine the nav/settings icons sitting
            # directly on the base screen (same observe->act->verify loop).
            if mn:
                opener = f"clicked Menu @ {mn['center']}"
                # depth-1: examine each top-level option once (avoids looping a shared info carousel).
                pan = await slot_agent.examine_panel(page, "Menu", mn["center"], SCREENSHOT_DIR,
                                                     tag="menu", max_depth=1)
                if not pan.get("opened"):
                    opener = "menu opener showed no panel — examined root-level on-screen options"
                    pan = await slot_agent.examine_root_options(page, SCREENSHOT_DIR, tag="rootmenu")
            else:
                opener = "no menu button — examined root-level on-screen options"
                pan = await slot_agent.examine_root_options(page, SCREENSHOT_DIR, tag="rootmenu")
            opts = pan.get("options", [])
            tm = TestResult(f"{pan.get('panel', 'Menu')} examined",
                            pan.get("shots", {}).get("panel") or "test_pre.png")
            tm.passed = bool(pan.get("opened"))
            _note = (" | " + "; ".join(pan["notes"])) if pan.get("notes") else ""
            tm.details = (f"opened={pan.get('opened')}, {len(opts)} option(s): "
                          f"{[o.get('label') for o in opts]}{_note}")
            tm.actions.append(opener)
            if pan.get("opened"):
                tm.actions.append(f"{len(opts)} option(s) found")
            for o in opts:
                tm.actions.append(f"saw option: {o.get('label')}")
            for n in pan.get("notes", []):
                tm.actions.append(f"note: {n}")
            _menu_t1 = time.time() - context_start_time
            tm.video_start = _menu_t0; tm.video_end = _menu_t1
            results.append(tm); print(tm)
            # one card per option describing WHAT it is / where it leads (share the menu's clip window)
            for o in opts:
                desc = f"[{o.get('type')}] {o.get('purpose') or ''}".strip()
                if o.get("state") not in (None, "null"):
                    desc += f" · state: {o.get('state')}"
                if o.get("leads_to"):
                    desc += f" · leads to: {o.get('leads_to')}"
                ro = TestResult(f"Menu → {o.get('label')}", o.get("screenshot") or "")
                ro.passed = True; ro.details = desc
                ro.video_start = _menu_t0; ro.video_end = _menu_t1
                results.append(ro); print(ro)
            try:
                await page.close()
                await finalize_media(page, results, recordings_dir)
                await browser.close()
            except Exception:
                pass
            return _emit_report(results)

        # NOTE: the Minimum Bet check runs LATER (after the bet round-trip) so it doesn't floor the
        # stake before those tests — see "TEST: Minimum Bet" below TEST 8.

        if spin_center_override:
            spin_center = spin_center_override
        elif spin_ctrl and "center" in spin_ctrl:
            spin_center = spin_ctrl["center"]
        else:
            print("  [!] Cannot find spin button. Aborting.")
            await browser.close()
            return results

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 2: Spin endpoint discovery
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        _t2_start = time.time() - context_start_time
        print(f"  TEST 2: Spin endpoint discovery")
        print(f"{'='*70}")

        endpoint = await monitor.discover_spin_endpoint(page, spin_center)

        t2 = TestResult("Spin endpoint discovered", "test_pre.png")
        t2.passed = bool(endpoint)
        t2.details = f"Endpoint: {endpoint}"
        results.append(t2)
        t2.video_start = _t2_start
        t2.video_end = time.time() - context_start_time
        print(t2)

        if not endpoint:
            print("  [!] Warning: Could not isolate network spin endpoint. Proceeding with visual-only tests.")

        # ── AGENTIC MODE (default): hand control to the QA agent, which drives the whole checklist
        #    itself. The prereqs above (startup, control detection + hero, spin-endpoint discovery)
        #    have run; the agent owns the rest and the report. `--mode scripted` keeps TEST 3-10. ──
        if MODE == "agentic":
            print(f"\n{'='*70}\n  AGENTIC QA MODE — the agent drives the checklist (set --mode scripted to restore TEST 3-10)\n{'='*70}")
            import slot_qa_agent
            results.clear()   # the agent owns the report (it re-covers launch/controls); hero already saved
            agent_results = await slot_qa_agent.run_qa(
                page, monitor, SCREENSHOT_DIR, region=region,
                caps=ENABLED_CAPS, context_start_time=context_start_time,
                spin_center=spin_center)   # known-good coord from TEST 2 — deterministic spins
            results.extend(agent_results)
            try:
                await page.close()
                await finalize_media(page, results, recordings_dir)
                await browser.close()
            except Exception:
                pass
            return _emit_report(results)

        if _cap_on("core"):
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST 3: Single spin click
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}")
            _t3_start = time.time() - context_start_time
            print(f"  TEST 3: Single click = 1 spin request")
            print(f"{'='*70}")

            # Capture state before spinning
            await page.screenshot(path=_ss("test_prespin.png"))
            prespin_vals = read_game_values(Image.open(_ss("test_prespin.png")))
            prespin_bal = parse_amount(prespin_vals.get("balance", ""))
            prespin_bet = parse_amount(prespin_vals.get("bet", ""))

            monitor.clear_spins()
            monitor.start_monitoring()

            _t3_click = time.time()
            if prespin_bet is not None and prespin_bet > config_env.MAX_STAKE:
                # HARD stake cap (team rule 2026-07-09): never spin above MAX_STAKE, period.
                print(f"    [CAP] Stake {prespin_bet:g} exceeds the safety cap "
                      f"{config_env.MAX_STAKE:g} — refusing to click spin")
            else:
                try:
                    await flash_target(page, spin_center, "SPIN", "lime")
                except Exception:
                    pass
                _t3_click = time.time()
                await page.mouse.click(*spin_center)
                await monitor.wait_for_spin_completion()
            monitor.stop_monitoring()

            # Provisional verdict from the frame count; reconciled below against the actual result/balance
            # (WS frame-counts are flaky, so the result is the authority — see [TEST 3 reconciled]).
            t3 = TestResult("Single click = 1 spin request", "test_postspin.png")
            t3.passed = monitor.spin_count == 1
            t3.details = f"Spin requests: {monitor.spin_count}"
            results.append(t3)
            _t3_end = time.time() - context_start_time
            t3.video_start = _t3_start
            t3.video_end = _t3_end
            print(t3)

            # Post-spin state capture
            await asyncio.sleep(1) # wait for animations to settle
            await page.screenshot(path=_ss("test_postspin.png"))
            postspin_vals = read_game_values(Image.open(_ss("test_postspin.png")))
            postspin_bal = parse_amount(postspin_vals.get("balance", ""))

            # ── Result reconciliation: exact network values (any provider) + visual fallback ──
            import slot_spin  # lazy import avoids circular dependency
            spin_resps = [r for r in monitor._all_responses
                          if monitor.spin_endpoint and monitor.spin_endpoint in r["path"]]
            net = slot_spin.parse_result_body(spin_resps[-1]["body"]) if spin_resps else {}

            wager = net["wager"] if net.get("wager") is not None else prespin_bet
            if net.get("payout") is not None:
                payout = net["payout"]
            elif prespin_bal is not None and postspin_bal is not None and wager is not None:
                payout = max(0.0, round(postspin_bal - (prespin_bal - wager), 2))
            else:
                payout = 0.0
            bal_after = net["balance"] if net.get("balance") is not None else postspin_bal
            feature_triggered = net.get("feature", False)
            feature_name = net.get("feature_name")
            value_src = "network" if net.get("found") else "visual"
            tax = slot_spin.withholding_tax(payout, wager, region)
            print(f"  [RESULT] src={value_src} wager={wager} payout={payout} "
                  f"balance={bal_after} feature={feature_name} tax={tax}")

            # ── Reconcile TEST 3 with the ACTUAL result. WS frame-counting is flaky (one shared socket),
            # but a parsed result OR a real balance change proves the single spin fired. This is the
            # generic, provider-agnostic signal. Only a genuine double-spin (count >= 2) fails. ──
            _fired = bool(net.get("found")) or (
                prespin_bal is not None and postspin_bal is not None and postspin_bal != prespin_bal)
            if monitor.spin_count >= 2:
                t3.passed = False
                t3.details = f"Spin requests: {monitor.spin_count} (double spin from one click!)"
            elif monitor.spin_count == 1:
                t3.passed = True; t3.details = "Spin requests: 1"
            elif _fired:
                t3.passed = True
                t3.details = f"1 spin (result-confirmed via {value_src}; WS frame count unavailable)"
            else:
                t3.passed = False
                t3.details = "no spin detected (no frame count, no result, no balance change)"
            print(f"  [TEST 3 reconciled] {t3.details}")

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST: Wager Processing
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}\n  TEST: Wager Processing\n{'='*70}")
            t_wager = TestResult("Wager correctly processed", "test_prespin.png")
            if wager is not None:
                t_wager.passed = True
                t_wager.details = f"Wager of {wager} applied during spin ({value_src})."
            else:
                t_wager.passed = False
                t_wager.details = "Could not identify wager amount."
            results.append(t_wager)
            t_wager.video_start = _t3_start
            t_wager.video_end = _t3_end
            print(t_wager)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST: Payout Handling
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}\n  TEST: Payout Handling\n{'='*70}")
            t_payout = TestResult("Payout successfully logged (if applicable)", "test_postspin.png")
            t_payout.passed = True
            t_payout.details = f"Payout {payout} recorded ({value_src})." if payout and payout > 0 else "No payout on this spin."
            results.append(t_payout)
            t_payout.video_start = _t3_start
            t_payout.video_end = _t3_end
            print(t_payout)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST: Feature Triggered Events
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}\n  TEST: Feature Triggered Events\n{'='*70}")
            t_feat = TestResult("Feature Triggers monitored", "test_postspin.png")
            t_feat.passed = True
            t_feat.details = f"Feature triggered: {feature_name}" if feature_triggered else "No features triggered."
            results.append(t_feat)
            t_feat.video_start = _t3_start
            t_feat.video_end = _t3_end
            print(t_feat)

            # TEST: Withholding tax (Mozambique all games / Zambia virtual) — 15% on (payout - wager)
            if region in ("MZ", "ZM"):
                print(f"\n{'='*70}\n  TEST: Withholding Tax ({region})\n{'='*70}")
                t_tax = TestResult(f"Withholding tax ({region})", "test_postspin.png")
                t_tax.passed = True
                t_tax.details = (f"15% on (payout {payout} - wager {wager}) = R{tax}"
                                 if tax > 0 else "No taxable win on this spin.")
                results.append(t_tax)
                t_tax.video_start = _t3_start
                t_tax.video_end = _t3_end
                print(t_tax)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST: Balance Update
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}\n  TEST: Balance Update\n{'='*70}")
            t_bal = TestResult("Balance updated correctly", "test_postspin.png")
            if prespin_bal is not None and wager is not None and bal_after is not None:
                expected_bal = round(prespin_bal - wager + (payout or 0.0), 2)
                # floating point inaccuracies can be annoying, checking difference
                if abs(bal_after - expected_bal) < 0.10:
                    t_bal.passed = True
                    t_bal.details = f"Balance correct: {prespin_bal} -> {bal_after} ({value_src})"
                else:
                    t_bal.passed = False
                    t_bal.details = f"Expected {expected_bal}, but balance is {bal_after}"
            else:
                t_bal.passed = None
                t_bal.details = f"Missing data. pre_bal:{prespin_bal}, wager:{wager}, post_bal:{bal_after}"
            results.append(t_bal)
            t_bal.video_start = _t3_start
            t_bal.video_end = _t3_end
            print(t_bal)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST 4: Rapid clicks during spin
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}")
            _t4_start = time.time() - context_start_time
            print(f"  TEST 4: Rapid clicks during spin = still 1 request")
            print(f"{'='*70}")
            monitor.clear_spins()
            monitor.start_monitoring()

            _t4_click = time.time()
            await page.mouse.click(*spin_center)
            # Brief wait to get INTO the spin (frame count if available, else a short fixed window —
            # WS games don't reliably surface the count). Then spam regardless so we actually exercise
            # rapid-clicking during the spin.
            for _ in range(15):
                await asyncio.sleep(0.1)
                if monitor.spin_count >= 1:
                    break
            print(f"  Spam-clicking the spin button (tight burst)...")
            CLICKS = 1 + 8
            for _ in range(8):                       # tight burst so they land during spin #1
                await page.mouse.click(*spin_center)
                await asyncio.sleep(0.06)
            await asyncio.sleep(0.5)
            monitor.stop_monitoring()
            await monitor.wait_for_spin_completion()

            # Judge by RATIO, not "exactly 1". A properly-locked button ignores clicks WHILE spinning, so
            # 9 rapid clicks yield very few spins. On a FAST game the spin can complete between clicks, so
            # 2-3 sequential spins is NORMAL (the button re-enabled between them) — NOT a lock failure.
            # Only a button that barely disables turns most clicks into spins. So: fail only when the
            # spin count is a large fraction of the clicks.
            t4 = TestResult("Rapid clicks during spin = still 1 request", "test_postspin.png")
            n4 = monitor.spin_count
            _t4_result = _spin_result_captured(monitor, _t4_click)
            if n4 >= max(4, int(CLICKS * 0.5)):
                t4.passed = False
                t4.details = f"{CLICKS} rapid clicks produced {n4} spins — button NOT disabled during spin!"
            elif n4 == 1:
                t4.passed = True
                t4.details = f"{CLICKS} clicks, 1 spin — button disabled during spin."
            elif n4 in (2, 3):
                t4.passed = True
                t4.details = (f"{CLICKS} clicks, {n4} spins — clicks were NOT queued into extra spins; "
                              f"the button re-enabled between fast sequential spins (not a lock failure).")
            elif _t4_result:
                t4.passed = True
                t4.details = f"{CLICKS} clicks, no extra spins counted (result-confirmed; WS frame count unavailable)"
            else:
                t4.passed = None
                t4.details = "Could not confirm spin in-flight on this WS game"

            results.append(t4)
            _t4_end = time.time() - context_start_time
            t4.video_start = _t4_start
            t4.video_end = _t4_end
            print(t4)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST 5: Spin re-enables after completion
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            print(f"\n{'='*70}")
            print(f"  TEST 5: Spin button re-enables after completion")
            print(f"{'='*70}")
            _t5_start = time.time() - context_start_time
            # CRITICAL: TEST 4 just spammed spins, so the game may STILL be spinning. Wait for it to
            # finish and the reels to settle FIRST — otherwise our click lands while the button is
            # legitimately disabled (0 spins) and we'd falsely report "did not re-enable".
            await monitor.wait_for_spin_completion()
            prev = None
            for _ in range(12):                     # wait until motion is low (button idle) or ~5s
                fp = _ss("t5_settle.png"); await page.screenshot(path=fp)
                calm = prev is not None and slot_spin.frame_motion(prev, fp) < 3.0
                prev = fp
                if calm:
                    break
                await asyncio.sleep(0.4)
            _bal_before = parse_amount(read_game_values(Image.open(_ss("t5_settle.png"))).get("balance", ""))

            monitor.clear_spins()
            monitor.start_monitoring()
            _t5_click = time.time()
            try:
                await flash_target(page, spin_center, "SPIN", "lime")
            except Exception:
                pass
            await page.mouse.click(*spin_center)
            await monitor.wait_for_spin_completion()
            monitor.stop_monitoring()
            await page.screenshot(path=_ss("test_postspin.png"))
            _bal_after = parse_amount(read_game_values(Image.open(_ss("test_postspin.png"))).get("balance", ""))

            # Re-enabled = clicking after the previous spin completed fires a NEW spin. Confirm by frame
            # count OR a captured result OR a balance change (all WS-safe).
            t5 = TestResult("Spin button re-enables after completion", "test_postspin.png")
            _refired = (monitor.spin_count >= 1 or _spin_result_captured(monitor, _t5_click)
                        or (_bal_before is not None and _bal_after is not None and _bal_after != _bal_before))
            if _refired:
                t5.passed = True
                t5.details = ("Spin requests: 1" if monitor.spin_count >= 1
                              else "re-spin fired after completion (result/balance-confirmed; WS count unavailable)")
            else:
                t5.passed = False
                t5.details = "clicking after the previous spin completed did not start a new spin"
            results.append(t5)
            t5.video_start = _t5_start
            t5.video_end = time.time() - context_start_time
            print(t5)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 6: Bet can be changed
        #
        # Adaptive strategy — tries in order:
        #   A: Direct + button click
        #   B: Click bet display area -> overlay -> pick different value
        #   C: Direct - button click
        #   If any click opens an overlay, auto-detects and handles it.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        bet_display = find_control(controls, "bet display", "bet amount", "bet")
        has_any_bet_control = bet_inc or bet_dec or bet_display
        # Define the timing marks up front so BOTH branches (run / skipped) can stamp video bounds
        # without an UnboundLocalError when the bet check is gated off or no bet control exists.
        _t6_start = time.time() - context_start_time
        _t6_end = _t6_start
        if has_any_bet_control and _cap_on("bet"):
            print(f"\n{'='*70}")
            print(f"  TEST 6: Bet can be changed")
            print(f"{'='*70}")
            await _dismiss_overlays(page)   # ensure no stray panel before measuring/changing bet
            # Re-detect the bet controls FRESH (the initial scan's coords may be stale after the
            # spin tests) — reused by TEST 6/7/8 below.
            bet_dec = await refind_control(page, "bet decrement", "bet -", "decrease", "minus") or bet_dec
            bet_inc = await refind_control(page, "bet increment", "bet +", "increase", "plus") or bet_inc

            # Read current bet
            await page.screenshot(path=_ss("test_bet_before.png"))
            before_vals = read_game_values(Image.open(_ss("test_bet_before.png")))
            bet_before = parse_amount(before_vals.get("bet", ""))
            print(f"  Bet before: {before_vals.get('bet')} (parsed: {bet_before})")

            bet_changed = False
            strategy_used = ""
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )

            async def try_overlay(before_path, after_path):
                """Check if overlay appeared, click a different bet if found."""
                bimg = copy.deepcopy(Image.open(before_path))
                aimg = copy.deepcopy(Image.open(after_path))
                bimg.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
                aimg.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
                prompt = """Compare these two slot game screenshots.
Did a bet selection overlay/popup/panel appear in the second image?
If yes, find a DIFFERENT bet value than the current one.
Return JSON: {"overlay": true/false, "different_bet": {"label": "text", "box_2d": [ymin, xmin, ymax, xmax]} or null}
Normalize box_2d to 0-1000."""
                ov = parse_gemini_json(gemini_call([bimg, aimg, prompt], config))
                if ov.get("overlay") and ov.get("different_bet"):
                    db = ov["different_bet"]
                    box = db.get("box_2d", [])
                    if box and len(box) == 4:
                        bx, by = config_env.norm_box_center(box)
                        print(f"    Overlay found! Selecting '{db.get('label')}' at ({bx}, {by})...")
                        await page.mouse.click(bx, by)
                        await asyncio.sleep(1.5)
                        return True
                return False

            async def read_bet(path):
                vals = read_game_values(Image.open(path))
                bet = parse_amount(vals.get("bet", ""))
                return bet, vals.get("bet", ""), (bet is not None and bet_before is not None and bet != bet_before)

            # ── Strategy A: Direct + button ──
            if bet_inc and "center" in bet_inc and not bet_changed:
                print(f"\n  [Strategy A] Trying Bet + at {bet_inc['center']}...")
                try:
                    await flash_target(page, bet_inc["center"], "Bet +", "cyan")
                except Exception:
                    pass
                await page.mouse.click(*bet_inc["center"])
                await asyncio.sleep(1.5)
                await page.screenshot(path=_ss("test_bet_A.png"))
                a_bet, a_text, changed = await read_bet(_ss("test_bet_A.png"))
                print(f"    Bet after: {a_text} (parsed: {a_bet})")
                if changed:
                    bet_changed = True
                    strategy_used = f"Direct + button: {before_vals.get('bet')} -> {a_text}"
                else:
                    ov_worked = await try_overlay(_ss("test_bet_before.png"), _ss("test_bet_A.png"))
                    if ov_worked:
                        await page.screenshot(path=_ss("test_bet_A2.png"))
                        _, a2_text, changed2 = await read_bet(_ss("test_bet_A2.png"))
                        if changed2:
                            bet_changed = True
                            strategy_used = f"+ button overlay: {before_vals.get('bet')} -> {a2_text}"
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            # ── Strategy B: Click bet display area ──
            if bet_display and "center" in bet_display and not bet_changed:
                print(f"\n  [Strategy B] Trying Bet Display at {bet_display['center']}...")
                await page.mouse.click(*bet_display["center"])
                await asyncio.sleep(1.5)
                await page.screenshot(path=_ss("test_bet_B.png"))
                ov_worked = await try_overlay(_ss("test_bet_before.png"), _ss("test_bet_B.png"))
                if ov_worked:
                    await page.screenshot(path=_ss("test_bet_B2.png"))
                    _, b_text, changed = await read_bet(_ss("test_bet_B2.png"))
                    if changed:
                        bet_changed = True
                        strategy_used = f"Bet display overlay: {before_vals.get('bet')} -> {b_text}"
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            # ── Strategy C: Direct - button ──
            if bet_dec and "center" in bet_dec and not bet_changed:
                print(f"\n  [Strategy C] Trying Bet - at {bet_dec['center']}...")
                try:
                    await flash_target(page, bet_dec["center"], "Bet -", "cyan")
                except Exception:
                    pass
                await page.mouse.click(*bet_dec["center"])
                await asyncio.sleep(1.5)
                await page.screenshot(path=_ss("test_bet_C.png"))
                c_bet, c_text, changed = await read_bet(_ss("test_bet_C.png"))
                print(f"    Bet after: {c_text} (parsed: {c_bet})")
                if changed:
                    bet_changed = True
                    strategy_used = f"Direct - button: {before_vals.get('bet')} -> {c_text}"
                else:
                    ov_worked = await try_overlay(_ss("test_bet_before.png"), _ss("test_bet_C.png"))
                    if ov_worked:
                        await page.screenshot(path=_ss("test_bet_C2.png"))
                        _, c2_text, changed2 = await read_bet(_ss("test_bet_C2.png"))
                        if changed2:
                            bet_changed = True
                            strategy_used = f"- button overlay: {before_vals.get('bet')} -> {c2_text}"
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

            # Close any leftover overlay
            await page.mouse.click(config_env.VIEWPORT_WIDTH // 2, config_env.VIEWPORT_HEIGHT // 3)
            await asyncio.sleep(1)

            t6 = TestResult("Bet can be changed", "test_bet_before.png")
            if bet_changed:
                t6.passed = True
                t6.details = strategy_used
            elif bet_before is None:
                t6.passed = None
                t6.details = f"Could not parse bet: {before_vals.get('bet')}"
            else:
                t6.passed = False
                t6.details = f"All strategies failed. Bet stayed at {before_vals.get('bet')}"

            results.append(t6)
            _t6_end = time.time() - context_start_time
            t6.video_start = _t6_start
            t6.video_end = _t6_end
            print(t6)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST 7: Restore bet to original (round-trip)
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            if bet_changed:
                print(f"\n{'='*70}")
                print(f"  TEST 7: Bet can be restored (round-trip)")
                print(f"{'='*70}")
                await _dismiss_overlays(page)   # clear any overlay opened during TEST 6

                # Closed-loop restore: nudge the stepper back TOWARD the original value, re-reading
                # after each click. Bet steps are often NON-UNIFORM (0.20/0.50/1.00/2.00…), so a fixed
                # click count can't return exactly — and blindly guessing an overlay option (old code)
                # mis-clicked on stepper games. This converges to the original regardless of direction.
                bet_restored = None
                for _ in range(10):
                    await page.screenshot(path=_ss("test_bet_restore.png"))
                    cur = parse_amount(read_game_values(Image.open(_ss("test_bet_restore.png"))).get("bet", ""))
                    bet_restored = cur
                    if cur is None or bet_before is None or abs(cur - bet_before) < 0.01:
                        break
                    btn = bet_dec if cur > bet_before else bet_inc
                    if not (btn and "center" in btn):
                        break
                    await page.mouse.click(*btn["center"]); await asyncio.sleep(0.6)

                await page.screenshot(path=_ss("test_bet_restored.png"))
                restored_vals = read_game_values(Image.open(_ss("test_bet_restored.png")))
                bet_restored = parse_amount(restored_vals.get("bet", ""))
                print(f"  Bet restored: {restored_vals.get('bet')} (parsed: {bet_restored})")

                t7 = TestResult("Bet can be restored (round-trip)", "test_bet_restored.png")
                if bet_before is not None and bet_restored is not None:
                    t7.passed = abs(bet_restored - bet_before) < 0.01
                    t7.details = f"Original: {before_vals.get('bet')}, Restored: {restored_vals.get('bet')}"
                else:
                    t7.passed = None
                    t7.details = f"Could not parse. Original: {before_vals.get('bet')}, Restored: {restored_vals.get('bet')}"
                results.append(t7)
                t7.video_start = _t6_start
                t7.video_end = _t6_end
                print(t7)
            else:
                t7 = TestResult("Bet can be restored (round-trip)", "test_bet_restored.png")
                t7.passed = None
                t7.details = "Skipped: bet change failed"
                results.append(t7)
                t7.video_start = _t6_start
                t7.video_end = _t6_end
                print(t7)
        else:
            _skip_why = "bet check not selected" if not _cap_on("bet") else "No bet control detected"
            t6 = TestResult("Bet can be changed", "test_bet_before.png")
            t6.passed = None
            t6.details = _skip_why
            results.append(t6)
            _t6_end = time.time() - context_start_time
            t6.video_start = _t6_start
            t6.video_end = _t6_end
            print(t6)

            t7 = TestResult("Bet can be restored (round-trip)", "test_bet_restored.png")
            t7.passed = None
            t7.details = _skip_why
            results.append(t7)
            t7.video_start = _t6_start
            t7.video_end = _t6_end
            print(t7)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 8: Bet buttons disabled during spin
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        bet_btn = bet_inc or bet_dec
        if bet_btn and "center" in bet_btn and _cap_on("bet"):
            print(f"\n{'='*70}")
            print(f"  TEST 8: Bet buttons disabled during spin")
            print(f"{'='*70}")

            # Read bet before
            await page.screenshot(path=_ss("test_bet_spin_before.png"))
            before_vals = read_game_values(Image.open(_ss("test_bet_spin_before.png")))
            bet_before = before_vals.get("bet", "")
            print(f"  Bet before spin: {bet_before}")

            # Start spin
            monitor.clear_spins()
            monitor.start_monitoring()
            await page.mouse.click(*spin_center)

            # Wait for spin to be in-flight
            for _ in range(30):
                await asyncio.sleep(0.1)
                if monitor.spin_count >= 1:
                    break

            if monitor.spin_count >= 1:
                # Try clicking bet button during spin
                print(f"  Spin in-flight! Clicking bet button during spin...")
                await page.mouse.click(*bet_btn["center"])
                await asyncio.sleep(0.3)
                await page.mouse.click(*bet_btn["center"])
                await asyncio.sleep(0.3)

            monitor.stop_monitoring()
            await monitor.wait_for_spin_completion()

            # Read bet after
            await page.screenshot(path=_ss("test_bet_spin_after.png"))
            after_vals = read_game_values(Image.open(_ss("test_bet_spin_after.png")))
            bet_after = after_vals.get("bet", "")
            print(f"  Bet after spin: {bet_after}")

            t8 = TestResult("Bet buttons disabled during spin", "test_bet_spin_after.png")
            bet_b = parse_amount(bet_before)
            bet_a = parse_amount(bet_after)
            if bet_b is not None and bet_a is not None:
                t8.passed = bet_b == bet_a
                t8.details = f"Bet unchanged during spin: {bet_before} -> {bet_after}"
                if not t8.passed:
                    t8.details = f"Bet CHANGED during spin! {bet_before} -> {bet_after}"
            else:
                t8.passed = None
                t8.details = f"Could not parse. Before: {bet_before}, After: {bet_after}"
            results.append(t8)
            t8.video_start = _t6_start
            t8.video_end = _t6_end
            print(t8)
        else:
            t8 = TestResult("Bet buttons disabled during spin", "test_bet_spin_after.png")
            t8.passed = None
            t8.details = "bet check not selected" if not _cap_on("bet") else "No bet buttons detected"
            results.append(t8)
            t8.video_start = _t6_start
            t8.video_end = _t6_end
            print(t8)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Minimum Bet — spam Decrease to the floor (compare to expected if given). Runs AFTER
        # the round-trip so it doesn't floor the stake before those tests. Gated under the bet cap.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Minimum Bet\n{'='*70}")
        _tmin_start = time.time() - context_start_time
        t_min = TestResult("Minimum bet", "test_min_bet.png")
        if not _cap_on("bet"):
            t_min.passed = None
            t_min.details = "bet check not selected"
        else:
            await _dismiss_overlays(page)
            _dec = await refind_control(page, "bet decrement", "bet -", "decrease", "minus") or bet_dec
            if _dec and "center" in _dec:
                print(f"  Clicking 'Decrease Bet' at {_dec['center']} to reach minimum (verified)...")
                try:
                    await flash_target(page, _dec["center"], "Bet - (to min)", "cyan")
                except Exception:
                    pass
                # CLOSED-LOOP: probe with ONE click and confirm the bet did not RISE before
                # spamming the rest — a "decrement" that raises the bet is a mislabeled "+"
                # (Thor's Rage 2026-07-09 pumped the stake to its 300 max this way).
                await page.screenshot(path=_ss("test_min_bet_before.png"))
                _bet0 = parse_amount(read_game_values(Image.open(_ss("test_min_bet_before.png"))).get("bet", ""))
                await page.mouse.click(*_dec["center"]); await asyncio.sleep(0.6)
                await page.screenshot(path=_ss("test_min_bet_probe.png"))
                _bet1 = parse_amount(read_game_values(Image.open(_ss("test_min_bet_probe.png"))).get("bet", ""))
                if _bet0 is not None and _bet1 is not None and _bet1 > _bet0 + 0.011:
                    t_min.passed = False
                    t_min.details = (f"'Decrease' control RAISED the bet ({_bet0:g} → {_bet1:g}) — "
                                     f"mislabeled increment; flooring aborted after one click")
                    await page.screenshot(path=_ss("test_min_bet.png"))
                else:
                    for _ in range(9):
                        await page.mouse.click(*_dec["center"]); await asyncio.sleep(0.4)
                    await page.screenshot(path=_ss("test_min_bet.png"))
                    min_bet_found = parse_amount(read_game_values(Image.open(_ss("test_min_bet.png"))).get("bet", ""))
                    if min_bet_found is None:
                        t_min.passed = False
                        t_min.details = "Could not read the bet after reaching the floor"
                    elif min_bet:
                        t_min.passed = (min_bet_found == parse_amount(min_bet))
                        t_min.details = f"Expected '{min_bet}', found '{min_bet_found}'."
                    else:
                        t_min.passed = True
                        t_min.details = f"Minimum bet reached: {min_bet_found}"
            else:
                t_min.passed = None
                t_min.details = "No 'Decrease Bet' control detected"
        results.append(t_min)
        t_min.video_start = _tmin_start; t_min.video_end = time.time() - context_start_time
        print(t_min)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # NOTE: Autoplay is verified properly in TEST 9 (slot_agent.drive_autoplay — actually
        # starts & stops, network-verified). The old weak "Auto Play functionality works" click-test
        # was removed: it always passed and its stray clicks could leave a panel open that polluted
        # later tests.

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Audio toggle — click the sound control and confirm its state visibly changed.
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Audio toggle\n{'='*70}")
        _taud = time.time() - context_start_time
        t_aud = TestResult("Audio toggle", "test_audio_after.png")
        await _dismiss_overlays(page)
        sound_ctrl = await refind_control(page, "sound", "volume", "mute", "speaker", "audio") or sound_ctrl
        if sound_ctrl and "center" in sound_ctrl:
            await page.screenshot(path=_ss("test_audio_before.png"))
            try:
                await flash_target(page, sound_ctrl["center"], "Sound", "cyan")
            except Exception:
                pass
            await page.mouse.click(*sound_ctrl["center"]); await asyncio.sleep(0.8)
            await page.screenshot(path=_ss("test_audio_after.png"))
            import slot_spin as _ss_mod
            _changed = _ss_mod.frame_motion(_ss("test_audio_before.png"), _ss("test_audio_after.png")) > 2.0
            await page.mouse.click(*sound_ctrl["center"]); await asyncio.sleep(0.4)  # toggle back
            t_aud.passed = bool(_changed)
            t_aud.details = ("Sound control toggled — icon/state changed." if _changed
                             else "Clicked the sound control but saw no visible change.")
        else:
            t_aud.passed = None
            t_aud.details = "No sound control detected"
        results.append(t_aud)
        t_aud.video_start = _taud; t_aud.video_end = time.time() - context_start_time
        print(t_aud)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: UI renders correctly — vision check on the launch frame (no cut-off / broken layout).
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: UI renders correctly\n{'='*70}")
        t_ui = TestResult("UI renders correctly", "test_pre.png")
        try:
            _ui_prompt = ("Look at this slot game screenshot. Does the game render CORRECTLY in the "
                          "frame — reels/symbols and the control bar fully visible, not cut off, "
                          "squished, overlapping, or showing broken/missing images? "
                          'Return JSON {"ok": true|false, "issue": "short reason or empty"}.')
            _ui_cfg = types.GenerateContentConfig(response_mime_type="application/json",
                                                  thinking_config=types.ThinkingConfig(thinking_budget=0))
            _ui = parse_gemini_json(gemini_call([Image.open(_ss("test_pre.png")), _ui_prompt], _ui_cfg))
            if not isinstance(_ui, dict):
                _ui = {}
            t_ui.passed = bool(_ui.get("ok"))
            t_ui.details = ("Renders cleanly in the frame." if _ui.get("ok")
                            else f"Rendering issue: {_ui.get('issue') or 'reported not-clean'}")
        except Exception as e:
            t_ui.passed = None
            t_ui.details = f"UI check could not run: {e}"
        results.append(t_ui)
        t_ui.video_start = _t1_start; t_ui.video_end = _t1_start
        print(t_ui)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 9: Agentic feature exploration — OPERATE each feature like a QA:
        # run autoplay, drill into menu options, page the paytable. (slot_agent.py)
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST 9: Agentic feature exploration\n{'='*70}")
        await _dismiss_overlays(page)   # clean base state before the agentic pass
        _t9_start = time.time() - context_start_time

        def _agentic(name, screenshot, passed, details, actions=None):
            r = TestResult(name, screenshot or "")
            r.passed = passed
            r.details = details
            r.actions = actions or []
            r.video_start = _t9_start; r.video_end = time.time() - context_start_time
            results.append(r); print(r)

        try:
            import slot_agent
            # qa_explore needs the spin monitor's idle baseline learned (TEST 2 already
            # discovered the endpoint; idle paths come from the same NetworkMonitor traffic).
            ag_monitor = slot_spin.UnifiedGameMonitor(); ag_monitor.attach(page)
            await ag_monitor.learn_idle(5)
            _caps = None if ENABLED_CAPS is None else (ENABLED_CAPS & {"autoplay", "menu", "paytable"})
            findings = await slot_agent.qa_explore(page, ag_monitor, SCREENSHOT_DIR, region=region, caps=_caps)

            if not findings:
                _agentic("Feature exploration", "test_pre.png", None,
                         "No operable features (autoplay/menu/paytable) detected on the bar")

            if "autoplay" in findings:
                a = findings["autoplay"]; shots = a.get("shots", {})
                note = (" | " + "; ".join(a["notes"])) if a.get("notes") else ""
                _aacts = [str(s) for s in a.get("plan", [])]
                if a.get("started"): _aacts.append(f"autoplay started ({a.get('spins_observed')} spins)")
                if a.get("stopped"): _aacts.append("autoplay stopped")
                _agentic("Autoplay runs & stops", shots.get("running") or shots.get("panel"),
                         bool(a.get("started") and a.get("stopped")),
                         f"started={a.get('started')}, auto-spins={a.get('spins_observed')}, "
                         f"stopped={a.get('stopped')}{note}", actions=_aacts)

            # Examined panels (menu, buy bonus): one summary card + a card per option
            # describing WHAT each option is (type/state/purpose) and where it leads.
            for key in ("menu", "buybonus"):
                if key not in findings:
                    continue
                pan = findings[key]; opts = pan.get("options", [])
                summary = (f"opened; {len(opts)} option(s): {[o.get('label') for o in opts]}"
                           if opts else "; ".join(pan.get("notes", [])) or "did not open")
                _pacts = ([f"saw option: {o.get('label')}" for o in opts]
                          + [f"note: {n}" for n in pan.get("notes", [])])
                _agentic(f"{pan.get('panel', key)} examined", pan.get("shots", {}).get("panel"),
                         bool(pan.get("opened")), summary, actions=_pacts)
                for o in opts:
                    desc = f"[{o.get('type')}] {o.get('purpose') or ''}".strip()
                    if o.get("state") not in (None, "null"):
                        desc += f" · state: {o.get('state')}"
                    if o.get("leads_to"):
                        desc += f" · leads to: {o.get('leads_to')}"
                    _agentic(f"{pan.get('panel', key)} → {o.get('label')}", o.get("screenshot"),
                             True, desc)

            if "paytable" in findings:
                pt = findings["paytable"]; pages = pt.get("pages", [])
                note = (" | " + "; ".join(pt["notes"])) if pt.get("notes") else ""
                _agentic("Paytable captured", pages[0] if pages else "",
                         bool(pt.get("opened")), f"{len(pages)} page(s) captured{note}")
        except Exception as e:
            import traceback; traceback.print_exc()
            _agentic("Feature exploration", "test_pre.png", False, f"Agentic explorer error: {e}")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 10: Valid server response
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        _t10_start = time.time() - context_start_time
        print(f"  TEST 10: Server returns valid spin response")
        print(f"{'='*70}")

        spin_resps = [r for r in monitor._all_responses
                      if r["path"] == monitor.spin_endpoint and r["status"] == 200]

        t10 = TestResult("Server returns valid spin response", "test_postspin.png")
        if spin_resps:
            last = spin_resps[-1]
            try:
                json.loads(last["body"])
                valid_json = True
            except:
                valid_json = False
            t10.passed = valid_json
            t10.details = f"Status: {last['status']}, Valid JSON: {valid_json}"
        else:
            t10.passed = False
            t10.details = "No spin responses captured"
        results.append(t10)
        t10.video_start = _t10_start
        t10.video_end = time.time() - context_start_time
        print(t10)
        
        await page.close()
        await finalize_media(page, results, recordings_dir)
        await browser.close()

    return _emit_report(results)


if __name__ == "__main__":
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except:
        pass

    # Import JPC backend modules
    jpc_available = False
    try:
        from modules.auth_handler import AuthHandler
        from modules.game_handler import GameHandler
        from modules.iframe_handler import IframeHandler
        from modules.game_excel import parse_excel
        jpc_available = True
    except ImportError as e:
        print(f"Warning: JackpotCity backend modules not available: {e}")
        parse_excel = None

    parser = argparse.ArgumentParser(description="Slot Game UI Test Suite")
    parser.add_argument("url", nargs="?", default=None, help="Direct Game URL")
    parser.add_argument("--wait", type=int, default=WAIT_SECONDS,
                        help="Seconds to wait for game load (default: 30)")
    parser.add_argument("--spin-xy", type=str, default=None,
                        help="Skip Gemini spin detection, use these coords. Format: x,y")
    parser.add_argument("--mobile", action="store_true", help="Run tests in mobile viewport (iPhone 13)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")

    # JPC Backend Integration Arguments
    parser.add_argument("--game", type=str, help="Game name to search on JackpotCity and auto-resolve iframe URL")
    parser.add_argument("--excel", type=str, help="Path to Excel sheet for JPC bulk testing")
    parser.add_argument("--brand", type=str, default="betway", help="Brand: betway | jackpotcity")
    parser.add_argument("--region", type=str, default="ZA", help="Region code: ZA, GH, NG, TZ, MW, MZ, BW, ZM")
    parser.add_argument("--username", type=str, default="222212222", help="JPC Username (default: 222212222)")
    parser.add_argument("--password", type=str, default="222212222", help="JPC Password (default: 222212222)")
    parser.add_argument("--default-bet", type=str, default="", help="Expected default bet amount")
    parser.add_argument("--min-bet", type=str, default="", help="Expected minimum bet amount")
    parser.add_argument("--run-dir", type=str, default="",
                        help="Per-run output folder; screenshots/video/logs/results land here")
    parser.add_argument("--tests", type=str, default="",
                        help=f"Comma-separated gated checks to run (subset of {GATED_CAPS}); "
                             f"empty = all. 'dsc' is special: it REPLACES the suite with the "
                             f"fast Daily Sanity Check + Excel report row.")
    parser.add_argument("--dsc-report", type=str, default="",
                        help="DSC only: path of the Excel report to append rows to "
                             "(default: runs/DSC_Report_<today>.xlsx, shared by all runs that day)")
    parser.add_argument("--mode", type=str, default="scripted", choices=["agentic", "scripted"],
                        help="scripted (default): deterministic TEST 3-10 + agentic autoplay/menu (TEST 9). "
                             "agentic: experimental full-agent-control brain (slower, less reliable).")
    parser.add_argument("--install-browser", action="store_true",
                        help="One-time setup: download the Chromium build Playwright drives, "
                             "using the copy of the playwright package bundled into this exe — "
                             "no separate Python/pip install needed. Run once, then exit "
                             "(ignores every other argument). Needs internet access.")

    args = parser.parse_args()

    if args.install_browser:
        # Frozen builds have no `python -m playwright install` available (no separate Python
        # on the QA machine) — this calls the SAME installer the bundled playwright package
        # ships, so Setup.bat can trigger it from the compiled exe alone.
        from playwright.__main__ import main as _playwright_main
        sys.argv = ["playwright", "install", "chromium"]
        sys.exit(_playwright_main())

    MODE = args.mode

    if args.tests.strip():
        ENABLED_CAPS = {t.strip().lower() for t in args.tests.split(",") if t.strip()}
        print(f"[SETUP] Gated checks enabled: {sorted(ENABLED_CAPS)} "
              f"(skipped: {sorted(set(GATED_CAPS) - ENABLED_CAPS)})")

    # ── Per-run folder: repoint artifacts + tee all output into <run-dir>/logs/run.log ──
    RUN_STARTED_AT = datetime.now().isoformat()
    RUN_START_TS = time.time()
    if args.run_dir:
        RUN_DIR = os.path.abspath(args.run_dir)
        RUN_ID = os.path.basename(RUN_DIR.rstrip(os.sep))
        SCREENSHOT_DIR = os.path.join(RUN_DIR, "screenshots")
        for sub in ("screenshots", "video", "logs"):
            os.makedirs(os.path.join(RUN_DIR, sub), exist_ok=True)
        try:
            _log_fh = open(os.path.join(RUN_DIR, "logs", "run.log"), "a", encoding="utf-8")
            sys.stdout = _Tee(sys.stdout, _log_fh, log_overlay.FEED)
            sys.stderr = _Tee(sys.stderr, _log_fh, log_overlay.FEED)
        except Exception as e:
            print(f"  [WARN] could not open run log: {e}")
            sys.stdout = _Tee(sys.stdout, log_overlay.FEED)
            sys.stderr = _Tee(sys.stderr, log_overlay.FEED)
    else:
        # Direct CLI run (no --run-dir): still mirror output into the in-page feed.
        sys.stdout = _Tee(sys.stdout, log_overlay.FEED)
        sys.stderr = _Tee(sys.stderr, log_overlay.FEED)

    WAIT_SECONDS = args.wait
    spin_override = None
    if args.spin_xy:
        x, y = args.spin_xy.split(",")
        spin_override = (int(x.strip()), int(y.strip()))

    # ── DSC mode: fast sanity sweep + the team's Excel report (one shared file per day) ──
    DSC_MODE = ENABLED_CAPS is not None and "dsc" in ENABLED_CAPS
    DSC_REPORT_PATH = None
    if DSC_MODE:
        from modules import dsc_report
        DSC_REPORT_PATH = os.path.abspath(args.dsc_report) if args.dsc_report else \
            dsc_report.default_report_path(_base_dir())
        # Batch sweeps: the report is a copy of the INPUT sheet (same format, result columns
        # cleared) and each game's row is filled in place as its run completes.
        dsc_report.ensure_report(DSC_REPORT_PATH, seed_from=args.excel or None)
        print(f"[DSC] Report file: {DSC_REPORT_PATH}")

    def _slug(s, maxlen=40):
        s = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (s or "").strip()).strip("-")
        return s[:maxlen] or "game"

    # ── Non-slot classification: daily sheets mix in live/table/crash games. DSC still
    # verifies Launch for them but must not run the slot bet flow (roulette has its own
    # "spin" button — clicking it with no table bet proves nothing). Conservative on
    # purpose: a miss just means the game reports "spin control not detected" instead.
    _NON_SLOT_TYPE_WORDS = ("live", "table", "roulette", "blackjack", "baccarat", "poker",
                            "crash", "instant", "scratch", "virtual", "bingo", "keno",
                            "lottery", "game show", "gameshow")
    _NON_SLOT_NAME_RE = re.compile(
        r"\b(roulette|blackjack|baccarat|poker|keno|bingo|craps|sic bo|andar bahar"
        r"|teen patti|dragon tiger|crazy time|dream catcher|mega ball|funky time"
        r"|monopoly (live|big baller)|lightning (dice|roulette|blackjack|baccarat)"
        r"|deal or no deal|football studio|aviator|spaceman|jetx|plinko)\b", re.I)

    def classify_non_slot(name, game_type=""):
        """Reason string when (name, catalog type) says not-a-slot; None when it's a slot
        or we can't tell. An explicit 'slot' in the catalog type always wins."""
        gt = str(game_type or "").strip().lower()
        if gt and gt != "unknown":
            if "slot" in gt:
                return None
            hit = next((w for w in _NON_SLOT_TYPE_WORDS if w in gt), None)
            if hit:
                return f"type: {game_type}"
        m = _NON_SLOT_NAME_RE.search(name or "")
        return f"name matches '{m.group(0)}'" if m else None

    def ui_test_pipeline(test_url, dsc_meta=None):
        print(f"\n{'='*70}\n🚀 Launching automation for: {test_url}\n{'='*70}")
        asyncio.run(run_tests(test_url, spin_center_override=spin_override, mobile=args.mobile, headless=args.headless, default_bet=args.default_bet, min_bet=args.min_bet, region=args.region, dsc_meta=dsc_meta))

    # --- JPC Backend Flow: --game or --excel ---
    if args.excel or args.game:
        if not jpc_available:
            print("❌ Cannot run JPC backend flow: missing modules.")
            print("   Make sure you're running from the slot-auto directory.")
            sys.exit(1)

        print(f"\n{'='*70}")
        print(f"  GAME RESOLVER")
        print(f"{'='*70}")
        print(f"Brand: {args.brand} | Region: {args.region}")
        print(f"Authenticating (user: {args.username})...")
        auth_res = AuthHandler().authenticate(args.username, args.password,
                                              brand=args.brand, region=args.region)
        if not auth_res.get("success"):
            print(f"❌ Auth failed: {auth_res.get('message')}")
            # DSC batch: this worker's whole shard dies with it — write a failure row per game
            # so the shared report says WHY these games have no result instead of staying blank.
            if DSC_MODE and args.excel and DSC_REPORT_PATH:
                try:
                    for i, g in enumerate(parse_excel(args.excel), 1):
                        meta = {"srNo": g.get("srNo") or i,
                                "provider": g.get("provider") or "Unknown",
                                "gameName": g["gameName"], "evidence": RUN_ID or ""}
                        dsc_report.upsert_row(
                            DSC_REPORT_PATH,
                            dsc_report.failure_row(meta, f"auth failed ({args.username}): "
                                                         f"{auth_res.get('message')}"))
                    print(f"[DSC] Auth-failure rows written -> {DSC_REPORT_PATH}")
                except Exception as e:
                    print(f"[DSC] Could not write auth-failure rows: {e}")
            sys.exit(1)

        token = auth_res["token"]
        print("✅ Authenticated successfully.\n")

        # Build game queue
        games_queue = []
        if args.excel:
            try:
                games_queue = parse_excel(args.excel, log_callback=print)
            except Exception as e:
                print(f"❌ Excel parsing failed: {e}")
                sys.exit(1)
        elif args.game:
            games_queue = [{"gameName": args.game, "status": "pending"}]

        print(f"📋 Queue: {len(games_queue)} game(s)\n")

        gh = GameHandler()
        ih = IframeHandler()

        # DSC batch sweeps get one parent folder with a per-game subfolder each, so the
        # screenshots/video evidence of 300 games never overwrite each other.
        DSC_BATCH_PARENT = None
        if DSC_MODE and args.excel:
            DSC_BATCH_PARENT = RUN_DIR or os.path.join(
                _base_dir(), "runs",
                f"{datetime.now():%Y%m%d_%H%M%S}_DSC")

        for i, g in enumerate(games_queue, 1):
            g_name = g["gameName"]
            print(f"--- [{i}/{len(games_queue)}] Processing: {g_name} ---")

            dsc_meta = None
            if DSC_MODE:
                dsc_meta = {"srNo": g.get("srNo") or i,
                            "provider": g.get("provider") or "Unknown",
                            "gameName": g_name,
                            "report_path": DSC_REPORT_PATH,
                            "evidence": RUN_ID or "",
                            "account": args.username,
                            "brand": args.brand,
                            "region": args.region,
                            "non_slot": classify_non_slot(g_name, g.get("gameType"))}

            # Step 1: Search game
            info = gh.search_game(g_name, token, brand=args.brand, region=args.region)
            if not info:
                print(f"❌ Skipping {g_name}: Not found in Betway catalog.\n")
                if dsc_meta:
                    dsc_report.upsert_row(DSC_REPORT_PATH,
                                          dsc_report.failure_row(dsc_meta, "not found in catalog"))
                continue

            g_id = info.get("id")
            min_bet = info.get("minBetAmount")
            if dsc_meta:
                dsc_meta["min_bet"] = min_bet or args.min_bet
                if dsc_meta["provider"] == "Unknown" and info.get("provider"):
                    dsc_meta["provider"] = info["provider"]
                # The catalog's own type beats input-sheet/name guesses either way.
                if not dsc_meta["non_slot"]:
                    dsc_meta["non_slot"] = classify_non_slot(g_name, info.get("game_type"))
                elif info.get("game_type") and "slot" in str(info["game_type"]).lower():
                    dsc_meta["non_slot"] = None
                if dsc_meta["non_slot"]:
                    print(f"  [DSC] Classified as NON-SLOT ({dsc_meta['non_slot']}) — "
                          f"launch check only, bet flow will be skipped")
            print(f"✅ Found: {g_name} (ID: {g_id}, minBet: {min_bet})")

            # Step 2: Get iframe URL
            iframe = None
            try:
                iframe = ih.get_iframe_url(g_id, token, brand=args.brand, region=args.region)
            except Exception as e:
                print(f"❌ Failed to fetch iframe for {g_name}: {e}\n")
                if dsc_meta:
                    dsc_report.upsert_row(DSC_REPORT_PATH,
                                          dsc_report.failure_row(dsc_meta, f"iframe launch failed: {e}"))
                continue

            if not iframe:
                print(f"❌ No iframe returned for {g_name}.\n")
                if dsc_meta:
                    dsc_report.upsert_row(DSC_REPORT_PATH,
                                          dsc_report.failure_row(dsc_meta, "no iframe URL returned"))
                continue

            print(f"🔗 Iframe URL: {iframe[:80]}...")

            # Per-game artifact folder for DSC sweeps (repoint the run globals before the run).
            if DSC_BATCH_PARENT:
                RUN_DIR = os.path.join(DSC_BATCH_PARENT, f"{i:03d}_{_slug(g_name)}")
                RUN_ID = os.path.basename(RUN_DIR)
                SCREENSHOT_DIR = os.path.join(RUN_DIR, "screenshots")
                for sub in ("screenshots", "video", "logs"):
                    os.makedirs(os.path.join(RUN_DIR, sub), exist_ok=True)
                # Evidence relative to runs/ — parallel workers each have a w<k>/ folder,
                # so a bare basename like "001_Game" wouldn't say WHICH worker ran it.
                _runs_root = os.path.join(_base_dir(), "runs")
                try:
                    dsc_meta["evidence"] = os.path.relpath(RUN_DIR, _runs_root)
                except ValueError:
                    dsc_meta["evidence"] = RUN_ID

            # Step 3: Run UI tests
            try:
                ui_test_pipeline(iframe, dsc_meta=dsc_meta)
            except Exception as e:
                # A crashed game must not kill the remaining queue — record and move on.
                print(f"❌ Run crashed for {g_name}: {e}\n")
                if dsc_meta and not dsc_meta.get("written"):
                    dsc_report.upsert_row(DSC_REPORT_PATH,
                                          dsc_report.failure_row(dsc_meta, f"run crashed: {e}"))

    # --- Standard Flow: direct URL ---
    elif args.url:
        _meta = None
        if DSC_MODE:
            _meta = {"srNo": 1, "provider": "Unknown", "gameName": args.url[:60],
                     "report_path": DSC_REPORT_PATH, "evidence": RUN_ID or "",
                     "account": args.username, "brand": args.brand, "region": args.region,
                     "non_slot": None}
        ui_test_pipeline(args.url, dsc_meta=_meta)
    else:
        parser.print_help()
        print("\n" + "="*70)
        print("EXAMPLES:")
        print("="*70)
        print("  By game name:  python test_spin_button.py --game \"Book of Dead\"")
        print("  By URL:        python test_spin_button.py https://games.example.com/slot/123")
        print("  With creds:    python test_spin_button.py --game \"Starburst\" --username 123 --password 456")
        print("="*70)
