import sys

with open('static/style.css', 'r', encoding='utf-8') as f:
    content = f.read()

index = content.find('.report {')
if index != -1:
    content = content[:index]

new_css = """
/* ═══ ARENA REPORT ═══ */
.arena-report {
  position: absolute;
  top: 40px; left: 40px; right: 40px; bottom: 40px;
  background: rgba(255,255,255,0.7);
  backdrop-filter: blur(20px);
  border-radius: 20px;
  border: 1px solid rgba(255,255,255,0.8);
  box-shadow: 0 20px 60px rgba(0,0,0,0.08);
  padding: 30px;
  overflow-y: auto;
  opacity: 0;
  transform: translateY(20px);
  pointer-events: none;
  transition: all 0.6s cubic-bezier(0.22, 1, 0.36, 1);
  display: flex;
  flex-direction: column;
  z-index: 100;
}

.arena-report.visible {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}

.arena-report::-webkit-scrollbar { width: 8px; }
.arena-report::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.15); border-radius: 4px; }

.report-summary {
  text-align: center;
  margin-bottom: 30px;
}
.report-summary h2 {
  font-size: 28px;
  font-weight: 800;
  color: var(--text);
  margin-bottom: 4px;
}
.report-summary p {
  font-size: 14px;
  color: var(--text-2);
}

.report-items-grid {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.arena-ri {
  background: white;
  border-radius: 12px;
  border: 1px solid var(--border);
  padding: 20px;
  box-shadow: 0 4px 15px rgba(0,0,0,0.02);
}

.arena-ri-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.arena-ri-badge {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
}
.arena-ri-badge.pass { background: rgba(5,150,105,0.1); color: var(--green); }
.arena-ri-badge.fail { background: rgba(225,29,72,0.1); color: var(--accent); }
.arena-ri-badge.skip { background: rgba(168,158,142,0.1); color: var(--text-3); }

.arena-ri-name {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
}

.arena-ri-detail {
  font-size: 13px;
  color: var(--text-2);
  margin-bottom: 16px;
  line-height: 1.5;
}

.arena-ri-media {
  display: flex;
  gap: 16px;
}

.media-box {
  flex: 1;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
}

.media-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-3);
  margin-bottom: 8px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.media-box img, .media-box video {
  width: 100%;
  border-radius: 4px;
  border: 1px solid var(--border);
}
"""
with open('static/style.css', 'w', encoding='utf-8') as f:
    f.write(content + new_css)
print("CSS Updated")
