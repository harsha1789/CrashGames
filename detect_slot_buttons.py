"""
detect_slot_buttons.py
======================
Detects interactive UI controls in a slot game screenshot
using a LOCAL vision model via LM Studio (OpenAI-compatible API).

Model:   qwen/qwen2.5-vl-7b
Usage:   python detect_slot_buttons.py <screenshot.png> [output.png]
"""

import sys
import json
import base64
import re
import requests
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import copy
import io
import math

# ─── Config ───────────────────────────────────────────────────────────────────
LM_STUDIO_URL  = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_ID       = "qwen/qwen2.5-vl-7b"
MAX_IMAGE_SIZE = 1024   # longest edge sent to model

# ─── Prompt ───────────────────────────────────────────────────────────────────
# Key lessons from testing Qwen2.5-VL:
#   1. Do NOT give it a list of 11 categories — it hallucinates every one
#   2. Ask it to ONLY report what it can actually SEE with certainty
#   3. Qwen outputs [x1, y1, x2, y2] absolute pixels — but needs full-height boxes
#   4. Ask for the CENTER point as backup — easier for the model to answer accurately
DETECTION_PROMPT = """Look at this slot game screenshot carefully.

Find every visible interactive button in the game's control bar (the toolbar/panel at the bottom of the screen).

For EACH button you can clearly identify:
- Give it a short descriptive label (e.g. "Spin", "Bet +", "Bet -", "Autoplay", "Menu", "Sound", "Turbo")
- Give the bounding box as [x1, y1, x2, y2] in pixel coordinates
- Give the center point as [cx, cy] in pixel coordinates

STRICT RULES:
- Only include buttons you are CERTAIN you can see. Do not guess.
- Do NOT include slot reel symbols, jackpot displays, game logos, or decorative elements
- If a button does not exist in this image, do not include it
- Each button must be a SEPARATE entry — no duplicates
- Bounding boxes must cover the FULL height and width of each button

Return ONLY a JSON array like this:
[
  {"label": "Spin",    "bbox": [x1, y1, x2, y2], "center": [cx, cy]},
  {"label": "Bet +",   "bbox": [x1, y1, x2, y2], "center": [cx, cy]},
  {"label": "Bet -",   "bbox": [x1, y1, x2, y2], "center": [cx, cy]}
]

No markdown. No explanation. Only the JSON array."""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def image_to_base64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def call_local_model(img: Image.Image) -> tuple:
    """Returns (raw_text, sent_w, sent_h)."""
    api_img = copy.deepcopy(img)
    api_img.thumbnail([MAX_IMAGE_SIZE, MAX_IMAGE_SIZE], Image.Resampling.LANCZOS)
    sent_w, sent_h = api_img.size
    print(f"  [API] Sending {sent_w}×{sent_h} image to {MODEL_ID} ...")

    payload = {
        "model": MODEL_ID,
        "temperature": 0.0,         # zero temp = most deterministic
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_to_base64(api_img)}},
                    {"type": "text", "text": DETECTION_PROMPT}
                ]
            }
        ]
    }

    resp = requests.post(LM_STUDIO_URL, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip(), sent_w, sent_h


def parse_json(text: str) -> list:
    """Robustly parse JSON array from model output."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        d = json.loads(text)
        return d if isinstance(d, list) else [d]
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(0))
                return d if isinstance(d, list) else [d]
            except:
                pass
    raise ValueError(f"Cannot parse JSON:\n{text}")


def scale_coords(val_list, sent_w, sent_h, orig_w, orig_h):
    """Scale coordinates from sent-image space back to original image space."""
    sx = orig_w / sent_w
    sy = orig_h / sent_h
    out = []
    for i, v in enumerate(val_list):
        if i % 2 == 0:   # x coordinate
            out.append(int(v * sx))
        else:             # y coordinate
            out.append(int(v * sy))
    return out


def center_dist(a, b):
    """Euclidean distance between two [cx, cy] points."""
    return math.sqrt((a[0] - b[0])**2 + (a[1] - b[1])**2)


def deduplicate(detections, min_dist=40):
    """
    Remove near-duplicate detections whose centers are within min_dist pixels.
    Keeps the first occurrence.
    """
    kept = []
    for item in detections:
        cx, cy = item.get("scaled_center", [0, 0])
        too_close = False
        for k in kept:
            kx, ky = k.get("scaled_center", [0, 0])
            if center_dist([cx, cy], [kx, ky]) < min_dist:
                too_close = True
                break
        if not too_close:
            kept.append(item)
    return kept


def fix_bbox_from_center(bbox, center, orig_w, orig_h):
    """
    If the model gave a very flat/thin bounding box (height < 10px),
    reconstruct a reasonable box around the center point.
    """
    x1, y1, x2, y2 = bbox
    w = x2 - x1
    h = y2 - y1
    cx, cy = center

    # If height is suspiciously small (< 10% of width), rebuild around center
    if h < 10 or h < w * 0.2:
        pad_x = max(w // 2, 15)
        pad_y = pad_x                       # square-ish padding
        x1 = max(0,      cx - pad_x)
        y1 = max(0,      cy - pad_y)
        x2 = min(orig_w, cx + pad_x)
        y2 = min(orig_h, cy + pad_y)

    return [x1, y1, x2, y2]


# ─── Draw ─────────────────────────────────────────────────────────────────────

def draw_detections(original_img, detections):
    orig_w, orig_h = original_img.size
    scale    = max(1, 1920 // orig_w)
    canvas_w = orig_w * scale
    canvas_h = orig_h * scale
    canvas   = original_img.resize((canvas_w, canvas_h), Image.Resampling.LANCZOS)
    draw     = ImageDraw.Draw(canvas)

    try:
        fsize = max(16, canvas_h // 35)
        font  = ImageFont.truetype("arial.ttf", size=fsize)
    except IOError:
        font = ImageFont.load_default()

    colors = [
        "#FF4444", "#44FF44", "#4488FF", "#FF44FF", "#FFFF44",
        "#44FFFF", "#FF8844", "#8844FF", "#FF4488", "#44FF88",
        "#FFAA00", "#00AAFF"
    ]

    for i, item in enumerate(detections):
        label = item.get("label", "?")
        bbox  = item.get("scaled_bbox")
        cx, cy = item.get("scaled_center", [0, 0])
        if not bbox:
            continue

        x1, y1, x2, y2 = [v * scale for v in bbox]
        color = colors[i % len(colors)]
        lw    = max(3, scale * 2)

        draw.rectangle([x1, y1, x2, y2], outline=color, width=lw)

        # Center crosshair
        r = max(4, scale * 3)
        draw.ellipse([cx*scale - r, cy*scale - r, cx*scale + r, cy*scale + r],
                     outline=color, width=lw)

        # Label background
        try:
            tb = draw.textbbox((0, 0), label, font=font)
            tw, th = tb[2] - tb[0], tb[3] - tb[1]
        except:
            tw, th = len(label) * 8, 16

        ly = y1 - th - 8
        if ly < 0:
            ly = y2 + 4

        draw.rectangle([x1, ly - 2, x1 + tw + 10, ly + th + 4], fill=color)
        draw.text((x1 + 5, ly), label, fill="white", font=font)

        print(f"  [{i+1:02d}] {label:30s} center=({cx},{cy})  bbox={bbox}")

    print(f"\n  ✅  Drew {len(detections)} detections  (canvas {canvas_w}×{canvas_h})")
    return canvas


# ─── Main ─────────────────────────────────────────────────────────────────────

def detect_and_draw(input_path: str, output_path: str):
    print(f"\n{'='*60}")
    print(f"  Slot UI Detector  —  Qwen2.5-VL via LM Studio")
    print(f"{'='*60}")
    print(f"  Input : {input_path}")
    print(f"  Output: {output_path}")
    print(f"  Model : {MODEL_ID}")
    print(f"{'='*60}\n")

    # Load original
    try:
        original = Image.open(input_path).convert("RGB")
        orig_w, orig_h = original.size
        print(f"  [OK] Image: {orig_w}×{orig_h} px")
    except Exception as e:
        print(f"  [ERROR] {e}"); sys.exit(1)

    # Call model
    try:
        raw, sent_w, sent_h = call_local_model(original)
    except requests.exceptions.ConnectionError:
        print("  [ERROR] LM Studio not reachable at http://127.0.0.1:1234")
        sys.exit(1)
    except Exception as e:
        print(f"  [ERROR] {e}"); sys.exit(1)

    print(f"\n  [RAW OUTPUT]\n{'-'*40}\n{raw}\n{'-'*40}\n")

    # Parse JSON
    try:
        raw_detections = parse_json(raw)
        print(f"  [OK] Parsed {len(raw_detections)} item(s)\n")
    except ValueError as e:
        print(f"  [ERROR] {e}"); sys.exit(1)

    # Process each detection
    processed = []
    for item in raw_detections:
        label  = item.get("label", "Unknown")

        # Accept both "bbox" and "bbox_2d" key names
        bbox_raw   = item.get("bbox") or item.get("bbox_2d") or []
        center_raw = item.get("center") or []

        if len(bbox_raw) < 4:
            print(f"  [SKIP] '{label}' — missing bbox")
            continue

        # Scale bbox to original resolution
        scaled_bbox = scale_coords(bbox_raw, sent_w, sent_h, orig_w, orig_h)
        x1, y1, x2, y2 = scaled_bbox

        # Clamp to image
        x1 = max(0, min(x1, orig_w)); x2 = max(0, min(x2, orig_w))
        y1 = max(0, min(y1, orig_h)); y2 = max(0, min(y2, orig_h))

        # Scale center if provided, else compute from bbox
        if len(center_raw) >= 2:
            cx = int(center_raw[0] * orig_w / sent_w)
            cy = int(center_raw[1] * orig_h / sent_h)
        else:
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2

        # Fix flat bounding boxes using center as anchor
        fixed_bbox = fix_bbox_from_center([x1, y1, x2, y2], [cx, cy], orig_w, orig_h)

        processed.append({
            "label":          label,
            "scaled_bbox":    fixed_bbox,
            "scaled_center":  [cx, cy],
        })

    # Deduplicate (remove overlapping boxes whose centers are too close)
    before = len(processed)
    processed = deduplicate(processed, min_dist=40)
    removed = before - len(processed)
    if removed:
        print(f"  [NMS] Removed {removed} duplicate detection(s)\n")

    print("  FINAL DETECTIONS:")
    annotated = draw_detections(original, processed)

    # Save
    try:
        annotated.save(output_path, quality=95)
        print(f"\n  [SAVED] → {output_path}")
    except Exception as e:
        print(f"  [ERROR] {e}"); sys.exit(1)

    # Output JSON
    result = [{"label": d["label"], "center_px": d["scaled_center"],
               "bbox_px": d["scaled_bbox"]} for d in processed]
    print("\n  JSON OUTPUT:")
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python detect_slot_buttons.py <screenshot.png> [output.png]")
        sys.exit(0)

    inp  = sys.argv[1]
    outp = sys.argv[2] if len(sys.argv) > 2 else "detected_" + Path(inp).name
    detect_and_draw(inp, outp)
