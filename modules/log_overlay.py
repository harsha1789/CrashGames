# modules/log_overlay.py
"""In-page log feed for the game window (Twitch-chat style, top-left corner).

The run's log lines are mirrored onto the live game page itself, so a person
watching the headed browser never has to switch back to the dashboard tab.
Three pieces:

  • FEED    — a stream-like object added to the stdout/stderr _Tee. Every log
              line lands in a pending queue (works from sync print calls).
  • pump()  — background asyncio task that flushes pending lines into the page
              every ~0.3s via page.evaluate. Create-if-missing, so the feed
              survives game navigations/reloads.
  • install_screenshot_guard() — wraps page.screenshot on the Page INSTANCE so
              every capture passes Playwright's `style` option (>=1.41) hiding
              the feed. Gemini therefore never sees a single log pixel — no
              hide/show timing races — while the feed stays permanently visible
              to the human (and in the session video, which records the live
              page). One hook covers every call site in test_spin_button /
              slot_agent / slot_spin, since they all share the same page object.

The feed div is click-transparent (pointer-events: none) and uses its own id —
NOT the .gameguard-highlight class — so clear_highlights() never wipes it.
"""

import time
import asyncio
from collections import deque

FEED_ID = "gameguard-log-feed"
# Applied only WHILE a screenshot is captured (Playwright screenshot(style=...)).
HIDE_FEED_CSS = "#%s { visibility: hidden !important; }" % FEED_ID

MAX_LINE_CHARS = 220   # clamp huge lines (JSON dumps) before shipping to the page
MAX_DOM_LINES = 60     # lines kept in the page DOM (viewport clips to the newest ~20)

_pending = deque(maxlen=400)  # [{t, s}] waiting to be flushed; bounded so a dead page can't leak
_partial = ""                 # trailing chunk not yet terminated by a newline


def _is_noise(line):
    """Separator/blank lines ('='*70 etc.) read fine in a terminal but are pure
    clutter in a 460px feed."""
    stripped = line.strip()
    if not stripped:
        return True
    return all(ch in "=─━-_ \t" for ch in stripped)


def push(data):
    """Accept raw stream chunks (print() writes text and '\\n' separately),
    split into whole lines, and queue them for the page."""
    global _partial
    _partial += data
    while "\n" in _partial:
        line, _partial = _partial.split("\n", 1)
        if _is_noise(line):
            continue
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " …"
        _pending.append({"t": time.strftime("%H:%M:%S"), "s": line})


class _FeedWriter:
    """File-like adapter so the feed can sit inside the existing _Tee unchanged."""
    def write(self, data):
        try:
            push(data)
        except Exception:
            pass
        return len(data)

    def flush(self):
        pass


FEED = _FeedWriter()


_INJECT_JS = """(lines) => {
    let feed = document.getElementById('%(id)s');
    if (!feed) {
        if (!document.body) return;
        feed = document.createElement('div');
        feed.id = '%(id)s';
        Object.assign(feed.style, {
            position: 'fixed', top: '10px', left: '10px',
            width: 'min(460px, 42vw)', maxHeight: '38vh',
            overflow: 'hidden', display: 'flex',
            flexDirection: 'column', justifyContent: 'flex-end',
            background: 'rgba(8, 10, 14, 0.55)',
            borderRadius: '8px', padding: '6px 10px',
            zIndex: '2147483000', pointerEvents: 'none',
            font: '13px/1.45 Consolas, "Courier New", monospace',
            color: '#d7e0ea', textShadow: '0 1px 2px rgba(0,0,0,0.85)',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
        });
        document.body.appendChild(feed);
    }
    for (const {t, s} of lines) {
        const row = document.createElement('div');
        const ts = document.createElement('span');
        ts.textContent = t + ' ';
        ts.style.color = 'rgba(215, 224, 234, 0.45)';
        const msg = document.createElement('span');
        msg.textContent = s;
        if (/❌|✗|\\[FAIL|ERROR|EXCEPTION|Traceback/i.test(s)) msg.style.color = '#ff8a8a';
        else if (/✅|✓|\\[PASS|SUCCESS/i.test(s)) msg.style.color = '#8aff9e';
        else if (/\\[WARN|⚠/i.test(s)) msg.style.color = '#ffd479';
        else if (/^\\[(SETUP|TEST|AGENT)/i.test(s)) msg.style.color = '#8ecbff';
        row.appendChild(ts);
        row.appendChild(msg);
        feed.appendChild(row);
    }
    while (feed.childNodes.length > %(max)d) feed.removeChild(feed.firstChild);
}""" % {"id": FEED_ID, "max": MAX_DOM_LINES}


async def pump(page, interval=0.3):
    """Flush pending lines to the page every `interval`s. Best-effort: a failed
    evaluate (mid-navigation) requeues the batch for the next tick; a closed
    page ends the task. Run as: asyncio.create_task(log_overlay.pump(page))."""
    while True:
        await asyncio.sleep(interval)
        try:
            if page.is_closed():
                return
        except Exception:
            return
        if not _pending:
            continue
        batch = []
        while _pending:
            batch.append(_pending.popleft())
        try:
            await page.evaluate(_INJECT_JS, batch)
        except Exception:
            _pending.extendleft(reversed(batch))  # retry after the navigation settles


def install_screenshot_guard(page):
    """Make EVERY page.screenshot() hide the feed for the duration of the
    capture, so screenshots fed to Gemini (and report/hero shots) stay clean."""
    orig = page.screenshot

    async def screenshot(**kwargs):
        style = kwargs.get("style")
        kwargs["style"] = (style + "\n" + HIDE_FEED_CSS) if style else HIDE_FEED_CSS
        return await orig(**kwargs)

    page.screenshot = screenshot
