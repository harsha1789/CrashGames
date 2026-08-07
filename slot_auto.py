import sys
import json
import time
import copy
import asyncio
import os
from pathlib import Path
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

# ─── Configuration ───────────────────────────────────────────────
_keys_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.json")
API_KEY = json.load(open(_keys_file))["single_key"] if os.path.exists(_keys_file) else os.environ.get("GEMINI_API_KEY", "")
WAIT_SECONDS = 30          # How long to wait for the game to fully load
VIEWPORT_WIDTH = 1920      # Browser viewport width
VIEWPORT_HEIGHT = 1080     # Browser viewport height
SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")
SCREENSHOT_PATH = os.path.join(SCREENSHOT_DIR, "game_screenshot.png")
OUTPUT_PATH = os.path.join(SCREENSHOT_DIR, "output_detected.png")


# ─── Step 1: Launch browser, navigate, wait, screenshot ─────────
async def capture_game_screenshot(url: str, screenshot_path: str):
    print(f"\n{'='*60}")
    print(f"STEP 1: Capturing game screenshot")
    print(f"{'='*60}")
    print(f"URL: {url}")

    async with async_playwright() as p:
        # Launch headed browser so you can see what's happening
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            # Some game iframes need these permissions
            ignore_https_errors=True,
        )
        page = await context.new_page()

        print(f"Navigating to URL...")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"Navigation warning (may be okay for game iframes): {e}")

        print(f"Waiting {WAIT_SECONDS} seconds for game to fully load...")
        await asyncio.sleep(WAIT_SECONDS)

        # Try to find and interact with any "start" or "continue" overlay
        # Some games have a click-to-start splash screen
        try:
            await page.mouse.click(VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)
            await asyncio.sleep(2)
        except:
            pass

        print(f"Taking screenshot...")
        await page.screenshot(path=screenshot_path, full_page=False)
        print(f"Screenshot saved to: {screenshot_path}")

        await browser.close()

    return screenshot_path


# ─── Step 2: Detect buttons via Gemini ───────────────────────────
def detect_buttons(image_path: str):
    print(f"\n{'='*60}")
    print(f"STEP 2: Detecting UI buttons via Gemini API")
    print(f"{'='*60}")

    original_img = Image.open(image_path)
    orig_width, orig_height = original_img.size
    print(f"Screenshot resolution: {orig_width}x{orig_height}")

    # Thumbnail for API
    api_img = copy.deepcopy(original_img)
    api_img.thumbnail([1024, 1024], Image.Resampling.LANCZOS)
    api_width, api_height = api_img.size
    print(f"Image sent to API: {api_width}x{api_height}")

    # Focused prompt: ONLY interactive UI controls, no slot symbols
    prompt = """You are analyzing a slot/casino game screenshot. Your task is to detect ONLY the interactive UI control buttons.

IMPORTANT RULES:
- ONLY detect clickable UI buttons and controls in the bottom toolbar/HUD area.
- DO NOT detect slot reel symbols (crowns, letters, numbers, chalices, etc.)
- DO NOT detect the game logo, background art, or decorative elements.
- DO NOT detect the reel grid or any symbols inside it.

Detect ONLY these types of interactive controls:
1. Spin button - the main play button (usually large, circular, on the right side of the bottom bar)
2. Autospin / Autoplay button - circular arrows or auto-play toggle
3. Bet Increment - small up arrow or + button near the bet amount
4. Bet Decrement - small down arrow or - button near the bet amount
5. Menu button - hamburger icon (three lines), usually bottom-left
6. Sound toggle - speaker/volume icon
7. Turbo / Fast Spin - lightning bolt or fast-forward icon
8. Info / Paytable button - "i" icon or question mark
9. Max Bet button - if visible
10. Balance display area
11. Bet amount display area

For each detected element, return a JSON object with:
- "label": a specific descriptive name (e.g. "Spin Button", "Menu Button", "Bet Increment")
- "box_2d": [ymin, xmin, ymax, xmax] normalized to 0-1000

Do NOT use generic labels like "label" or "button". Every label must be descriptive.
"""

    print("Sending to Gemini API...")
    client = genai.Client(api_key=API_KEY)

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        thinking_config=types.ThinkingConfig(thinking_budget=0)
    )

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[api_img, prompt],
        config=config
    )

    data = json.loads(response.text)

    # Filter out any generic "label" results that slipped through
    filtered = [item for item in data if item.get("label", "").strip().lower() != "label"]
    print(f"Detected {len(data)} elements, kept {len(filtered)} UI controls (filtered {len(data)-len(filtered)} generic labels):")
    print(json.dumps(filtered, indent=2))

    return filtered, original_img


# ─── Step 3: Draw bounding boxes ────────────────────────────────
def draw_boxes(data, original_img, output_path: str):
    print(f"\n{'='*60}")
    print(f"STEP 3: Drawing bounding boxes")
    print(f"{'='*60}")

    orig_width, orig_height = original_img.size

    # Work at original resolution
    draw_img = original_img.copy().convert("RGBA")
    overlay = Image.new("RGBA", draw_img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw_main = ImageDraw.Draw(draw_img)

    try:
        font_size = max(12, int(orig_height / 55))
        font = ImageFont.truetype("arial.ttf", size=font_size)
    except IOError:
        font = ImageFont.load_default()

    # Distinct, vivid colors for UI controls
    colors = [
        (0, 255, 128),   # Green
        (255, 80, 80),    # Red
        (80, 180, 255),   # Blue
        (255, 200, 0),    # Yellow
        (200, 80, 255),   # Purple
        (255, 128, 0),    # Orange
        (0, 255, 255),    # Cyan
        (255, 80, 200),   # Pink
        (128, 255, 0),    # Lime
        (255, 255, 128),  # Light yellow
    ]

    results = []

    for i, item in enumerate(data):
        label = item.get("label", "Unknown")
        box = item.get("box_2d", None)

        if box is None or len(box) != 4:
            continue

        ymin_norm, xmin_norm, ymax_norm, xmax_norm = box

        # Convert normalized [0-1000] to pixels
        left = int(xmin_norm / 1000 * orig_width)
        top = int(ymin_norm / 1000 * orig_height)
        right = int(xmax_norm / 1000 * orig_width)
        bottom = int(ymax_norm / 1000 * orig_height)

        # Clamp
        left = max(0, min(left, orig_width - 1))
        top = max(0, min(top, orig_height - 1))
        right = max(0, min(right, orig_width - 1))
        bottom = max(0, min(bottom, orig_height - 1))

        if left > right:
            left, right = right, left
        if top > bottom:
            top, bottom = bottom, top

        color = colors[i % len(colors)]
        center_x = (left + right) // 2
        center_y = (top + bottom) // 2

        results.append({
            "label": label,
            "center": (center_x, center_y),
            "bbox": (left, top, right, bottom)
        })

        print(f"  {label}: bbox=({left},{top},{right},{bottom}) center=({center_x},{center_y})")

        # Draw semi-transparent fill
        fill_color = color + (40,)  # Low alpha fill
        draw.rectangle([left, top, right, bottom], fill=fill_color)

        # Draw solid border on main image
        line_width = 2
        draw_main.rectangle([left, top, right, bottom], outline=color, width=line_width)

        # Draw compact label inside top of box
        try:
            text_bbox = draw_main.textbbox((0, 0), label, font=font)
            text_w = text_bbox[2] - text_bbox[0]
            text_h = text_bbox[3] - text_bbox[1]
        except Exception:
            text_w, text_h = 80, 14

        # Place label at top-left of box, inside
        label_x = left + 2
        label_y = top + 2
        # If box is too small, place above
        if (bottom - top) < text_h + 6:
            label_y = top - text_h - 4
            if label_y < 0:
                label_y = bottom + 2

        # Dark background pill for readability
        pill_rect = [label_x - 1, label_y - 1, label_x + text_w + 5, label_y + text_h + 3]
        draw.rectangle(pill_rect, fill=(0, 0, 0, 180))
        draw_main.text((label_x + 2, label_y), label, fill=color, font=font)

        # Draw center dot
        dot_r = 3
        draw_main.ellipse(
            [center_x - dot_r, center_y - dot_r, center_x + dot_r, center_y + dot_r],
            fill=color
        )

    # Composite overlay onto main image
    draw_img = Image.alpha_composite(draw_img, overlay)
    draw_img = draw_img.convert("RGB")

    print(f"\nSaving output to {output_path}...")
    draw_img.save(output_path, quality=95)
    print(f"Done! Output size: {orig_width}x{orig_height}")

    return results


# ─── Main pipeline ───────────────────────────────────────────────
async def main():
    if len(sys.argv) < 2:
        print("Usage: python slot_auto.py <iframe_url> [wait_seconds]")
        print("  Example: python slot_auto.py https://games.example.com/slot/123")
        sys.exit(1)

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    global WAIT_SECONDS
    url = sys.argv[1]
    WAIT_SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else WAIT_SECONDS

    # Step 1: Capture screenshot
    screenshot = await capture_game_screenshot(url, SCREENSHOT_PATH)

    # Step 2: Detect buttons
    data, img = detect_buttons(screenshot)

    # Step 3: Draw and save
    results = draw_boxes(data, img, OUTPUT_PATH)

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"Screenshot: {SCREENSHOT_PATH}")
    print(f"Annotated:  {OUTPUT_PATH}")
    print(f"\nDetected button coordinates (original pixels):")
    for r in results:
        print(f"  {r['label']:25s} center=({r['center'][0]:4d}, {r['center'][1]:4d})  bbox={r['bbox']}")

    # Save results as JSON too
    json_path = os.path.join(SCREENSHOT_DIR, "detection_results.json")
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nJSON results saved to: {json_path}")


if __name__ == "__main__":
    asyncio.run(main())
