import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Replace renderReport and buildReport
old_js = """function renderReport(results) {
  const r = $('report');
  r.innerHTML = '';
  if (!results || !results.length) return;
  
  const passed = results.filter(x => x.passed === true).length;
  const failed = results.filter(x => x.passed === false).length;
  
  let grade = 'A'; let gClass = 'a';
  if (failed > 0) { grade = 'F'; gClass = 'f'; }
  else if (passed < results.length) { grade = 'B'; gClass = 'b'; }

  let html = `<div class="report-grade ${gClass}">${grade}</div>
              <div class="report-summary">${passed} passed, ${failed} failed checks</div>
              <div class="report-items">`;
              
  results.forEach(res => {
    const dotClass = res.passed === true ? 'pass' : (res.passed === false ? 'fail' : '');
    html += `<div class="ri">
               <div class="ri-dot ${dotClass}"></div>
               <div class="ri-body">
                 <div class="ri-name">${res.name}</div>
                 <div class="ri-detail">${res.details || ''}</div>
               </div>
             </div>`;
  });
  html += `</div><div class="report-footer">Automated Slot Verification</div>`;
  
  r.innerHTML = html;
  r.classList.add('visible');
}

function fin(){
  if(eventSource)eventSource.close();eventSource=null;isRunning=false;
  $('launchBtn').textContent='Launch Test';$('launchBtn').classList.remove('running');
  addLog('Done.','');loadAccounts();
}

async function buildReport(){try{const r=await fetch('/api/results');const res=await r.json();if(!res.length)return;let pass=0,fail=0;res.forEach(x=>x.passed?pass++:fail++);const tot=pass+fail,pct=Math.round(pass/tot*100);let g='F',gc='f';if(pct>=90){g='A';gc='a';}else if(pct>=75){g='B';gc='b';}else if(pct>=50){g='C';gc='c';}let h=`<div class="report-grade ${gc}">${g}</div><div class="report-summary">${pass}/${tot} passed · ${getGameName()}</div><div class="report-items">`;res.forEach(x=>{const ok=x.passed===true;h+=`<div class="ri"><div class="ri-dot ${ok?'pass':'fail'}"></div><div class="ri-body"><div class="ri-name">${x.name}</div><div class="ri-detail">${x.details||''}</div></div></div>`;});h+=`</div><div class="report-footer">${new Date().toLocaleString()} · Melon</div>`;$('report').innerHTML=h;setTimeout(()=>$('report').classList.add('visible'),100);}catch{}}"""

new_js = """function renderReport(results) {
  const r = $('report');
  r.innerHTML = '';
  if (!results || !results.length) return;
  
  const passed = results.filter(x => x.passed === true).length;
  const failed = results.filter(x => x.passed === false).length;

  let html = `<div class="report-summary">${passed} passed, ${failed} failed checks</div>
              <div class="report-items">`;
              
  results.forEach(res => {
    const dotClass = res.passed === true ? 'pass' : (res.passed === false ? 'fail' : '');
    html += `<div class="ri" style="flex-direction: column; align-items: stretch; margin-bottom: 20px; padding: 15px; border: 1px solid var(--border); border-radius: 8px;">
               <div style="display: flex; align-items: center; margin-bottom: 10px;">
                   <div class="ri-dot ${dotClass}"></div>
                   <div class="ri-name" style="margin-left: 10px; font-weight: bold; font-size: 1.1em; color: var(--text-1);">${res.name}</div>
               </div>
               <div class="ri-detail" style="margin-left: 20px; margin-bottom: 15px; color: var(--text-2);">${res.details || ''}</div>`;
               
    if (res.screenshot) {
       html += `<div style="margin-left: 20px; margin-bottom: 15px;"><div style="font-size: 0.8em; color: var(--text-3); margin-bottom: 5px;">Screenshot:</div><img src="/screenshots/${res.screenshot}" style="max-width: 100%; border-radius: 4px; border: 1px solid var(--border);"></div>`;
    }
    
    if (res.video) {
       html += `<div style="margin-left: 20px;"><div style="font-size: 0.8em; color: var(--text-3); margin-bottom: 5px;">Video:</div><video src="/recordings/${res.video}" controls style="max-width: 100%; border-radius: 4px; border: 1px solid var(--border);"></video></div>`;
    }
    
    html += `</div>`;
  });
  html += `</div><div class="report-footer">Automated Slot Verification</div>`;
  
  r.innerHTML = html;
  r.classList.add('visible');
}

function fin(){
  if(eventSource)eventSource.close();eventSource=null;isRunning=false;
  $('launchBtn').textContent='Launch Test';$('launchBtn').classList.remove('running');
  addLog('Done.','');loadAccounts();
}

async function buildReport(){
  try {
    const r=await fetch('/api/results');
    const res=await r.json();
    if(!res.length)return;
    renderReport(res);
  } catch{}
}"""

if old_js in html:
    html = html.replace(old_js, new_js)
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("Patched index.html")
else:
    print("Could not find the old_js block in index.html!")
