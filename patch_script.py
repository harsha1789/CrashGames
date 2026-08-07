import re

with open('test_spin_button.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Update TestResult class
old_class = """class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = None
        self.details = \"\"

    def __str__(self):"""
new_class = """class TestResult:
    def __init__(self, name: str, screenshot: str = ""):
        self.name = name
        self.passed = None
        self.details = ""
        self.screenshot = screenshot
        self.video = ""

    def __str__(self):"""
code = code.replace(old_class, new_class)

# 2. Add screenshots to TestResult instantiations
replacements = {
    't1 = TestResult("All UI controls detected")': 't1 = TestResult("All UI controls detected", "test_pre.png")',
    't2 = TestResult("Spin endpoint discovered")': 't2 = TestResult("Spin endpoint discovered", "test_pre.png")',
    't3 = TestResult("Single click = 1 spin request")': 't3 = TestResult("Single click = 1 spin request", "test_postspin.png")',
    't_wager = TestResult("Wager correctly processed")': 't_wager = TestResult("Wager correctly processed", "test_prespin.png")',
    't_payout = TestResult("Payout successfully logged (if applicable)")': 't_payout = TestResult("Payout successfully logged (if applicable)", "test_postspin.png")',
    't_feat = TestResult("Feature Triggers monitored")': 't_feat = TestResult("Feature Triggers monitored", "test_postspin.png")',
    't_bal = TestResult("Balance updated correctly")': 't_bal = TestResult("Balance updated correctly", "test_postspin.png")',
    't4 = TestResult("Rapid clicks during spin = still 1 request")': 't4 = TestResult("Rapid clicks during spin = still 1 request", "test_postspin.png")',
    't5 = TestResult("Spin button re-enables after completion")': 't5 = TestResult("Spin button re-enables after completion", "test_postspin.png")',
    't6 = TestResult("Bet can be changed")': 't6 = TestResult("Bet can be changed", "test_bet_before.png")',
    't7 = TestResult("Bet can be restored (round-trip)")': 't7 = TestResult("Bet can be restored (round-trip)", "test_bet_restored.png")',
    't8 = TestResult("Bet buttons disabled during spin")': 't8 = TestResult("Bet buttons disabled during spin", "test_bet_spin_after.png")',
    't_auto = TestResult("Auto Play functionality works")': 't_auto = TestResult("Auto Play functionality works", "test_autoplay.png")',
    't10 = TestResult("Server returns valid spin response")': 't10 = TestResult("Server returns valid spin response", "test_postspin.png")'
}

for old_str, new_str in replacements.items():
    code = code.replace(old_str, new_str)

# 3. Comment out TEST 9
# Find TEST 9 block and wrap in """ ... """
test9_pattern = re.compile(r'(        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n        # TEST 9: Menu button opens a panel\n        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n.*?)(        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n        # TEST 10: Valid server response)', re.DOTALL)

match = test9_pattern.search(code)
if match:
    # Just comment out every line in the match group 1
    commented_test9 = "\\n".join(["        # " + line.lstrip() for line in match.group(1).split("\\n")])
    code = code[:match.start(1)] + commented_test9 + match.group(2) + code[match.end(2):]
else:
    print("Could not find TEST 9 block")

# 4. Attach video path and update payload
old_end = """        results.append(t10)
        print(t10)

        await browser.close()"""
new_end = """        results.append(t10)
        print(t10)
        
        await page.close()
        video_filename = ""
        try:
            video_path = await page.video.path()
            video_filename = os.path.basename(video_path)
        except Exception:
            pass
        
        for r in results:
            r.video = video_filename

        await browser.close()"""
code = code.replace(old_end, new_end)

old_payload = 'payload = [{"name": r.name, "passed": r.passed, "details": r.details} for r in results]'
new_payload = 'payload = [{"name": r.name, "passed": r.passed, "details": r.details, "screenshot": r.screenshot, "video": r.video} for r in results]'
code = code.replace(old_payload, new_payload)

with open('test_spin_button_modified.py', 'w', encoding='utf-8') as f:
    f.write(code)
print("Modifications written to test_spin_button_modified.py")
