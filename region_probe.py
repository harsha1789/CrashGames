"""
region_probe.py — auto-discover per-region backend config by sniffing the live site.

WHY: the `x-brand-id`, real origin (some regions redirect, e.g. betway.co.bw -> bdbetway.com),
and the auth/search/launch/balance API hosts live inside the site's JS / network traffic — they
can't be scraped statically. This opens the real site in a browser, captures every request's URL
+ headers, and prints a ready-to-paste REGION_OVERRIDES entry for modules/utils.py.

USAGE (run on your authorized network):
    python region_probe.py --brand betway --region BW
    python region_probe.py --brand jackpotcity --region GH --headed   # default is headed

The browser opens the region's site. LOG IN ONCE (your real creds) and click into the casino /
open a game — that triggers the auth + wallet + casino-launch calls so the probe can capture the
x-brand-id and the balance/launch endpoints. Then press ENTER in the terminal to print the result.
Nothing is sent anywhere; it only observes your own browser's requests.
"""
import argparse
import asyncio
import re
from urllib.parse import urlsplit
from playwright.async_api import async_playwright

from modules.utils import SITE_ORIGIN, region_config

# Patterns that classify a captured request URL into a config slot.
_PATTERNS = [
    ("auth_url",          re.compile(r"(authenticate|/auth/|/login)", re.I)),
    ("balance_url",       re.compile(r"(balance|wallet)", re.I)),
    ("casino_search_url", re.compile(r"(gaming/search|/search)", re.I)),
    ("casino_launch_url", re.compile(r"(gaming/launch|/launch)", re.I)),
]


async def probe(brand, region, headed=True, wait=120):
    start = SITE_ORIGIN.get((brand, region))
    if not start:
        print(f"No known start URL for {brand}/{region}; add it to SITE_ORIGIN first.")
        return
    print(f"\nProbing {brand}/{region} — opening {start}")
    print("→ Log in with your real account, then open the casino / launch a game.")
    print("→ When you've done that, come back here and press ENTER.\n")

    found = {"x_brand_id": None, "origin": None}
    hits = {k: None for k, _ in _PATTERNS}      # slot -> full URL (first seen)
    hosts = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not headed,
                                           args=["--window-size=1280,900", "--window-position=0,0"])
        ctx = await browser.new_context(ignore_https_errors=True, no_viewport=True)
        page = await ctx.new_page()

        def on_request(req):
            # x-brand-id is the prize — captured from whatever request carries it (often the auth POST).
            xbid = req.headers.get("x-brand-id")
            if xbid and not found["x_brand_id"]:
                found["x_brand_id"] = xbid
                print(f"   [captured] x-brand-id = {xbid}")
            url = req.url
            host = urlsplit(url).netloc
            if any(s in host for s in ("api", "casino", "auth", "synapse", "wallet", "balance", "jpc")):
                hosts.add(host)
            for slot, rx in _PATTERNS:
                if hits[slot] is None and rx.search(url):
                    hits[slot] = url.split("?")[0] if slot != "casino_launch_url" else url
                    print(f"   [captured] {slot} = {hits[slot]}")

        page.on("request", on_request)
        try:
            await page.goto(start, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"   nav warning: {e}")
        found["origin"] = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"

        # Wait for the user to log in + launch, capped at `wait` seconds.
        try:
            await asyncio.get_event_loop().run_in_executor(None, input)
        except Exception:
            await asyncio.sleep(wait)
        try:
            await browser.close()
        except Exception:
            pass

    # ── emit a ready-to-paste override entry ──
    print("\n" + "=" * 70)
    print(f"  REGION_OVERRIDES entry for ({brand!r}, {region!r}) — paste into modules/utils.py")
    print("=" * 70)
    cfg = region_config(brand, region)
    entry = {
        "auth_url": hits["auth_url"] or cfg["auth_url"],
        "x_brand_id": found["x_brand_id"] or "TODO-not-captured (was it sent? try logging in again)",
        "balance_url": hits["balance_url"] or "TODO-not-seen (open the wallet/balance once)",
    }
    if brand == "jackpotcity":
        entry["casino_search_url"] = hits["casino_search_url"] or "TODO"
        entry["casino_launch_url"] = hits["casino_launch_url"] or "TODO"
    if found["origin"] and found["origin"] not in (cfg["origin"], None):
        entry["referer"] = found["origin"] + "/"
        print(f"  # NOTE: real origin after redirect = {found['origin']} (assumed {cfg['origin']})")
    print(f'REGION_OVERRIDES[({brand!r}, {region!r})] = {{')
    for k, v in entry.items():
        print(f'    {k!r}: {v!r},')
    print("}")
    print(f"\n  API hosts seen: {sorted(hosts) or '(none)'}")
    print("=" * 70)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Probe a region's live site for backend config")
    ap.add_argument("--brand", default="betway")
    ap.add_argument("--region", default="ZA")
    ap.add_argument("--headed", action="store_true", default=True)
    ap.add_argument("--wait", type=int, default=120, help="fallback seconds if ENTER isn't used")
    a = ap.parse_args()
    asyncio.run(probe(a.brand.lower(), a.region.upper(), headed=a.headed, wait=a.wait))
