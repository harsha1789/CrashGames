import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# The regex accidentally deleted everything from `<div class="split">` up to `<!-- ═══ TARGET...`.
# We need to find `<!-- ═══ TARGET: PLATFORM -> REGION -> ENV ═══ -->` and prepend the missing HTML.

missing_html = """
  <!-- ═══ LEFT — ARENA ═══ -->
  <div class="arena" id="arena">

    <div class="arena-title" id="arenaTitle">Melon</div>

    <!-- Actual Slot Machine -->
    <div class="slot-frame" id="slotFrame">
      <div class="reel"><div class="reel-strip r-down">
        <div class="sym">🍒</div><div class="sym">🍋</div><div class="sym">🍉</div><div class="sym">🔔</div><div class="sym">💎</div>
        <div class="sym">🍒</div><div class="sym">🍋</div><div class="sym">🍉</div><div class="sym">🔔</div><div class="sym">💎</div>
      </div></div>
      <div class="reel"><div class="reel-strip r-up">
        <div class="sym">⭐</div><div class="sym">🍒</div><div class="sym">💎</div><div class="sym">🍋</div><div class="sym">🔔</div>
        <div class="sym">⭐</div><div class="sym">🍒</div><div class="sym">💎</div><div class="sym">🍋</div><div class="sym">🔔</div>
      </div></div>
      <div class="reel"><div class="reel-strip r-down-slow">
        <div class="sym">🍉</div><div class="sym">🔔</div><div class="sym">🍒</div><div class="sym">⭐</div><div class="sym">🍋</div>
        <div class="sym">🍉</div><div class="sym">🔔</div><div class="sym">🍒</div><div class="sym">⭐</div><div class="sym">🍋</div>
      </div></div>
      <div class="reel"><div class="reel-strip r-up-fast">
        <div class="sym">💎</div><div class="sym">🍉</div><div class="sym">⭐</div><div class="sym">🍒</div><div class="sym">🔔</div>
        <div class="sym">💎</div><div class="sym">🍉</div><div class="sym">⭐</div><div class="sym">🍒</div><div class="sym">🔔</div>
      </div></div>
      <div class="reel"><div class="reel-strip r-down-med">
        <div class="sym">🍋</div><div class="sym">🍒</div><div class="sym">🔔</div><div class="sym">💎</div><div class="sym">🍉</div>
        <div class="sym">🍋</div><div class="sym">🍒</div><div class="sym">🔔</div><div class="sym">💎</div><div class="sym">🍉</div>
      </div></div>
    </div>

    <div class="arena-subtitle">Slot Testing Engine</div>

    <!-- Live Arena Log (visible on launch) -->
    <div class="arena-live-log" id="arenaLog"></div>

    <!-- expansive arena report -->
    <div class="arena-report" id="report"></div>
  </div>

  <!-- ═══ RIGHT — CONTROLS ═══ -->
  <div class="controls-panel">

    <div class="tagline">
      <span class="tagline-static">Automate.</span>
      <span class="typewriter-line" id="tw"></span>
    </div>

"""

if 'class="arena"' not in html:
    html = html.replace('<!-- ═══ TARGET: PLATFORM -> REGION -> ENV ═══ -->', missing_html + '    <!-- ═══ TARGET: PLATFORM -> REGION -> ENV ═══ -->')
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("Fixed HTML structure")
else:
    print("HTML already contains arena")
