import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Update envToggle JS to trigger account refresh
old_env_js = """let env='PROD';
document.querySelectorAll('#envToggle .platform-opt').forEach(b=>{
  b.addEventListener('click',()=>{
    env=b.dataset.env;
    document.querySelectorAll('#envToggle .platform-opt').forEach(x=>x.classList.toggle('active',x===b));
  });
});"""

new_env_js = """let env='PROD';
document.querySelectorAll('#envToggle .platform-opt').forEach(b=>{
  b.addEventListener('click',()=>{
    env=b.dataset.env;
    document.querySelectorAll('#envToggle .platform-opt').forEach(x=>x.classList.toggle('active',x===b));
    selectedUser=null; $('inputUser').value=''; $('inputPass').value='';
    updateSaveContext(); loadAccounts();
  });
});"""
html = html.replace(old_env_js, new_env_js)

# 2. Update save context to include env
old_save_ctx = "Saving to <strong>${brandLabel(platform)}</strong> A ${region} ${m.name}"
# Due to encoding issues with the dot, let's use regex
html = re.sub(r'Saving to <strong>\$\{brandLabel\(platform\)\}</strong>.*?(\$\{m\.name\})`', r'Saving to <strong>${brandLabel(platform)}</strong> ${region} ${m.name} (${env})`', html)

# 3. Update loadAccounts to filter by env
html = re.sub(r'const accs=allAccs\.filter\(a=>\(a\.brand\|\|\'betway\'\)===platform && \(a\.region\|\|\'ZA\'\)===region\);',
              r"const accs=allAccs.filter(a=>(a.brand||'betway')===platform && (a.region||'ZA')===region && (a.env||'PROD')===env);", html)
              
html = re.sub(r'No accounts for \$\{brandLabel\(platform\)\}.*?\$\{region\}\. Add one',
              r'No accounts for ${brandLabel(platform)} ${region} (${env}). Add one', html)

# 4. Update save account fetch
html = re.sub(r'body:JSON\.stringify\(\{username:u,password:p,brand:platform,region\}\)',
              r'body:JSON.stringify({username:u,password:p,brand:platform,region,env})', html)

# 5. Update delete account fetch (if needed, but delete usually uses query params, I'll skip it unless it's in URL)
html = re.sub(r'fetch\(`/api/accounts/\$\{encodeURIComponent\(a\.username\)\}\?brand=\$\{a\.brand\|\|\'betway\'\}&region=\$\{a\.region\|\|\'ZA\'\}`',
              r"fetch(`/api/accounts/${encodeURIComponent(a.username)}?brand=${a.brand||'betway'}&region=${a.region||'ZA'}&env=${a.env||'PROD'}`", html)

# 6. Update fetchBal fetch
# Wait, fetchBal replaces brand:platform,region with brand:platform,region,env
# This was already covered in step 4 if I use a general replace for POST bodies.
# Let's just do it directly.
html = html.replace('body:JSON.stringify({username:u,password:p,brand:platform,region})', 'body:JSON.stringify({username:u,password:p,brand:platform,region,env:env})')

# 7. Update launch fetch
html = html.replace('body:JSON.stringify({game:g,username:u,password:p,mobile:false,brand:platform,region:region,default_bet:dbet,min_bet:mbet,tests:tests})',
                    'body:JSON.stringify({game:g,username:u,password:p,mobile:false,brand:platform,region:region,env:env,default_bet:dbet,min_bet:mbet,tests:tests})')


with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("JS env logic patched successfully")
