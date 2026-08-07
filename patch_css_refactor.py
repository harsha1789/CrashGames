import re

with open('static/style.css', 'r', encoding='utf-8') as f:
    css = f.read()

# 1. Macro Layout Split
css = re.sub(r'(\.arena\s*\{\s*width:\s*)60%', r'\g<1>50%', css)
css = re.sub(r'(\.controls-panel\s*\{\s*width:\s*)40%', r'\g<1>50%', css)

# 2. Account Management Compression
css = re.sub(r'(\.acc-row\s*\{[^}]*padding:\s*)10px 12px', r'\g<1>8px 12px', css)
css = re.sub(r'(\.acc-avatar\s*\{[^}]*width:\s*)30px([^}]*height:\s*)30px([^}]*font-size:\s*)11px', r'\g<1>24px\g<2>24px\g<3>9px', css)

# Add new styles
new_styles = """
/* ═══ ADDED STYLES FOR UI REFACTOR ═══ */

/* Top Row: Command Toggles */
.toggles-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}
.toggles-row .platform-toggle {
  flex: 1;
  margin-bottom: 0;
}

/* Second Row: Region Dropdown */
.premium-select {
  width: 100%;
  padding: 12px 14px;
  background: white;
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  outline: none;
  transition: all 0.2s;
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath d='M6 8L1 3h10z' fill='%23a89e8e'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 14px center;
}
.premium-select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(225,29,72,0.06);
}

/* Fourth Row: Smart Input */
.smart-input-container {
  position: relative;
  display: flex;
  align-items: center;
  transition: all 0.25s;
  border-radius: 8px;
  margin-bottom: 10px;
}
.smart-input-container .field-input {
  padding-right: 40px;
  border: 1px dashed var(--border);
}
.smart-input-container.drag-over .field-input {
  border-color: var(--gold);
  border-style: solid;
  background: var(--green-dim);
}
.smart-input-icon {
  position: absolute;
  right: 14px;
  color: var(--text-3);
  pointer-events: none;
}
.smart-input-container.loaded .field-input {
  border-color: var(--green);
  border-style: solid;
  color: var(--green);
}
.smart-input-container.loaded .smart-input-icon {
  color: var(--green);
}

/* Bottom Row: Progressive Disclosure Checks */
.advanced-checks {
  margin-top: 10px;
  margin-bottom: 10px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-surface);
  overflow: hidden;
}
.advanced-checks summary {
  list-style: none;
  padding: 10px 14px;
  font-size: 12px;
  font-weight: 700;
  color: var(--text-2);
  cursor: pointer;
  user-select: none;
  transition: background 0.2s;
}
.advanced-checks summary::-webkit-details-marker {
  display: none;
}
.advanced-checks summary:hover {
  background: rgba(225,29,72,0.03);
  color: var(--accent);
}
.advanced-checks[open] summary {
  border-bottom: 1px solid var(--border);
}
.advanced-checks .checks-grid {
  padding: 12px 14px;
}

"""

if '/* ═══ ADDED STYLES FOR UI REFACTOR ═══ */' not in css:
    css += new_styles

with open('static/style.css', 'w', encoding='utf-8') as f:
    f.write(css)

print("CSS updated successfully.")
