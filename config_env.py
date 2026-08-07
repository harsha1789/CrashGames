"""
config_env.py — single source of truth for the LIVE render viewport + coordinate scaling.
================================================================================
Every vision box is normalized [0,1000] as [ymin, xmin, ymax, xmax]; a CSS click pixel is
`coord/1000 * VIEWPORT_DIM`. Keeping the viewport HERE — and reading it by ATTRIBUTE
(`config_env.VIEWPORT_WIDTH`, never `from config_env import VIEWPORT_WIDTH`) — eliminates the old
double-import hazard: there is exactly ONE module object, so the mutation `run_tests` makes after
the page loads is seen by every importer (slot_agent, slot_explore, test_spin_button).

This module is also the shared coordinate-clamping utility (Step 3): every function that turns a
box into a Playwright click coordinate routes through `norm_to_css` / `clamp_point` here, so no
target can ever exceed the live viewport bounds.
"""

# Live render area. Defaults to the fixed desktop surface run_tests launches (1366x768) so that
# any import-time scaling before run_tests sets the real size is already in the right ballpark.
VIEWPORT_WIDTH = 1366
VIEWPORT_HEIGHT = 768

# HARD betting-safety cap (team rule, 2026-07-09): no automated flow may ever spin with a stake
# above this — period. Root incident: at min stake Thor's Rage renders its "-" disabled/near-
# invisible, vision pinned "Bet Decrement" onto the "+", and 16 blind flooring clicks rode the
# stake ladder to its 300 max before spinning. Enforced at the last gate before every spin click
# (slot_spin.spin_and_measure), before autoplay starts (slot_agent.drive_autoplay), and at DSC's
# pre-spin check (slot_dsc.run_dsc).
MAX_STAKE = 50.0


def set_viewport(width, height):
    """Mutate the live viewport. Called once by run_tests after the Playwright page is created.
    No-ops on falsy/zero dims. Returns the resulting (W, H)."""
    global VIEWPORT_WIDTH, VIEWPORT_HEIGHT
    if width and height:
        VIEWPORT_WIDTH, VIEWPORT_HEIGHT = int(width), int(height)
    return VIEWPORT_WIDTH, VIEWPORT_HEIGHT


def clamp_point(x, y):
    """Hard-clamp a CSS pixel target into [0, W-1] x [0, H-1] of the live viewport (Step 3)."""
    return (max(0, min(int(x), VIEWPORT_WIDTH - 1)),
            max(0, min(int(y), VIEWPORT_HEIGHT - 1)))


def norm_to_css(x_norm, y_norm, warn=True):
    """Normalized [0,1000] (x, y) -> CLAMPED CSS click px. Warns if it had to clamp — the classic
    symptom of a mis-scaled box or a background element that bled in below the panel."""
    x = int(x_norm / 1000.0 * VIEWPORT_WIDTH)
    y = int(y_norm / 1000.0 * VIEWPORT_HEIGHT)
    cx, cy = clamp_point(x, y)
    if warn and (x, y) != (cx, cy):
        print(f"    [WARN] target ({x},{y}) outside viewport {VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}; clamped to ({cx},{cy})")
    return (cx, cy)


def norm_rect(box):
    """box [ymin,xmin,ymax,xmax] (0-1000) -> (left, top, right, bottom) CLAMPED CSS px, or None.
    Defensive: tolerates extra/short/garbage values — never raises."""
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None
    ymin, xmin, ymax, xmax = box[0], box[1], box[2], box[3]
    try:
        l, t = norm_to_css(xmin, ymin, warn=False)
        r, b = norm_to_css(xmax, ymax, warn=False)
    except (TypeError, ValueError):
        return None
    if r < l:
        l, r = r, l
    if b < t:
        t, b = b, t
    return (l, t, r, b)


def norm_box_center(box):
    """box [ymin,xmin,ymax,xmax] (0-1000) -> CLAMPED CSS pixel center, or None."""
    rect = norm_rect(box)
    if rect is None:
        return None
    l, t, r, b = rect
    return ((l + r) // 2, (t + b) // 2)
