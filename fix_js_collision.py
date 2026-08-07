import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# 1. Add id="platformToggle" to the first platform-toggle
html = html.replace('<div class="platform-toggle">', '<div class="platform-toggle" id="platformToggle">', 1)

# 2. Update the JS listener for platform
# The old code: document.querySelectorAll('.platform-opt').forEach(b=>{
# We want to change it to: document.querySelectorAll('#platformToggle .platform-opt').forEach(b=>{
html = html.replace("document.querySelectorAll('.platform-opt').forEach(b=>{", "document.querySelectorAll('#platformToggle .platform-opt').forEach(b=>{")

# Inside the listener, it also does:
# document.querySelectorAll('.platform-opt').forEach(x=>x.classList.toggle('active',x===b));
# We need to change that to:
html = html.replace("document.querySelectorAll('.platform-opt').forEach(x=>x.classList.toggle('active',x===b));", "document.querySelectorAll('#platformToggle .platform-opt').forEach(x=>x.classList.toggle('active',x===b));")

# Let's verify we also need to fix the add account POST payload which missed the env tag:
# await fetch('/api/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label,username:u,password:p,brand:platform,region})});
html = re.sub(r'body:JSON\.stringify\(\{label,username:u,password:p,brand:platform,region\}\)',
              r'body:JSON.stringify({label,username:u,password:p,brand:platform,region,env:env})', html)

# And fix the display of add account
# If it was saved with 'undefined', the user might have bad data, but restarting/reloading fixes the UI state.

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(html)

print("Fixed JS listener collision")
