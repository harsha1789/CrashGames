import sys

with open('test_spin_button.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
in_core = False

for i, line in enumerate(lines):
    if line.strip() == 'GATED_CAPS = ("bet", "autoplay", "menu", "paytable")':
        new_lines.append('GATED_CAPS = ("core", "bet", "autoplay", "menu", "paytable")\n')
        continue

    # Start of core checks
    if line.strip() == '# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━' and i+1 < len(lines) and 'TEST 3: Single spin click' in lines[i+1]:
        new_lines.append('        if _cap_on("core"):\n')
        in_core = True

    if in_core:
        if line.strip() == '# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━' and i+1 < len(lines) and 'TEST 6: Bet can be changed' in lines[i+1]:
            in_core = False

    if in_core:
        # Add 4 spaces to indent, except for empty lines
        if line.strip() == '':
            new_lines.append(line)
        else:
            new_lines.append('    ' + line)
    else:
        new_lines.append(line)

with open('test_spin_button.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Patched test_spin_button.py')
