import sys
import re

with open('slot_agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
in_func = False

for i, line in enumerate(lines):
    if line.strip() == 'it["center"] = _center(it.get("box_2d"))   # defensive -> None if bad/extra values' and 'def describe_panel_options' not in line:
        # We found the insertion point in describe_panel_options
        indent = line[:len(line) - len(line.lstrip())]
        new_lines.append(indent + '# Deduplicate: if boxes overlap significantly (IoU > 0.2) or are horizontally adjacent with similar names\n')
        new_lines.append(indent + 'is_dup = False\n')
        new_lines.append(indent + 'if isinstance(box, (list, tuple)) and len(box) >= 4:\n')
        new_lines.append(indent + '    lbl = (it.get("label") or "").lower()\n')
        new_lines.append(indent + '    cy = (box[0] + box[2]) / 2.0\n')
        new_lines.append(indent + '    for k in out:\n')
        new_lines.append(indent + '        kbox = k.get("box_2d")\n')
        new_lines.append(indent + '        if not isinstance(kbox, (list, tuple)) or len(kbox) < 4: continue\n')
        new_lines.append(indent + '        iou = T._iou(box, kbox)\n')
        new_lines.append(indent + '        klbl = (k.get("label") or "").lower()\n')
        new_lines.append(indent + '        kcy = (kbox[0] + kbox[2]) / 2.0\n')
        new_lines.append(indent + '        same_y = abs(cy - kcy) < 50\n')
        new_lines.append(indent + '        name_match = (lbl in klbl or klbl in lbl) and len(lbl) > 2\n')
        new_lines.append(indent + '        if iou > 0.2 or (same_y and name_match):\n')
        new_lines.append(indent + '            is_dup = True\n')
        new_lines.append(indent + '            break\n')
        new_lines.append(indent + 'if is_dup:\n')
        new_lines.append(indent + '    dropped.append(f"{it.get(\'label\')} (duplicate)")\n')
        new_lines.append(indent + '    continue\n')
    
    new_lines.append(line)

with open('slot_agent.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Patched slot_agent.py')
