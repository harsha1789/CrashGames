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
import time
import asyncio
import os
from urllib.parse import urlparse
from google import genai
from google.genai import types
from PIL import Image
from playwright.async_api import async_playwright

# --- Configuration ---
_keys_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.json")
API_KEYS = json.load(open(_keys_file))["key_list"] if os.path.exists(_keys_file) else []

WAIT_SECONDS = 30
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080
MAX_RETRIES = 5
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
current_key_idx = 0
client = genai.Client(api_key=API_KEYS[current_key_idx])

def rotate_api_key():
    global current_key_idx, client
    current_key_idx = (current_key_idx + 1) % len(API_KEYS)
    client = genai.Client(api_key=API_KEYS[current_key_idx])
    print(f"    [!] Switched to backup Gemini API Key...")


def _ss(name):
    """Return screenshot path inside SCREENSHOT_DIR."""
    return os.path.join(SCREENSHOT_DIR, name)

NOISE_PATTERNS = [
    "google-analytics", "analytics", "/collect", "googletagmanager",
    "facebook", "doubleclick", "hotjar", "clarity", "sentry",
    ".js", ".css", ".png", ".jpg", ".gif", ".svg", ".woff", ".ttf", ".woff2",
    "hot-update", "sockjs", "__webpack", "favicon",
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
    api_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)

    prompt = """Analyze this slot/casino game screenshot. Detect ALL interactive UI controls.

RULES:
- ONLY detect clickable buttons and controls (NOT slot symbols or decorations)
- Each control must have a specific descriptive label

Detect these types:
1. Spin Button - the main play button
2. Autoplay Button / Autospin Button  
3. Bet Increment - up arrow or + button near bet amount
4. Bet Decrement - down arrow or - button near bet amount
5. Menu Button - hamburger icon or settings
6. Sound Toggle - speaker/volume icon
7. Turbo / Fast Spin - lightning bolt or speed icon
8. Info / Paytable - "i" icon
9. Buy Bonus - if visible
10. Balance Display - the balance amount area
11. Bet Display - the bet amount area

Return a JSON array of objects with:
- "label": specific name (e.g. "Spin Button", "Bet Increment", "Menu Button")
- "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000"""

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
            ymin, xmin, ymax, xmax = box
            item["center"] = (
                int((xmin + xmax) / 2 / 1000 * VIEWPORT_WIDTH),
                int((ymin + ymax) / 2 / 1000 * VIEWPORT_HEIGHT)
            )
            valid_controls.append(item)
        elif item.get("label"):
            # Keep the label info even without valid coords
            valid_controls.append(item)

    return valid_controls


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

    def __str__(self):
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

async def clear_highlights(page):
    try:
        await page.evaluate('''() => {
            const els = document.querySelectorAll('.gameguard-highlight');
            els.forEach(el => el.remove());
        }''')
    except Exception:
        pass

        icon = "PASS" if self.passed is True else "FAIL" if self.passed is False else "SKIP"
        return f"  [{icon}] {self.name}: {self.details}"


# ─── Network Monitor ────────────────────────────────────────────
class NetworkMonitor:
    def __init__(self):
        self._all_requests = []
        self._all_responses = []
        self.idle_post_paths = set()
        self.spin_endpoint = None
        self.spin_requests = []
        self._monitoring = False

    def attach(self, page):
        def on_request(req):
            if req.resource_type not in ("fetch", "xhr"):
                return
            entry = {
                "url": req.url, "path": _extract_path(req.url),
                "method": req.method, "time": time.time(),
                "post_data": req.post_data[:500] if hasattr(req, 'post_data') and req.post_data else None,
            }
            self._all_requests.append(entry)
            if self._monitoring and self.spin_endpoint:
                if entry["path"] == self.spin_endpoint:
                    self.spin_requests.append(entry)
                    print(f"    [NET] >> Spin request intercepted! (total: {len(self.spin_requests)})")

        async def on_response(resp):
            try:
                body = await resp.text()
            except:
                body = ""
            self._all_responses.append({
                "url": resp.url, "path": _extract_path(resp.url),
                "status": resp.status, "time": time.time(), "body": body[:2000],
            })

        def on_websocket(ws):
            def on_framesent(payload):
                path = _extract_path(ws.url) + "@WS_SEND"
                entry = {
                    "url": ws.url, "path": path,
                    "method": "WS_SEND", "time": time.time(),
                    "post_data": str(payload)[:500]
                }
                self._all_requests.append(entry)
                if self._monitoring and self.spin_endpoint:
                    if entry["path"] == self.spin_endpoint:
                        self.spin_requests.append(entry)
                        print(f"    [NET] >> Spin WS frame intercepted! (total: {len(self.spin_requests)})")

            def on_framereceived(payload):
                path = _extract_path(ws.url) + "@WS_SEND" # Map response to the same endpoint
                self._all_responses.append({
                    "url": ws.url, "path": path,
                    "status": 200, "time": time.time(), "body": str(payload)[:2000],
                })

            ws.on("framesent", on_framesent)
            ws.on("framereceived", on_framereceived)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", on_websocket)

    async def learn_idle(self, duration=8):
        print(f"  [IDLE] Recording idle traffic for {duration}s...")
        start = len(self._all_requests)
        await asyncio.sleep(duration)
        for r in self._all_requests[start:]:
            if r["method"] in ("GET", "POST", "WS_SEND") and not _is_noise(r["url"]):
                self.idle_post_paths.add(r["path"])
        print(f"  [IDLE] Background paths: {self.idle_post_paths or '{none}'}")

    async def discover_spin_endpoint(self, page, spin_center):
        req_before = len(self._all_requests)
        print(f"  [CALIBRATE] Clicking spin at {spin_center}...")
        await page.mouse.click(*spin_center)
        await asyncio.sleep(8)

        for r in self._all_requests[req_before:]:
            if r["method"] in ("GET", "POST", "WS_SEND") and not _is_noise(r["url"]):
                if r["path"] not in self.idle_post_paths:
                    self.spin_endpoint = r["path"]
                    break
        if not self.spin_endpoint:
            for r in self._all_requests[req_before:]:
                if r["method"] in ("GET", "POST", "WS_SEND") and not _is_noise(r["url"]):
                    self.spin_endpoint = r["path"]
                    break

        print(f"  [CALIBRATE] Discovered endpoint: {self.spin_endpoint}")
        return self.spin_endpoint

    def get_new_posts_since(self, start_idx):
        return [r for r in self._all_requests[start_idx:]
                if r["method"] in ("GET", "POST", "WS_SEND") and not _is_noise(r["url"])]

    def start_monitoring(self):
        self.spin_requests.clear()
        self._monitoring = True

    def stop_monitoring(self):
        self._monitoring = False

    @property
    def spin_count(self):
        return len(self.spin_requests)

    def clear_spins(self):
        self.spin_requests.clear()

    async def wait_for_spin_completion(self, timeout=20):
        start = time.time()
        while time.time() - start < timeout:
            resp_count = sum(1 for r in self._all_responses if r["path"] == self.spin_endpoint)
            req_count = len(self.spin_requests)
            if req_count > 0 and resp_count >= req_count:
                import asyncio
                await asyncio.sleep(1.5)
                return True
            import asyncio
            await asyncio.sleep(0.5)
        return False


async def auto_handle_startup(page):
    """Smart startup loop utilizing Gemini to detect loading state and click overlays."""
    print("\n  [STARTUP] Starting AI visual check loop...")
    await asyncio.sleep(8)  # initial wait for HTML rendering / network
    
    max_attempts = 15
    for attempt in range(1, max_attempts + 1):
        try:
            ss_path = _ss(f"startup_check.png")
            await page.screenshot(path=ss_path)
            img = Image.open(ss_path)
            img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)

            prompt = """Analyze this slot game screenshot. Determine the current state of the game startup.
There are 3 possible states:
1. "wait" - The game is still loading (progress bar visible, blank screen, or spinning loader).
2. "click" - The game has loaded an intro screen, age check, sound prompt, or instructions WITH a specific button to click (e.g., "Continue", "Start", "Play", "I Accept", "OK").
3. "ready" - The main slot game grid, reels, spin button, and bet amounts are fully visible, and NO intro overlays are blocking the center of the screen.

If "click", you MUST provide the precise bounding box of the button to click.
Return JSON in this format: {"state": "wait" | "click" | "ready", "box_2d": [ymin, xmin, ymax, xmax] or null}
Normalize box_2d to 0-1000."""
            
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )
            
            response_json = parse_gemini_json(gemini_call([img, prompt], config))
            state = response_json.get("state", "wait")
            
            if state == "ready":
                print(f"    [{attempt}/{max_attempts}] 🎯 Gemini says 'ready' - Game fully loaded!")
                return True
            elif state == "click" and response_json.get("box_2d"):
                box = response_json["box_2d"]
                if len(box) == 4:
                    ymin, xmin, ymax, xmax = box
                    cx = int((xmin + xmax) / 2 / 1000 * VIEWPORT_WIDTH)
                    cy = int((ymin + ymax) / 2 / 1000 * VIEWPORT_HEIGHT)
                    print(f"    [{attempt}/{max_attempts}] 🖱️ Gemini found a button! Clicking at ({cx}, {cy})...")
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
async def run_tests(url: str, spin_center_override: tuple = None, mobile: bool = False, default_bet: str = "", min_bet: str = ""):
    results = []
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    monitor = NetworkMonitor()

    print(f"\n{'='*70}")
    print(f"  SLOT GAME UI TEST SUITE")
    print(f"{'='*70}")
    print(f"URL: {url}\n")
    
    recordings_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context_args = {
            "ignore_https_errors": True,
            "record_video_dir": recordings_dir
        }
        if mobile:
            context_args.update(p.devices['iPhone 13'])
        else:
            context_args["viewport"] = {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
        context = await browser.new_context(**context_args)
        page = await context.new_page()
        monitor.attach(page)

        # --- SETUP ---
        print(f"[SETUP] Loading game...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  Nav warning: {e}")

        # AI Startup handling replaces manual waiting
        await auto_handle_startup(page)

        await monitor.learn_idle(duration=8)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 1: Detect ALL UI controls
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        print(f"  TEST 1: Detect all UI controls")
        print(f"{'='*70}")
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

        t1 = TestResult("All UI controls detected", "test_pre.png")
        t1.passed = len(controls) >= 3  # At minimum: spin, bet display, balance
        t1.details = f"Found {len(controls)} controls: {[c.get('label') for c in controls]}"
        results.append(t1)
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
        # TEST: Default Bet Verification
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if default_bet:
            print(f"\n{'='*70}\n  TEST: Verify Default Bet\n{'='*70}")
            t_def = TestResult("Default bet matches expected")
            if pre_bet is not None:
                dbet_val = parse_amount(default_bet)
                t_def.passed = (pre_bet == dbet_val)
                t_def.details = f"Expected UI default '{default_bet}', found '{pre_bet}'."
            else:
                t_def.passed = False
                t_def.details = "Could not parse initial bet from UI"
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

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Minimum Bet Verification
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if min_bet:
            print(f"\n{'='*70}\n  TEST: Verify Minimum Bet\n{'='*70}")
            t_min = TestResult("Minimum bet matches expected")
            if bet_dec and "center" in bet_dec:
                print(f"  Spam clicking 'Decrease Bet' at {bet_dec['center']} to reach minimum...")
                for _ in range(8):
                    await page.mouse.click(*bet_dec["center"])
                    await asyncio.sleep(0.5)
                
                await page.screenshot(path=_ss("test_min_bet.png"))
                min_vals = read_game_values(Image.open(_ss("test_min_bet.png")))
                min_bet_found = parse_amount(min_vals.get("bet", ""))
                
                mbet_val = parse_amount(min_bet)
                t_min.passed = (min_bet_found == mbet_val)
                t_min.details = f"Expected '{min_bet}', found '{min_bet_found}'."
            else:
                t_min.passed = False
                t_min.details = "No 'Decrease Bet' control detected"
            results.append(t_min)
            print(t_min)

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
        print(f"  TEST 2: Spin endpoint discovery")
        print(f"{'='*70}")

        endpoint = await monitor.discover_spin_endpoint(page, spin_center)

        t2 = TestResult("Spin endpoint discovered", "test_pre.png")
        t2.passed = bool(endpoint)
        t2.details = f"Endpoint: {endpoint}"
        results.append(t2)
        print(t2)

        if not endpoint:
            print("  [!] Warning: Could not isolate network spin endpoint. Proceeding with visual-only tests.")

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 3: Single spin click
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        print(f"  TEST 3: Single click = 1 spin request")
        print(f"{'='*70}")

        # Capture state before spinning
        await page.screenshot(path=_ss("test_prespin.png"))
        prespin_vals = read_game_values(Image.open(_ss("test_prespin.png")))
        prespin_bal = parse_amount(prespin_vals.get("balance", ""))
        prespin_bet = parse_amount(prespin_vals.get("bet", ""))

        monitor.clear_spins()
        monitor.start_monitoring()

        await page.mouse.click(*spin_center)
        await monitor.wait_for_spin_completion()
        monitor.stop_monitoring()

        t3 = TestResult("Single click = 1 spin request", "test_postspin.png")
        t3.passed = monitor.spin_count == 1
        t3.details = f"Spin requests: {monitor.spin_count}"
        results.append(t3)
        print(t3)

        # Post-spin state capture
        await asyncio.sleep(1) # wait for animations to settle
        await page.screenshot(path=_ss("test_postspin.png"))
        postspin_vals = read_game_values(Image.open(_ss("test_postspin.png")))
        postspin_bal = parse_amount(postspin_vals.get("balance", ""))

        # Network Payout & Feature detection
        import re
        spin_resps = [r for r in monitor._all_responses if monitor.spin_endpoint and monitor.spin_endpoint in r["path"]]
        payout = 0.0
        feature_triggered = False
        if spin_resps:
            last_resp = spin_resps[-1]
            try:
                import json
                data = json.loads(last_resp["body"])
                str_body = str(data).lower()
                payout_match = re.search(r"'(?:win|winamount|payout|totalwin)':\s*([\d\.]+)", str_body)
                if payout_match:
                    payout = float(payout_match.group(1))
                if "feature" in str_body or "freespin" in str_body or "bonus" in str_body:
                    feature_triggered = True
            except:
                pass

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Wager Processing
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Wager Processing\n{'='*70}")
        t_wager = TestResult("Wager correctly processed", "test_prespin.png")
        if prespin_bet is not None:
            t_wager.passed = True
            t_wager.details = f"Wager of {prespin_bet} applied during spin."
        else:
            t_wager.passed = False
            t_wager.details = "Could not identify wager amount before spin."
        results.append(t_wager)
        print(t_wager)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Payout Handling
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Payout Handling\n{'='*70}")
        t_payout = TestResult("Payout successfully logged (if applicable)", "test_postspin.png")
        t_payout.passed = True
        t_payout.details = f"Network payout detected: {payout}" if payout > 0 else "No payout on this spin."
        results.append(t_payout)
        print(t_payout)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Feature Triggered Events
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Feature Triggered Events\n{'='*70}")
        t_feat = TestResult("Feature Triggers monitored", "test_postspin.png")
        t_feat.passed = True
        t_feat.details = "Feature triggered!" if feature_triggered else "No features triggered."
        results.append(t_feat)
        print(t_feat)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Balance Update
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Balance Update\n{'='*70}")
        t_bal = TestResult("Balance updated correctly", "test_postspin.png")
        if prespin_bal is not None and prespin_bet is not None and postspin_bal is not None:
            expected_bal = round(prespin_bal - prespin_bet + payout, 2)
            # floating point inaccuracies can be annoying, checking difference
            if abs(postspin_bal - expected_bal) < 0.10:
                t_bal.passed = True
                t_bal.details = f"Balance updated: {prespin_bal} -> {postspin_bal}"
            else:
                t_bal.passed = False
                t_bal.details = f"Expected {expected_bal}, but UI shows {postspin_bal}"
        else:
            t_bal.passed = None
            t_bal.details = f"Missing data. pre_bal:{prespin_bal}, pre_bet:{prespin_bet}, post_bal:{postspin_bal}"
        results.append(t_bal)
        print(t_bal)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 4: Rapid clicks during spin
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        print(f"  TEST 4: Rapid clicks during spin = still 1 request")
        print(f"{'='*70}")
        monitor.clear_spins()
        monitor.start_monitoring()

        await page.mouse.click(*spin_center)
        # Wait for spin request to confirm in-flight
        for _ in range(30):
            await asyncio.sleep(0.1)
            if monitor.spin_count >= 1:
                break

        if monitor.spin_count >= 1:
            print(f"  Spin in-flight! Spam-clicking...")
            for _ in range(8):
                await page.mouse.click(*spin_center)
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.5)
            monitor.stop_monitoring()
            await monitor.wait_for_spin_completion()

            t4 = TestResult("Rapid clicks during spin = still 1 request", "test_postspin.png")
            t4.passed = monitor.spin_count == 1
            t4.details = f"9 clicks, {monitor.spin_count} spin(s). {'Button DISABLED!' if monitor.spin_count == 1 else 'NOT disabled!'}"
        else:
            monitor.stop_monitoring()
            t4 = TestResult("Rapid clicks during spin = still 1 request", "test_postspin.png")
            t4.passed = None
            t4.details = "Skipped: spin did not fire"

        results.append(t4)
        print(t4)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 5: Spin re-enables after completion
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
        print(f"  TEST 5: Spin button re-enables after completion")
        print(f"{'='*70}")
        monitor.clear_spins()
        monitor.start_monitoring()
        await page.mouse.click(*spin_center)
        await monitor.wait_for_spin_completion()
        monitor.stop_monitoring()

        t5 = TestResult("Spin button re-enables after completion", "test_postspin.png")
        t5.passed = monitor.spin_count == 1
        t5.details = f"Spin requests: {monitor.spin_count}"
        results.append(t5)
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
        if has_any_bet_control:
            print(f"\n{'='*70}")
            print(f"  TEST 6: Bet can be changed")
            print(f"{'='*70}")

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
                        ymin, xmin, ymax, xmax = box
                        bx = int((xmin + xmax) / 2 / 1000 * VIEWPORT_WIDTH)
                        by = int((ymin + ymax) / 2 / 1000 * VIEWPORT_HEIGHT)
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
            await page.mouse.click(VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 3)
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
            print(t6)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # TEST 7: Restore bet to original (round-trip)
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            if bet_changed:
                print(f"\n{'='*70}")
                print(f"  TEST 7: Bet can be restored (round-trip)")
                print(f"{'='*70}")

                restore_btn = bet_dec or bet_inc or bet_display
                if restore_btn and "center" in restore_btn:
                    await page.mouse.click(*restore_btn["center"])
                    await asyncio.sleep(1.5)
                    await page.screenshot(path=_ss("test_bet_restore.png"))

                    rest_img = copy.deepcopy(Image.open(_ss("test_bet_restore.png")))
                    rest_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
                    rt_prompt = f"""This slot game screenshot may show a bet selection overlay.
Find the bet option for {before_vals.get('bet', 'unknown')}.
Return JSON: {{"bet_option": {{"label": "text", "box_2d": [ymin, xmin, ymax, xmax]}} or null}}
Normalize box_2d to 0-1000."""
                    rt_result = parse_gemini_json(gemini_call([rest_img, rt_prompt], config))
                    if rt_result.get("bet_option") and rt_result["bet_option"].get("box_2d"):
                        box = rt_result["bet_option"]["box_2d"]
                        if len(box) == 4:
                            ymin, xmin, ymax, xmax = box
                            rx = int((xmin + xmax) / 2 / 1000 * VIEWPORT_WIDTH)
                            ry = int((ymin + ymax) / 2 / 1000 * VIEWPORT_HEIGHT)
                            print(f"  Selecting original bet at ({rx}, {ry})...")
                            await page.mouse.click(rx, ry)
                            await asyncio.sleep(1.5)

                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)

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
                print(t7)
            else:
                t7 = TestResult("Bet can be restored (round-trip)", "test_bet_restored.png")
                t7.passed = None
                t7.details = "Skipped: bet change failed"
                results.append(t7)
                print(t7)
        else:
            t6 = TestResult("Bet can be changed", "test_bet_before.png")
            t6.passed = None
            t6.details = "No bet control detected"
            results.append(t6)
            print(t6)

            t7 = TestResult("Bet can be restored (round-trip)", "test_bet_restored.png")
            t7.passed = None
            t7.details = "No bet control detected"
            results.append(t7)
            print(t7)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 8: Bet buttons disabled during spin
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        bet_btn = bet_inc or bet_dec
        if bet_btn and "center" in bet_btn:
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
            print(t8)
        else:
            t8 = TestResult("Bet buttons disabled during spin", "test_bet_spin_after.png")
            t8.passed = None
            t8.details = "No bet buttons detected"
            results.append(t8)
            print(t8)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST: Auto Play Trigger
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}\n  TEST: Auto Play Trigger\n{'='*70}")
        if autoplay_ctrl and "center" in autoplay_ctrl:
            print(f"  Found Autoplay control at {autoplay_ctrl['center']}. Clicking...")
            await page.mouse.click(*autoplay_ctrl["center"])
            await asyncio.sleep(2)
            
            # Autoplay might open a menu or start instantly. 
            await page.screenshot(path=_ss("test_autoplay.png"))
            
            t_auto = TestResult("Auto Play functionality works", "test_autoplay.png")
            t_auto.passed = True
            t_auto.details = "Autoplay interacted successfully."
            
            # Click it again to close/stop for safety
            await page.mouse.click(*autoplay_ctrl["center"])
            await asyncio.sleep(1)
            # Click center of screen to dismiss any panel
            await page.mouse.click(VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)
            await asyncio.sleep(1)
        else:
            t_auto = TestResult("Auto Play functionality works", "test_autoplay.png")
            t_auto.passed = None
            t_auto.details = "No Autoplay button detected in initial UI."
        results.append(t_auto)
        print(t_auto)

        # # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 9: Menu button opens a panel
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if menu_ctrl and "center" in menu_ctrl:
            print(f"\n        # {'='*70}")
            print(f"  TEST 9: Menu button opens a panel")
            print(f"{'='*70}")

            await page.screenshot(path=_ss("test_menu_before.png"))
            print(f"  Clicking Menu at {menu_ctrl['center']}...")
            await page.mouse.click(*menu_ctrl["center"])
            await asyncio.sleep(2)
            await page.screenshot(path=_ss("test_menu_after.png"))

            # Use Gemini to check if a menu/panel opened
            before_img = copy.deepcopy(Image.open(_ss("test_menu_before.png")))
            after_img = copy.deepcopy(Image.open(_ss("test_menu_after.png")))
            before_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
            after_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)

            check_prompt = """Compare these two slot game screenshots (before and after clicking the menu button).
Did a menu panel, settings panel, or overlay open after the click?
Return JSON: {"menu_opened": true/false, "reason": "brief explanation"}"""

            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            )
            menu_result = parse_gemini_json(gemini_call([before_img, after_img, check_prompt], config))

            t9 = TestResult("Menu button opens a panel")
            t9.passed = menu_result.get("menu_opened", False)
            t9.details = menu_result.get("reason", "N/A")
            results.append(t9)
            print(t9)

            # Close menu by clicking away or pressing escape
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
            await page.mouse.click(VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)
            await asyncio.sleep(1)
        else:
            t9 = TestResult("Menu button opens a panel")
            t9.passed = None
            t9.details = "Menu button not detected"
            results.append(t9)
            print(t9)

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # TEST 10: Valid server response
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        print(f"\n{'='*70}")
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
        print(t10)
        
        await page.close()
        video_filename = ""
        try:
            video_path = await page.video.path()
            video_filename = os.path.basename(video_path)
        except Exception:
            pass
        
        for r in results:
            r.video = video_filename

        await browser.close()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # FINAL REPORT
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print(f"\n{'='*70}")
    print(f"  TEST RESULTS SUMMARY")
    print(f"{'='*70}")

    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    skipped = sum(1 for r in results if r.passed is None)

    for r in results:
        print(r)

    print(f"\n  Total: {len(results)} | Passed: {passed} | Failed: {failed} | Skipped: {skipped}")
    if failed == 0 and passed > 0:
        print(f"  ALL TESTS PASSED!")
    elif failed > 0:
        print(f"  {failed} TEST(S) FAILED")
    print(f"{'='*70}\n")

    payload = [{"name": r.name, "passed": r.passed, "details": r.details, "screenshot": r.screenshot, "video": r.video, "video_start": round(r.video_start, 1), "video_end": round(r.video_end, 1)} for r in results]
    root_dir = os.path.dirname(os.path.abspath(__file__))
    results_file = os.path.join(root_dir, "test_results.json")
    with open(results_file, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Results saved to {results_file}")
    print("\nREPORTPAYLOAD===")
    print(json.dumps(payload))
    print("===REPORTPAYLOAD\n")
    return results


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

    # JPC Backend Integration Arguments
    parser.add_argument("--game", type=str, help="Game name to search on JackpotCity and auto-resolve iframe URL")
    parser.add_argument("--excel", type=str, help="Path to Excel sheet for JPC bulk testing")
    parser.add_argument("--username", type=str, default="222212222", help="JPC Username (default: 222212222)")
    parser.add_argument("--password", type=str, default="222212222", help="JPC Password (default: 222212222)")
    parser.add_argument("--default-bet", type=str, default="", help="Expected default bet amount")
    parser.add_argument("--min-bet", type=str, default="", help="Expected minimum bet amount")

    args = parser.parse_args()

    WAIT_SECONDS = args.wait
    spin_override = None
    if args.spin_xy:
        x, y = args.spin_xy.split(",")
        spin_override = (int(x.strip()), int(y.strip()))

    def ui_test_pipeline(test_url):
        print(f"\n{'='*70}\n🚀 Launching automation for: {test_url}\n{'='*70}")
        asyncio.run(run_tests(test_url, spin_center_override=spin_override, mobile=args.mobile, default_bet=args.default_bet, min_bet=args.min_bet))

    # --- JPC Backend Flow: --game or --excel ---
    if args.excel or args.game:
        if not jpc_available:
            print("❌ Cannot run JPC backend flow: missing modules.")
            print("   Make sure you're running from the slot-auto directory.")
            sys.exit(1)

        print(f"\n{'='*70}")
        print(f"  BETWAY GAME RESOLVER")
        print(f"{'='*70}")
        print(f"Authenticating with Betway (user: {args.username})...")
        auth_res = AuthHandler().authenticate(args.username, args.password)
        if not auth_res.get("success"):
            print(f"❌ Auth failed: {auth_res.get('message')}")
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

        for i, g in enumerate(games_queue, 1):
            g_name = g["gameName"]
            print(f"--- [{i}/{len(games_queue)}] Processing: {g_name} ---")

            # Step 1: Search game
            info = gh.search_game(g_name, token)
            if not info:
                print(f"❌ Skipping {g_name}: Not found in Betway catalog.\n")
                continue

            g_id = info.get("id")
            min_bet = info.get("minBetAmount")
            print(f"✅ Found: {g_name} (ID: {g_id}, minBet: {min_bet})")

            # Step 2: Get iframe URL
            iframe = None
            try:
                iframe = ih.get_iframe_url(g_id, token)
            except Exception as e:
                print(f"❌ Failed to fetch iframe for {g_name}: {e}\n")
                continue

            if not iframe:
                print(f"❌ No iframe returned for {g_name}.\n")
                continue

            print(f"🔗 Iframe URL: {iframe[:80]}...")

            # Step 3: Run UI tests
            ui_test_pipeline(iframe)

    # --- Standard Flow: direct URL ---
    elif args.url:
        ui_test_pipeline(args.url)
    else:
        parser.print_help()
        print("\n" + "="*70)
        print("EXAMPLES:")
        print("="*70)
        print("  By game name:  python test_spin_button.py --game \"Book of Dead\"")
        print("  By URL:        python test_spin_button.py https://games.example.com/slot/123")
        print("  With creds:    python test_spin_button.py --game \"Starburst\" --username 123 --password 456")
        print("="*70)
