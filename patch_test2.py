import re
import os

with open('test_spin_button.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Update TestResult class
old_class = """class TestResult:
    def __init__(self, name: str, screenshot: str = ""):
        self.name = name
        self.passed = None
        self.details = ""
        self.screenshot = screenshot
        self.video = ""

    def __str__(self):"""
new_class = """class TestResult:
    def __init__(self, name: str, screenshot: str = ""):
        self.name = name
        self.passed = None
        self.details = ""
        self.screenshot = screenshot
        self.video = ""
        self.video_start = 0.0
        self.video_end = 0.0

    def __str__(self):"""
code = code.replace(old_class, new_class)

# 2. Add Helpers
helpers = """
async def draw_highlight(page, xmin, ymin, xmax, ymax, text, color="lime"):
    try:
        await page.evaluate('''([x, y, w, h, text, color]) => {
            const el = document.createElement('div');
            el.className = 'melon-highlight';
            el.style.position = 'absolute';
            el.style.left = x + 'px';
            el.style.top = y + 'px';
            el.style.width = w + 'px';
            el.style.height = h + 'px';
            el.style.border = '4px solid ' + color;
            el.style.backgroundColor = 'rgba(0, 255, 0, 0.2)';
            el.style.zIndex = '999999';
            el.style.pointerEvents = 'none';
            const label = document.createElement('div');
            label.innerText = text;
            label.style.position = 'absolute';
            label.style.top = '-30px';
            label.style.left = '0';
            label.style.backgroundColor = color;
            label.style.color = '#fff';
            label.style.padding = '4px 8px';
            label.style.fontSize = '16px';
            label.style.fontWeight = 'bold';
            label.style.borderRadius = '4px';
            label.style.whiteSpace = 'nowrap';
            el.appendChild(label);
            document.body.appendChild(el);
        }''', [xmin, ymin, xmax - xmin, ymax - ymin, text, color])
    except Exception:
        pass

async def clear_highlights(page):
    try:
        await page.evaluate('''() => {
            const els = document.querySelectorAll('.melon-highlight');
            els.forEach(el => el.remove());
        }''')
    except Exception:
        pass
"""

# Insert helpers after TestResult class
if "async def draw_highlight" not in code:
    code = code.replace(new_class, new_class + helpers)

# 3. Add context_start_time
old_page_create = """        print("  [INIT] Creating browser context (recording video)...")
        context_args = {
            "ignore_https_errors": True,
            "record_video_dir": recordings_dir
        }"""
new_page_create = """        print("  [INIT] Creating browser context (recording video)...")
        context_args = {
            "ignore_https_errors": True,
            "record_video_dir": recordings_dir
        }"""
# Wait, actually we need to get time right after page is created.
old_page_ready = """        page = await context.new_page()
        page.set_default_timeout(20000)"""
new_page_ready = """        page = await context.new_page()
        context_start_time = time.time()
        page.set_default_timeout(20000)"""
if "context_start_time = time.time()" not in code:
    code = code.replace(old_page_ready, new_page_ready)

# 4. Inject test timings and highlights
# For each test, we want to capture t_start and t_end. 
# We can do this manually by replacing specific strings around the tests.
# TEST 1:
old_t1_start = """        print(f"\\n{'='*50}\\n  TEST 1: Dynamic UI Discovery (Vision)\\n{'='*50}")"""
new_t1_start = """        print(f"\\n{'='*50}\\n  TEST 1: Dynamic UI Discovery (Vision)\\n{'='*50}")
        t1.video_start = time.time() - context_start_time"""
code = code.replace(old_t1_start, new_t1_start)

# Before taking test_pre.png
old_test_pre = """        await page.screenshot(path=os.path.join(screenshots_dir, "test_pre.png"))"""
new_test_pre = """        for ctrl in controls:
            try:
                await draw_highlight(page, ctrl["box"][1], ctrl["box"][0], ctrl["box"][3], ctrl["box"][2], ctrl["label"], "yellow")
            except:
                pass
        await page.screenshot(path=os.path.join(screenshots_dir, "test_pre.png"))
        await clear_highlights(page)
        t1.video_end = time.time() - context_start_time"""
if "t1.video_end" not in code:
    code = code.replace(old_test_pre, new_test_pre)

# TEST 3 (Single click spin):
old_t3_start = """        print(f"\\n{'='*50}\\n  TEST 3: Spin Validation & Core Loop\\n{'='*50}")"""
new_t3_start = """        print(f"\\n{'='*50}\\n  TEST 3: Spin Validation & Core Loop\\n{'='*50}")
        t3.video_start = time.time() - context_start_time"""
code = code.replace(old_t3_start, new_t3_start)

# Highlight spin button before clicking it in Test 3
old_click_spin = """        await page.mouse.click(center_x, center_y)"""
new_click_spin = """        await draw_highlight(page, spin_btn["box"][1], spin_btn["box"][0], spin_btn["box"][3], spin_btn["box"][2], "Clicking Spin...", "lime")
        await page.screenshot(path=os.path.join(screenshots_dir, "test_prespin.png"))
        await clear_highlights(page)
        await page.mouse.click(center_x, center_y)"""
if "Clicking Spin..." not in code:
    # there are multiple clicks, but the first one in Test 3 is right after "print("    -> Clicking spin button...")"
    old_test3_click = """        print(f"    -> Clicking spin button at ({center_x}, {center_y})")
        await page.mouse.click(center_x, center_y)"""
    new_test3_click = """        print(f"    -> Clicking spin button at ({center_x}, {center_y})")
        await draw_highlight(page, spin_btn["box"][1], spin_btn["box"][0], spin_btn["box"][3], spin_btn["box"][2], "Clicking Spin", "lime")
        await page.screenshot(path=os.path.join(screenshots_dir, "test_prespin.png"))
        await clear_highlights(page)
        await page.mouse.click(center_x, center_y)"""
    code = code.replace(old_test3_click, new_test3_click)

# After test 3 spin completes (taking test_postspin.png)
old_test_post = """        await page.screenshot(path=os.path.join(screenshots_dir, "test_postspin.png"))"""
new_test_post = """        # highlight balance
        if bal_disp:
            await draw_highlight(page, bal_disp["box"][1], bal_disp["box"][0], bal_disp["box"][3], bal_disp["box"][2], f"Balance: {post_balance}", "cyan")
        await page.screenshot(path=os.path.join(screenshots_dir, "test_postspin.png"))
        await clear_highlights(page)
        
        t_now = time.time() - context_start_time
        t3.video_end = t_now
        t_wager.video_start = t3.video_start
        t_wager.video_end = t_now
        t_bal.video_start = t3.video_start
        t_bal.video_end = t_now
        t_payout.video_start = t3.video_start
        t_payout.video_end = t_now
        t_feat.video_start = t3.video_start
        t_feat.video_end = t_now"""
if "t3.video_end = t_now" not in code:
    code = code.replace(old_test_post, new_test_post)


# TEST 4 Rapid clicks
old_t4_start = """        print(f"\\n{'='*50}\\n  TEST 4 & 5: Spam Clicks & Button State\\n{'='*50}")"""
new_t4_start = """        print(f"\\n{'='*50}\\n  TEST 4 & 5: Spam Clicks & Button State\\n{'='*50}")
        t4.video_start = time.time() - context_start_time"""
code = code.replace(old_t4_start, new_t4_start)

# spam clicks screenshot
old_spam_click = """        print("    -> Initiating spam clicks...")
        await page.mouse.click(center_x, center_y)"""
new_spam_click = """        print("    -> Initiating spam clicks...")
        await draw_highlight(page, spin_btn["box"][1], spin_btn["box"][0], spin_btn["box"][3], spin_btn["box"][2], "Spam Clicking...", "red")
        await page.screenshot(path=os.path.join(screenshots_dir, "test_spam_click.png"))
        await clear_highlights(page)
        await page.mouse.click(center_x, center_y)"""
if "Spam Clicking..." not in code:
    code = code.replace(old_spam_click, new_spam_click)
    
old_t4_end = """        t4.passed = (spam_spins == 1)"""
new_t4_end = """        t4.passed = (spam_spins == 1)
        t4.video_end = time.time() - context_start_time
        t5.video_start = t4.video_start
        t5.video_end = t4.video_end"""
code = code.replace(old_t4_end, new_t4_end)


# TEST 6,7,8 (Bet Change)
old_t6_start = """        print(f"\\n{'='*50}\\n  TEST 6, 7 & 8: Bet Adjustments\\n{'='*50}")"""
new_t6_start = """        print(f"\\n{'='*50}\\n  TEST 6, 7 & 8: Bet Adjustments\\n{'='*50}")
        t6.video_start = time.time() - context_start_time
        t7.video_start = t6.video_start
        t8.video_start = t6.video_start"""
code = code.replace(old_t6_start, new_t6_start)

# Before bet click
old_bet_click = """        await page.screenshot(path=os.path.join(screenshots_dir, "test_bet_before.png"))

        print("    -> Clicking Bet Increment (or Decrement)")
        # Click the first inc/dec button"""
new_bet_click = """        await draw_highlight(page, bet_btn["box"][1], bet_btn["box"][0], bet_btn["box"][3], bet_btn["box"][2], "Clicking Bet", "magenta")
        await page.screenshot(path=os.path.join(screenshots_dir, "test_bet_before.png"))
        await clear_highlights(page)

        print("    -> Clicking Bet Increment (or Decrement)")
        # Click the first inc/dec button"""
if "Clicking Bet" not in code:
    code = code.replace(old_bet_click, new_bet_click)

old_bet_after = """        await page.screenshot(path=os.path.join(screenshots_dir, "test_bet_after.png"))"""
new_bet_after = """        await draw_highlight(page, bet_btn["box"][1], bet_btn["box"][0], bet_btn["box"][3], bet_btn["box"][2], "Bet Changed", "magenta")
        await page.screenshot(path=os.path.join(screenshots_dir, "test_bet_after.png"))
        await clear_highlights(page)"""
if "Bet Changed" not in code:
    code = code.replace(old_bet_after, new_bet_after)
    
old_t6_end = """        t7.passed = (restored_bet == initial_bet)"""
new_t6_end = """        t7.passed = (restored_bet == initial_bet)
        t_now = time.time() - context_start_time
        t6.video_end = t_now
        t7.video_end = t_now
        t8.video_end = t_now"""
code = code.replace(old_t6_end, new_t6_end)


# Payload serialization
old_payload = 'payload = [{"name": r.name, "passed": r.passed, "details": r.details, "screenshot": r.screenshot, "video": r.video} for r in results]'
new_payload = 'payload = [{"name": r.name, "passed": r.passed, "details": r.details, "screenshot": r.screenshot, "video": r.video, "video_start": round(r.video_start, 1), "video_end": round(r.video_end, 1)} for r in results]'
code = code.replace(old_payload, new_payload)

with open('test_spin_button_modified_2.py', 'w', encoding='utf-8') as f:
    f.write(code)
print("Wrote patch to test_spin_button_modified_2.py")
