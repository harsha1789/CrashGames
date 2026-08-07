"""
Diagnose: Monitor ALL network traffic & WebSocket messages while clicking spin.
This tells us definitively if a spin event reached the game server.
"""
import asyncio
import json
from playwright.async_api import async_playwright

URL = ("https://pocket-play.live.stake-engine.com/bank-blast/v4/"
       "?sessionID=YZE-EnL-vnbhnWFD8Cn4f8hkJXmrUw5YBRwDoh4bgv4YMtMBtXr3C6dTaSMvOOHu1xVWa5diNECdzXQQ_GmzIg=="
       "&rgs_url=rgsd.stake-engine.com&lang=en&currency=USD&device=desktop&social=false&demo=false")

VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080


async def main():
    all_requests = []
    all_responses = []
    ws_messages = []
    ws_connections = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            ignore_https_errors=True,
        )
        page = await context.new_page()

        # ── Intercept ALL network requests ──
        def on_request(req):
            all_requests.append({
                "url": req.url,
                "method": req.method,
                "post_data": req.post_data[:500] if req.post_data else None,
                "resource_type": req.resource_type,
            })

        def on_response(resp):
            all_responses.append({
                "url": resp.url,
                "status": resp.status,
            })

        # ── Intercept WebSocket connections ──
        def on_websocket(ws):
            ws_connections.append(ws.url)
            print(f"  🔌 WebSocket connected: {ws.url}")

            def on_ws_received(payload):
                ws_messages.append({"direction": "received", "data": str(payload)[:300]})

            def on_ws_sent(payload):
                ws_messages.append({"direction": "sent", "data": str(payload)[:300]})

            ws.on("framereceived", on_ws_received)
            ws.on("framesent", on_ws_sent)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("websocket", on_websocket)

        print("Navigating...")
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"Nav warning: {e}")

        print("Waiting 35s for game to load...")
        await asyncio.sleep(35)

        # Dismiss splash if any
        await page.mouse.click(VIEWPORT_WIDTH // 2, VIEWPORT_HEIGHT // 2)
        await asyncio.sleep(2)

        # ── Clear tracking — only care about what happens AFTER this point ──
        print(f"\n{'='*70}")
        print(f"  GAME LOADED — Clearing request logs, starting fresh monitoring")
        print(f"{'='*70}")
        print(f"  WebSocket connections so far: {len(ws_connections)}")
        for ws_url in ws_connections:
            print(f"    🔌 {ws_url}")
        print(f"  Total requests during load: {len(all_requests)}")
        print(f"  Total WS messages during load: {len(ws_messages)}")

        pre_request_count = len(all_requests)
        pre_response_count = len(all_responses)
        pre_ws_count = len(ws_messages)

        # ── Take pre-click screenshot ──
        await page.screenshot(path="diag_pre_click.png")

        # ── PHASE 1: Wait 5 seconds without clicking (baseline) ──
        print(f"\n{'─'*70}")
        print(f"  PHASE 1: 5s baseline — NO clicking (measure idle traffic)")
        print(f"{'─'*70}")
        await asyncio.sleep(5)

        baseline_new_requests = len(all_requests) - pre_request_count
        baseline_new_ws = len(ws_messages) - pre_ws_count
        print(f"  Idle traffic: {baseline_new_requests} new HTTP requests, {baseline_new_ws} new WS messages")

        # Log any new requests during idle
        for req in all_requests[pre_request_count:]:
            if "stake-engine" in req["url"] or "rgs" in req["url"].lower():
                print(f"    📡 RGS request during idle: {req['method']} {req['url'][:120]}")

        # ── PHASE 2: Click spin button area and monitor ──
        # From the Gemini detection: Spin Button center=(1301, 1018)
        # But let's also try the visually apparent location
        spin_coords_gemini = (1301, 1018)

        # Let's also try clicking a few candidate locations
        # The spin button in the screenshot appears to be around the bottom-right toolbar
        # Let's compute from the Gemini box_2d: [907, 650, 980, 706]
        # That means: ymin=907, xmin=650, ymax=980, xmax=706 → center_x = (650+706)/2*1920/1000 = 1301, center_y = (907+980)/2*1080/1000 = 1018
        
        print(f"\n{'─'*70}")
        print(f"  PHASE 2: Clicking spin button at Gemini coords {spin_coords_gemini}")
        print(f"{'─'*70}")

        pre_click_requests = len(all_requests)
        pre_click_ws = len(ws_messages)

        # Click at Gemini-detected position
        print(f"  ▶ Clicking at {spin_coords_gemini}...")
        await page.mouse.click(spin_coords_gemini[0], spin_coords_gemini[1])

        # Wait and observe
        await asyncio.sleep(1)
        await page.screenshot(path="diag_1s_after_click.png")

        await asyncio.sleep(5)
        await page.screenshot(path="diag_6s_after_click.png")

        post_click_requests = all_requests[pre_click_requests:]
        post_click_ws = ws_messages[pre_click_ws:]

        print(f"\n  📊 After clicking Gemini coords:")
        print(f"     New HTTP requests: {len(post_click_requests)}")
        print(f"     New WS messages:   {len(post_click_ws)}")

        # Show ALL new requests (focusing on game-server communication)
        for req in post_click_requests:
            is_rgs = "stake-engine" in req["url"] or "rgs" in req["url"].lower()
            marker = "🎰 RGS" if is_rgs else "   "
            print(f"     {marker} {req['method']} {req['url'][:120]}")
            if req["post_data"]:
                print(f"          POST data: {req['post_data'][:200]}")

        # Show WS messages
        for msg in post_click_ws:
            print(f"     🔌 WS {msg['direction']}: {msg['data'][:200]}")

        # ── PHASE 3: Try alternative click approaches ──
        print(f"\n{'─'*70}")
        print(f"  PHASE 3: Trying alternative click methods")
        print(f"{'─'*70}")

        pre_alt_requests = len(all_requests)
        pre_alt_ws = len(ws_messages)

        # Method A: dispatchEvent on the overlay canvas
        print(f"\n  Method A: JavaScript dispatchEvent on overlay-canvas at {spin_coords_gemini}...")
        await page.evaluate(f"""() => {{
            const canvas = document.querySelector('.overlay-canvas');
            if (!canvas) return 'no overlay-canvas';
            const rect = canvas.getBoundingClientRect();
            const x = {spin_coords_gemini[0]};
            const y = {spin_coords_gemini[1]};
            
            // Dispatch pointer events (many game engines use these)
            const pointerDown = new PointerEvent('pointerdown', {{
                clientX: x, clientY: y, bubbles: true, pointerId: 1, pointerType: 'mouse'
            }});
            const pointerUp = new PointerEvent('pointerup', {{
                clientX: x, clientY: y, bubbles: true, pointerId: 1, pointerType: 'mouse'
            }});
            canvas.dispatchEvent(pointerDown);
            canvas.dispatchEvent(pointerUp);
            
            // Also dispatch mouse events
            canvas.dispatchEvent(new MouseEvent('mousedown', {{clientX: x, clientY: y, bubbles: true}}));
            canvas.dispatchEvent(new MouseEvent('mouseup', {{clientX: x, clientY: y, bubbles: true}}));
            canvas.dispatchEvent(new MouseEvent('click', {{clientX: x, clientY: y, bubbles: true}}));
            return 'dispatched';
        }}""")

        await asyncio.sleep(6)
        await page.screenshot(path="diag_after_js_click.png")

        post_alt_requests = all_requests[pre_alt_requests:]
        post_alt_ws = ws_messages[pre_alt_ws:]
        print(f"     New HTTP requests: {len(post_alt_requests)}")
        print(f"     New WS messages:   {len(post_alt_ws)}")
        for req in post_alt_requests:
            is_rgs = "stake-engine" in req["url"] or "rgs" in req["url"].lower()
            marker = "🎰 RGS" if is_rgs else "   "
            print(f"     {marker} {req['method']} {req['url'][:120]}")
            if req["post_data"]:
                print(f"          POST data: {req['post_data'][:200]}")
        for msg in post_alt_ws:
            print(f"     🔌 WS {msg['direction']}: {msg['data'][:200]}")

        # ── PHASE 4: Try clicking the main canvas instead ──
        print(f"\n  Method B: JavaScript dispatchEvent on main canvas...")
        pre_b_requests = len(all_requests)
        pre_b_ws = len(ws_messages)

        await page.evaluate(f"""() => {{
            const canvas = document.querySelector('.canvas');
            if (!canvas) return 'no canvas';
            const x = {spin_coords_gemini[0]};
            const y = {spin_coords_gemini[1]};
            canvas.dispatchEvent(new PointerEvent('pointerdown', {{
                clientX: x, clientY: y, bubbles: true, pointerId: 1, pointerType: 'mouse'
            }}));
            canvas.dispatchEvent(new PointerEvent('pointerup', {{
                clientX: x, clientY: y, bubbles: true, pointerId: 1, pointerType: 'mouse'
            }}));
            canvas.dispatchEvent(new MouseEvent('click', {{clientX: x, clientY: y, bubbles: true}}));
            return 'dispatched';
        }}""")

        await asyncio.sleep(6)
        await page.screenshot(path="diag_after_canvas_click.png")

        post_b_requests = all_requests[pre_b_requests:]
        post_b_ws = ws_messages[pre_b_ws:]
        print(f"     New HTTP requests: {len(post_b_requests)}")
        print(f"     New WS messages:   {len(post_b_ws)}")
        for req in post_b_requests:
            is_rgs = "stake-engine" in req["url"] or "rgs" in req["url"].lower()
            marker = "🎰 RGS" if is_rgs else "   "
            print(f"     {marker} {req['method']} {req['url'][:120]}")
        for msg in post_b_ws:
            print(f"     🔌 WS {msg['direction']}: {msg['data'][:200]}")

        # ── FINAL SUMMARY ──
        print(f"\n{'='*70}")
        print(f"  FINAL SUMMARY")
        print(f"{'='*70}")
        total_new_reqs = len(all_requests) - pre_request_count
        total_new_ws = len(ws_messages) - pre_ws_count

        # Filter RGS-related requests
        rgs_requests = [r for r in all_requests[pre_request_count:] 
                       if "stake-engine" in r["url"] or "rgs" in r["url"].lower()]

        print(f"  Total HTTP requests after game loaded: {total_new_reqs}")
        print(f"  RGS-related requests: {len(rgs_requests)}")
        print(f"  Total WS messages: {total_new_ws}")
        print(f"  WebSocket connections: {ws_connections}")

        if len(rgs_requests) == 0 and total_new_ws == 0:
            print(f"\n  ⚠️  NO game-server communication detected!")
            print(f"  Possible causes:")
            print(f"    1. Click coordinates are wrong (not hitting the spin button)")
            print(f"    2. Game uses a communication method we're not monitoring")
            print(f"    3. Session expired")
        elif total_new_ws > 0:
            print(f"\n  ✅ WebSocket communication detected — game uses WebSockets!")
        elif len(rgs_requests) > 0:
            print(f"\n  ✅ RGS HTTP requests detected — game uses HTTP API!")

        await browser.close()


asyncio.run(main())
