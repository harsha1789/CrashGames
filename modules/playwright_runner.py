import asyncio
import threading
from playwright.async_api import async_playwright
from playwright_modules import base, bet_handler, spin

# Device mappings for mobile emulation
MOBILE_DEVICES = {
    "Mobile Web": "iPhone 12",
    "Android": "Pixel 5", 
    "iOS": "iPhone 12"
}

# Map GUI test names to actual functions
TEST_MAP = {
    "default_bet": bet_handler.record_default_bet,
    "max_bet": bet_handler.get_max_bet,
    "min_bet": bet_handler.get_min_bet,
    "spin": spin.run,
    # TODO: Add these later
    # "autospin": autospin.run,
    # "volume": volume.test,
}

async def run_single_game(playwright, browser, game, config, log_callback):
    """Run tests on a single game across all selected platforms"""
    game_name = game.get("gameName", "Unknown Game")
    url = game.get("iframeUrl") or game.get("iframe")
    
    if not url:
        log_callback(f"[SKIP] {game_name} - No iframe URL found")
        return
    
    log_callback(f"\n--- Testing {game_name} ---")
    log_callback(f"URL: {url}")
    
    for platform in config["platforms"]:
        log_callback(f"\n[{platform}] Starting {game_name}")
        
        context = None
        try:
            # Create browser context for platform
            if platform in MOBILE_DEVICES:
                device_name = MOBILE_DEVICES[platform]
                device = playwright.devices[device_name]
                context = await browser.new_context(**device)
                log_callback(f"[{platform}] Emulating {device_name}")
            else:  # Desktop
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )
                log_callback(f"[{platform}] Using desktop viewport")
            
            page = await context.new_page()
            
            # Navigate to game
            log_callback(f"[{platform}] Loading game...")
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(2)  # Allow game to initialize
            
            # Run common startup flow (accept instructions)
            log_callback(f"[{platform}] Handling startup flow...")
            startup_success = await base.handle_startup_flow(page, log_callback, platform)
            
            if not startup_success:
                log_callback(f"[{platform}] ❌ Startup flow failed - skipping tests")
                continue
            
            # Run selected tests
            log_callback(f"[{platform}] Running {len(config['tests'])} tests...")
            for test_name in config["tests"]:
                test_func = TEST_MAP.get(test_name)
                if test_func:
                    try:
                        log_callback(f"[{platform}] → {test_name}")
                        result = await test_func(page, log_callback, platform)
                        if result is not None:
                            log_callback(f"[{platform}] ✓ {test_name}: {result}")
                        else:
                            log_callback(f"[{platform}] ✓ {test_name}: completed")
                    except Exception as e:
                        log_callback(f"[{platform}] ❌ {test_name} failed: {str(e)}")
                else:
                    log_callback(f"[{platform}] ❌ Test '{test_name}' not implemented yet")
            
            log_callback(f"[{platform}] ✓ {game_name} completed")
            
        except Exception as e:
            log_callback(f"[{platform}] ❌ {game_name} failed: {str(e)}")
        
        finally:
            if context:
                try:
                    await context.close()
                except:
                    pass

async def run_all_tests(games, config, log_callback):
    """Main async function to run all tests"""
    log_callback("Launching browser...")
    
    async with async_playwright() as playwright:
        # Launch browser
        browser = await playwright.chromium.launch(
            headless=not config.get("headed", False),
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled"
            ]
        )
        
        try:
            if config["execution"] == "parallel":
                log_callback(f"Running {len(games)} games in PARALLEL mode...")
                
                # Create tasks for all games
                tasks = []
                for game in games:
                    task = run_single_game(playwright, browser, game, config, log_callback)
                    tasks.append(task)
                
                # Run all games concurrently
                await asyncio.gather(*tasks, return_exceptions=True)
                
            else:  # sequential
                log_callback(f"Running {len(games)} games in SEQUENTIAL mode...")
                
                for i, game in enumerate(games, 1):
                    log_callback(f"\n{'='*20} Game {i}/{len(games)} {'='*20}")
                    await run_single_game(playwright, browser, game, config, log_callback)
        
        finally:
            log_callback("Closing browser...")
            await browser.close()

def run_playwright_tests(games, config, log_callback=print):
    """Entry point called from GUI thread"""
    try:
        # Run the async event loop
        asyncio.run(run_all_tests(games, config, log_callback))
        log_callback("\n🎉 All tests completed successfully!")
        
    except Exception as e:
        log_callback(f"\n❌ Test run failed: {str(e)}")
        import traceback
        log_callback(f"Error details: {traceback.format_exc()}")

# For testing purposes
if __name__ == "__main__":
    # Test configuration
    test_games = [
        {
            "gameName": "Test Game 1",
            "iframeUrl": "https://example.com/game1"
        },
        {
            "gameName": "Test Game 2", 
            "iframeUrl": "https://example.com/game2"
        }
    ]
    
    test_config = {
        "tests": ["spin", "default_bet"],
        "platforms": ["Desktop", "Mobile Web"],
        "execution": "sequential",
        "headed": True
    }
    
    run_playwright_tests(test_games, test_config)