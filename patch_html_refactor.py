import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Target Card -> Toggles Row + Region Select
target_card_regex = re.compile(r'<div class="target-card">.*?</div>\s*</div>\s*</div>', re.DOTALL)

new_toggles = """
    <!-- ═══ TARGET: BRAND + ENV ═══ -->
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
    </div>
"""
html = target_card_regex.sub(new_toggles.strip(), html)

# 2. Excel Upload Zone -> Smart Input
excel_zone_regex = re.compile(r'<div class="excel-upload-zone" id="excelZone">.*?</div>\s*<input type="file" id="excelFile" accept="\.xlsx,\.xls,\.csv">', re.DOTALL)
html = excel_zone_regex.sub('<input type="file" id="excelFile" accept=".xlsx,.xls,.csv" style="display:none">', html)

game_input_regex = re.compile(r'<input class="field-input" id="gameInput" type="text" autocomplete="off"\s*placeholder="Search a game — type a few letters…">', re.DOTALL)
new_game_input = """
        <div class="smart-input-container" id="smartInputZone">
          <input class="field-input" id="gameInput" type="text" autocomplete="off"
                 placeholder="Search for a game, or drop an .xlsx matrix here...">
          <div class="smart-input-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>
          </div>
        </div>
"""
html = game_input_regex.sub(new_game_input.strip(), html)

# 3. Progressive Disclosure Checks
checks_grid_regex = re.compile(r'<div class="checks-grid" id="checksGrid">\s*<label class="chk"><input type="checkbox" data-cap="core" checked><span>Core spin/wager/balance</span></label>\s*<label class="chk"><input type="checkbox" data-cap="bet" checked><span>Bet \+/- &amp; restore</span></label>\s*<label class="chk"><input type="checkbox" data-cap="autoplay" checked><span>Autoplay</span></label>\s*<label class="chk"><input type="checkbox" data-cap="menu" checked><span>Menu / settings</span></label>\s*<label class="chk"><input type="checkbox" data-cap="paytable" checked><span>Paytable / info</span></label>\s*</div>', re.DOTALL)

new_checks = """
      <div class="checks-grid" id="checksGrid">
        <label class="chk"><input type="checkbox" data-cap="core" checked><span>Core spin/wager/balance</span></label>
        <label class="chk"><input type="checkbox" data-cap="autoplay" checked><span>Autoplay</span></label>
      </div>
      <details class="advanced-checks">
        <summary>Advanced Configurations</summary>
        <div class="checks-grid">
          <label class="chk"><input type="checkbox" data-cap="bet" checked><span>Bet +/- &amp; restore</span></label>
          <label class="chk"><input type="checkbox" data-cap="menu" checked><span>Menu / settings</span></label>
          <label class="chk"><input type="checkbox" data-cap="paytable" checked><span>Paytable / info</span></label>
        </div>
      </details>
"""
html = checks_grid_regex.sub(new_checks.strip(), html)

# JS Updates
# Environment toggle JS
env_js = """
let env='PROD';
document.querySelectorAll('#envToggle .platform-opt').forEach(b=>{
  b.addEventListener('click',()=>{
    env=b.dataset.env;
    document.querySelectorAll('#envToggle .platform-opt').forEach(x=>x.classList.toggle('active',x===b));
  });
});
"""
# Insert before buildRegions()
html = html.replace('function buildRegions(){', env_js + '\nfunction buildRegions(){')

# Region Dropdown JS
old_build_regions = """function buildRegions(){
  const grid=$('regionGrid'); grid.innerHTML='';
  const list=REGIONS[platform]||[];
  if(!list.includes(region)) region=list[0];
  list.forEach(rc=>{
    const m=REGION_META[rc]||{name:rc};
    const b=document.createElement('button');
    b.className='region-chip'+(rc===region?' active':'');
    b.dataset.r=rc; b.title=m.name;
    b.innerHTML=`<span class="rc">${rc}</span><span class="rn">${m.name}</span>`;
    b.addEventListener('click',()=>{
      region=rc;
      document.querySelectorAll('.region-chip').forEach(x=>x.classList.toggle('active',x===b));
      selectedUser=null; $('inputUser').value=''; $('inputPass').value='';
      updateRegionLabel(); updateSaveContext(); loadAccounts();
    });
    grid.appendChild(b);
  });
  updateRegionLabel(); updateSaveContext();
}"""

new_build_regions = """function buildRegions(){
  const select=$('regionSelect'); select.innerHTML='';
  const list=REGIONS[platform]||[];
  if(!list.includes(region)) region=list[0];
  list.forEach(rc=>{
    const m=REGION_META[rc]||{name:rc};
    const opt=document.createElement('option');
    opt.value=rc; opt.textContent=`${rc} — ${m.name}`;
    if(rc===region) opt.selected=true;
    select.appendChild(opt);
  });
  updateRegionLabel(); updateSaveContext();
}
$('regionSelect').addEventListener('change', (e) => {
  region=e.target.value;
  selectedUser=null; $('inputUser').value=''; $('inputPass').value='';
  updateRegionLabel(); updateSaveContext(); loadAccounts();
});"""

html = html.replace(old_build_regions, new_build_regions)

# Smart Input drag and drop JS
old_dnd = """const excelZone = $('excelZone');
const excelInput = $('excelFile');
const gameSelect = $('gameSelect');
const gameCount = $('gameCount');

excelZone.addEventListener('click', () => excelInput.click());
excelZone.addEventListener('dragover', (e) => { e.preventDefault(); excelZone.style.borderColor='var(--purple)'; });
excelZone.addEventListener('dragleave', () => { excelZone.style.borderColor=''; });
excelZone.addEventListener('drop', (e) => {
  e.preventDefault(); excelZone.style.borderColor='';
  if(e.dataTransfer.files.length) handleExcel(e.dataTransfer.files[0]);
});"""

new_dnd = """const smartInputZone = $('smartInputZone');
const excelInput = $('excelFile');
const gameSelect = $('gameSelect');
const gameCount = $('gameCount');
const smartInputIcon = document.querySelector('.smart-input-icon');

smartInputIcon.addEventListener('click', () => excelInput.click()); // Click icon to upload
smartInputZone.addEventListener('dragover', (e) => { e.preventDefault(); smartInputZone.classList.add('drag-over'); });
smartInputZone.addEventListener('dragleave', () => { smartInputZone.classList.remove('drag-over'); });
smartInputZone.addEventListener('drop', (e) => {
  e.preventDefault(); smartInputZone.classList.remove('drag-over');
  if(e.dataTransfer.files.length) handleExcel(e.dataTransfer.files[0]);
});"""

html = html.replace(old_dnd, new_dnd)

# Replace excelZone references in handleExcel
html = html.replace("excelZone.classList.add('loaded');", "smartInputZone.classList.add('loaded');")
html = html.replace("excelZone.querySelector('.excel-upload-text').innerHTML", "gameInput.placeholder = 'Loaded: ' + file.name + ' (' + games.length + ' games)'; //")

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("HTML patched successfully")
