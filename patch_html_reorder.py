import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

old_block = """    <!-- ═══ TARGET: BRAND + ENV ═══ -->
    <div class="toggles-row">
      <div class="platform-toggle">
        <button class="platform-opt active" data-p="betway">Betway</button>
        <button class="platform-opt" data-p="jackpotcity">JackpotCity</button>
      </div>
      <div class="platform-toggle" id="envToggle">
        <button class="platform-opt active" data-env="prod">PROD</button>
        <button class="platform-opt" data-env="uat">UAT</button>
      </div>
    </div>

    <div class="target-row" style="margin-bottom: 18px;">
      <div class="target-label" style="margin-bottom: 8px;">Region <span class="region-active" id="regionActive"></span></div>
      <select id="regionSelect" class="premium-select"></select>
    </div>"""

# Replace exact block
# Wait, the HTML comment has different symbols because of encoding issues in powershell output.
# Let's use regex to find and replace it.

regex = re.compile(r'<!--.*?TARGET: BRAND \+ ENV.*?</div>\s*</div>\s*<div class="target-row".*?<select id="regionSelect" class="premium-select"></select>\s*</div>', re.DOTALL)

new_block = """    <!-- ═══ TARGET: PLATFORM -> REGION -> ENV ═══ -->
    <div class="target-card" style="border:none; box-shadow:none; padding:0; background:transparent;">
      <div class="target-row">
        <div class="target-label">Platform</div>
        <div class="platform-toggle">
          <button class="platform-opt active" data-p="betway">Betway</button>
          <button class="platform-opt" data-p="jackpotcity">JackpotCity</button>
        </div>
      </div>
      
      <div class="target-row">
        <div class="target-label" style="margin-bottom: 4px;">Region <span class="region-active" id="regionActive" style="display:none;"></span></div>
        <select id="regionSelect" class="premium-select"></select>
      </div>

      <div class="target-row">
        <div class="target-label">Environment</div>
        <div class="platform-toggle" id="envToggle">
          <button class="platform-opt active" data-env="prod">PROD</button>
          <button class="platform-opt" data-env="uat">UAT</button>
        </div>
      </div>
    </div>"""

if regex.search(html):
    html = regex.sub(new_block, html)
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("HTML patched successfully")
else:
    print("Could not find the block to replace")
