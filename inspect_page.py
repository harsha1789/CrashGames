"""Quick diagnostic: inspect the game page DOM structure."""
import asyncio
from playwright.async_api import async_playwright

URL = ("https://pocket-play.live.stake-engine.com/bank-blast/v4/"
       "?sessionID=YZE-EnL-vnbhnWFD8Cn4f8hkJXmrUw5YBRwDoh4bgv4YMtMBtXr3C6dTaSMvOOHu1xVWa5diNECdzXQQ_GmzIg=="
       "&rgs_url=rgsd.stake-engine.com&lang=en&currency=USD&device=desktop&social=false&demo=false")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        print("Navigating...")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"Nav warning: {e}")

        print("Waiting 35s for game to load...")
        await asyncio.sleep(35)

        # 1. Check for iframes
        iframes_info = await page.evaluate("""() => {
            const iframes = document.querySelectorAll('iframe');
            return Array.from(iframes).map(f => ({
                src: f.src,
                id: f.id,
                className: f.className,
                rect: f.getBoundingClientRect(),
                width: f.width,
                height: f.height,
            }));
        }""")
        print(f"\n=== IFRAMES ({len(iframes_info)}) ===")
        for i, info in enumerate(iframes_info):
            print(f"  iframe[{i}]: {info}")

        # 2. Check for canvas elements on main page
        canvas_info = await page.evaluate("""() => {
            const canvases = document.querySelectorAll('canvas');
            return Array.from(canvases).map(c => ({
                id: c.id,
                className: c.className,
                width: c.width,
                height: c.height,
                rect: c.getBoundingClientRect(),
                style: c.style.cssText,
            }));
        }""")
        print(f"\n=== CANVAS ELEMENTS ON MAIN PAGE ({len(canvas_info)}) ===")
        for i, info in enumerate(canvas_info):
            print(f"  canvas[{i}]: {info}")

        # 3. Check frames accessible via Playwright
        print(f"\n=== PLAYWRIGHT FRAMES ({len(page.frames)}) ===")
        for i, frame in enumerate(page.frames):
            print(f"  frame[{i}]: name={frame.name}, url={frame.url[:120]}")
            # Check for canvas inside each frame
            try:
                canvas_in_frame = await frame.evaluate("""() => {
                    const canvases = document.querySelectorAll('canvas');
                    return Array.from(canvases).map(c => ({
                        id: c.id,
                        className: c.className,
                        width: c.width,
                        height: c.height,
                        rect: c.getBoundingClientRect(),
                    }));
                }""")
                if canvas_in_frame:
                    print(f"    -> Canvas elements in this frame: {canvas_in_frame}")
            except Exception as e:
                print(f"    -> Cannot access frame: {e}")

        # 4. Dump top-level body children
        body_children = await page.evaluate("""() => {
            const body = document.body;
            if (!body) return 'No body';
            return Array.from(body.children).map(el => ({
                tag: el.tagName,
                id: el.id,
                className: el.className,
                rect: el.getBoundingClientRect(),
            }));
        }""")
        print(f"\n=== BODY CHILDREN ===")
        for child in body_children:
            print(f"  {child}")

        await browser.close()

asyncio.run(main())
