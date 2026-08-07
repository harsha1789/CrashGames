import re
import time

with open('test_spin_button.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

clean_lines = []
for line in lines:
    if ".video_start = " in line or ".video_end = " in line or "context_start_time = time.time()" in line or "t_now = " in line:
        continue
    clean_lines.append(line)

out = []
for line in clean_lines:
    indent = line[:len(line) - len(line.lstrip())]
    
    if 'print(f"  TEST 1:' in line:
        out.append(f"{indent}t1.video_start = time.time() - context_start_time\n")
    if 'print(f"  TEST 2:' in line:
        out.append(f"{indent}t2.video_start = time.time() - context_start_time\n")
    if 'print(f"  TEST 3:' in line:
        out.append(f"{indent}t3.video_start = time.time() - context_start_time\n")
        out.append(f"{indent}t_wager.video_start = t3.video_start\n")
        out.append(f"{indent}t_payout.video_start = t3.video_start\n")
        out.append(f"{indent}t_feat.video_start = t3.video_start\n")
        out.append(f"{indent}t_bal.video_start = t3.video_start\n")
    if 'print(f"  TEST 4:' in line:
        out.append(f"{indent}t4.video_start = time.time() - context_start_time\n")
        out.append(f"{indent}t5.video_start = t4.video_start\n")
    if 'print(f"  TEST 6:' in line:
        out.append(f"{indent}t6.video_start = time.time() - context_start_time\n")
        out.append(f"{indent}t7.video_start = t6.video_start\n")
        out.append(f"{indent}t8.video_start = t6.video_start\n")
    if 'print(f"\\n{\'=\'*70}\\n  TEST: Auto Play Trigger' in line:
        out.append(f"{indent}t_auto.video_start = time.time() - context_start_time\n")
    if 'print(f"  TEST 10:' in line:
        out.append(f"{indent}t10.video_start = time.time() - context_start_time\n")
        
    out.append(line)

    if 'page = await context.new_page()' in line:
        out.append(f"{indent}context_start_time = time.time()\n")
        
    if 'results.append(t1)' in line:
        out.append(f"{indent}t1.video_end = time.time() - context_start_time\n")
    if 'results.append(t2)' in line:
        out.append(f"{indent}t2.video_end = time.time() - context_start_time\n")
    if 'results.append(t3)' in line:
        out.append(f"{indent}t_now = time.time() - context_start_time\n")
        out.append(f"{indent}t3.video_end = t_now\n{indent}t_wager.video_end = t_now\n{indent}t_payout.video_end = t_now\n{indent}t_feat.video_end = t_now\n{indent}t_bal.video_end = t_now\n")
    if 'results.append(t5)' in line:
        out.append(f"{indent}t_now = time.time() - context_start_time\n")
        out.append(f"{indent}t4.video_end = t_now\n{indent}t5.video_end = t_now\n")
    if 'results.append(t8)' in line:
        out.append(f"{indent}t_now = time.time() - context_start_time\n")
        out.append(f"{indent}t6.video_end = t_now\n{indent}t7.video_end = t_now\n{indent}t8.video_end = t_now\n")
    if 'results.append(t_auto)' in line:
        out.append(f"{indent}t_auto.video_end = time.time() - context_start_time\n")
    if 'results.append(t10)' in line:
        out.append(f"{indent}t10.video_end = time.time() - context_start_time\n")

with open('test_spin_button.py', 'w', encoding='utf-8') as f:
    f.writelines(out)
print("Time tracking injected successfully!")
