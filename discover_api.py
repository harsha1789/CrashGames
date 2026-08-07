"""
Discover spin API for Le Pharaoh - grid sweep of clicks over the spin button area.
"""
import asyncio
import time
from playwright.async_api import async_playwright

URL = ("https://d1oa92ndvzdrfz.cloudfront.net/launcher/static-launcher-backend.html"
       "?gameid=1562&channel=desktop&mode=demo&currency=eur&language=en"
       "&lobbyurl=https%3a%2f%2fstake.bet&token=StakeDemoToken&partner=stake"
       "&rcenable=false&rcelapsed=0&rcinterval=0")

VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080


async def main():
    all_requests = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        def on_request(req):
            if req.resource_type in ("document", "stylesheet", "font", "image", "media", "other"):
                return
            all_requests.append({
                "time": time.time(),
                "url": req.url,
                "method": req.method,
                "type": req.resource_type,
                "post_data": req.post_data[:500] if req.post_data else None,
            })

        page.on("request", on_request)

        print("Navigating...")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"Nav warning: {e}")

        print("Waiting 35s for game to load...")
        await asyncio.sleep(35)
        await page.mouse.click(VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)
        await asyncio.sleep(3)

        # The spin button is approx at x=1350, y=990. Let's do a short discovery.
        click_targets = [ 
            (1355, 985),  # direct hit?
            (1360, 990), 
            (1365, 990),
            (1350, 995) 
        ]

        for (cx, cy) in click_targets:
            pre_count = len(all_requests)
            print(f"\nClicking spin at ({cx}, {cy})...")
            await page.mouse.click(cx, cy)
            await asyncio.sleep(4)
            
            new_reqs = all_requests[pre_count:]
            game_reqs = [r for r in new_reqs if 'google' not in r['url'] and ('play' in r['url'] or 'spin' in r['url'] or 'bet' in r['url'] or 'cloudfront' in r['url'])]
            
            if game_reqs:
                print(f"Activity detected!")
                for r in game_reqs:
                    if r['method'] == 'POST':
                        print(f"  ** POST {r['url']}")
                        if r['post_data']:
                            print(f"         POST: {r['post_data'][:300]}")
                break

        await browser.close()

asyncio.run(main())
