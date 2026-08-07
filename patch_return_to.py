import sys

with open('slot_agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
in_return_to = False

for i, line in enumerate(lines):
    if line.startswith('async def _return_to('):
        in_return_to = True
        
    if in_return_to and 'close_el = _find_close(_describe_merged(chk, passes=1))' in line:
        indent = line[:len(line) - len(line.lstrip())]
        new_lines.append(indent + 'close_el = _find_close(_describe_merged(chk, passes=1))\n')
        new_lines.append(indent + 'if not close_el:\n')
        new_lines.append(indent + '    # Fallback: general detector is better at finding small Xs on modal edges\n')
        new_lines.append(indent + '    try:\n')
        new_lines.append(indent + '        close_el = _find_close(T.detect_controls_merged(Image.open(chk), passes=1))\n')
        new_lines.append(indent + '    except Exception:\n')
        new_lines.append(indent + '        pass\n')
        continue
        
    if in_return_to and 'if reopen:' in line:
        indent = line[:len(line) - len(line.lstrip())]
        new_lines.append(indent + 'if reopen:\n')
        new_lines.append(indent + '    await reopen()\n')
        new_lines.append(indent + '    # For modals that closed the side-menu, reopen() brings it back.\n')
        new_lines.append(indent + '    # Verify if we actually successfully restored the menu state to save a rescan.\n')
        new_lines.append(indent + '    final_chk = os.path.join(ss_dir, f"{tag}_retfinal.png"); await page.screenshot(path=final_chk)\n')
        new_lines.append(indent + '    if slot_spin.frame_motion(base_shot, final_chk) < CLOSE_DELTA:\n')
        new_lines.append(indent + '        return True\n')
        new_lines.append(indent + 'return False\n')
        in_return_to = False
        continue
        
    if in_return_to and 'await reopen()' in line and not 'if reopen:' in line:
        # We handled it above
        continue
        
    if in_return_to and 'return False' in line:
        # We handled it above
        continue
        
    new_lines.append(line)

with open('slot_agent.py', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print("Patched _return_to in slot_agent.py")
