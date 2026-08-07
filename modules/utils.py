# modules/utils.py
from copy import deepcopy
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

JACKPOT_CITY_ENDPOINTS = {
    "AUTH": "https://www.betway.co.za/appsynapse/auth/users/authenticate?nogeoredirect=1",
    "BALANCE": "https://app.jpc.africa/balance/v1/wallet/balance",
    "GAME_SEARCH": "https://casinoapic.betwayafrica.com/api/v3/Gaming/Search",
    "GAME_LAUNCH": "https://casinoapi.betwayafrica.com/api/v3/Gaming/launch?environment=Production",
}

# Wallet/balance endpoint per brand. Auth is always against betway.co.za, but the wallet API
# may differ by brand — keep them separate so a wrong one for a single brand is easy to correct.
# NOTE: the Betway value below is unconfirmed (it returns HTTP 200 with an empty body for the
# demo accounts). The account-card balance feature was removed; if wallet balance is needed again,
# capture the real wallet request from the game's network tab and drop it in here.
BALANCE_ENDPOINTS = {
    "betway": "https://app.jpc.africa/balance/v1/wallet/balance",
    "jackpotcity": "https://app.jpc.africa/balance/v1/wallet/balance",
}

# Minimal headers; we'll inject Authorization when needed
HEADERS = {
    "AUTH": {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://www.betway.co.za",
        "referer": "https://www.betway.co.za/lobby/casino-games",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-brand-id": "bd66ebe1-080b-4455-9094-bf0464d4adbf",
    },
    "BALANCE": {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://www.betway.co.za",
        "referer": "https://www.betway.co.za/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        # note: Authorization header must be added per-request
    },
    "GAME_SEARCH": {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://www.betway.co.za",
        "referer": "https://www.betway.co.za/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        # note: Authorization header must be added per-request
    },
    "GAME_LAUNCH": {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://www.betway.co.za",
        "referer": "https://www.betway.co.za/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        # note: Authorization header must be added per-request
    },
}

API_DELAYS = {
    "SEARCH": 1.0,
    "IFRAME": 1.5,
}

GAME_STATUSES = {
    "FOUND": "found",
    "NOT_FOUND": "not_found",
    "ERROR": "error",
    "READY": "ready",
    "IFRAME_FAILED": "iframe_failed",
    "IFRAME_ERROR": "iframe_error",
}

def add_za_country_code(username: str) -> str:
    """Back-compat ZA shim — delegates to the region-aware formatter."""
    return add_country_code(username, "ZA")


# ════════════════════════════════════════════════════════════════════════════════
#  PER-(BRAND, REGION) BACKEND CONFIG
#  ZA is fully known/verified. Other regions are scaffolded from their public domains +
#  standard country data so that region selection drives auth/search/launch. Fields marked
#  TODO must be captured from a real logged-in session (browser Network tab) before that
#  region can authenticate/launch — see fill_region() / the SLOT_TEST_CHECKLIST notes:
#    • x_brand_id  — the `x-brand-id` header the site sends on auth   (REQUIRED for auth)
#    • balance_url — that brand+region's wallet/balance endpoint
#    • (JPC) casino_search_url / casino_launch_url — JackpotCity's casino API base
#  Assumption (override per region if wrong): every Betway region uses the same auth PATH on
#  its own domain, and the Betway-Africa casino API (casinoapi*.betwayafrica.com) is shared
#  across regions with regionCode/currency carried as params.
# ════════════════════════════════════════════════════════════════════════════════

# ISD dialling prefix used to normalize a saved username into the backend's expected MSISDN.
DIAL_CODE = {"ZA": "27", "BW": "267", "GH": "233", "MW": "265",
             "MZ": "258", "NG": "234", "TZ": "255", "ZM": "260"}
# Wallet currency per region.
CURRENCY = {"ZA": "ZAR", "BW": "BWP", "GH": "GHS", "MW": "MWK",
            "MZ": "MZN", "NG": "NGN", "TZ": "TZS", "ZM": "ZMW"}

# Public site origin per (brand, region) — from the URLs the team provided.
SITE_ORIGIN = {
    ("betway", "ZA"): "https://www.betway.co.za",
    ("betway", "BW"): "https://www.betway.co.bw",
    ("betway", "GH"): "https://www.betway.com.gh",
    ("betway", "MW"): "https://www.betway.mw",
    ("betway", "MZ"): "https://en.betway.co.mz",
    ("betway", "NG"): "https://www.betway.com.ng",
    ("betway", "TZ"): "https://en.betway.co.tz",
    ("betway", "ZM"): "https://www.betway.co.zm",
    ("jackpotcity", "ZA"): "https://www.jackpotcity.co.za",
    ("jackpotcity", "GH"): "https://www.jackpotcitycasino.com.gh",
    ("jackpotcity", "TZ"): "https://en.jackpotcitycasino.co.tz",
}

# Betway-Africa casino API — VERIFIED shared across all Betway regions (2026-07-17): the same
# host serves every region, scoped by the regionCode/currency params + the region's bearer token.
# Search MUST be v4: v3 only ever returned the ZA catalog (GH/NG/ZM/TZ came back empty), while v4
# returns games for every region (nested under verticals[].data[] — game_handler handles both
# shapes). Launch stays v3 (v4 launch is 405 Method Not Allowed; v3 returns the iframe for all).
_BW_CASINO_SEARCH = "https://casinoapic.betwayafrica.com/api/v4/Gaming/Search"
_BW_CASINO_LAUNCH = "https://casinoapi.betwayafrica.com/api/v3/Gaming/launch?environment=Production"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")

# Verified / supplied per-region overrides. Fill these as credentials + brand-ids arrive; any key
# omitted falls back to the derived default in region_config().
REGION_OVERRIDES = {
    ("betway", "ZA"): {
        "auth_url": "https://www.betway.co.za/appsynapse/auth/users/authenticate?nogeoredirect=1",
        "referer": "https://www.betway.co.za/lobby/casino-games",
        "x_brand_id": "bd66ebe1-080b-4455-9094-bf0464d4adbf",
        "balance_url": "https://app.jpc.africa/balance/v1/wallet/balance",
    },
    # JackpotCity (verified 2026-07-17). JPC is a SEPARATE backend from Betway: auth is a shared
    # host (app.jpc.africa/auth/v3/Users/authenticate), the casino API is apic.jpc.africa, and the
    # regionCode is J-prefixed (JZA/JGH/JTZ). x-brand-id is REQUIRED and PER-REGION (unlike Betway,
    # which needs none). Token comes back at data.jwtToken (auth_handler already handles that).
    ("jackpotcity", "ZA"): {
        "auth_url": "https://app.jpc.africa/auth/v3/Users/authenticate",
        "x_brand_id": "D76A0E62-B728-4A28-8134-C57A1A003199",
        "casino_search_url": "https://apic.jpc.africa/api/v4/Gaming/Search",
        "casino_launch_url": "https://apic.jpc.africa/api/v3/Gaming/launch?environment=Production",
        "region_code": "JZA",
        "balance_url": "https://app.jpc.africa/balance/v2/Wallet/balance",
    },
    ("jackpotcity", "GH"): {
        "auth_url": "https://app.jpc.africa/auth/v3/Users/authenticate",
        "x_brand_id": "BE6CDE0B-73E4-448A-A706-F9C20CF3F669",
        "casino_search_url": "https://apic.jpc.africa/api/v4/Gaming/Search",
        "casino_launch_url": "https://apic.jpc.africa/api/v3/Gaming/launch?environment=Production",
        "region_code": "JGH",
    },
    ("jackpotcity", "TZ"): {
        "auth_url": "https://app.jpc.africa/auth/v3/Users/authenticate",
        "x_brand_id": "E430B4FB-55DC-42CC-AA65-1057CE49C7E6",
        "casino_search_url": "https://apic.jpc.africa/api/v4/Gaming/Search",
        "casino_launch_url": "https://apic.jpc.africa/api/v3/Gaming/launch?environment=Production",
        "region_code": "JTZ",
    },
    # ("betway", "BW"): accounts locked (NumberOfFailedLoginAttemptsExceeded) — config already works
}


def region_config(brand: str, region: str) -> dict:
    """Resolve the backend config for a (brand, region). Known/supplied values win; the rest are
    derived from the site origin + standard country data. `x_brand_id`/`balance_url` may be None
    until supplied — callers should surface a clear 'region not configured' error in that case."""
    brand = (brand or "betway").lower()
    region = (region or "ZA").upper()
    origin = SITE_ORIGIN.get((brand, region))
    ov = REGION_OVERRIDES.get((brand, region), {})
    is_bw = brand == "betway"
    return {
        "brand": brand,
        "region": region,
        "origin": origin,
        "referer": ov.get("referer") or (f"{origin}/" if origin else None),
        "auth_url": ov.get("auth_url") or
                    (f"{origin}/appsynapse/auth/users/authenticate?nogeoredirect=1" if origin else None),
        "x_brand_id": ov.get("x_brand_id"),
        "balance_url": ov.get("balance_url"),
        "casino_search_url": ov.get("casino_search_url") or (_BW_CASINO_SEARCH if is_bw else None),
        "casino_launch_url": ov.get("casino_launch_url") or (_BW_CASINO_LAUNCH if is_bw else None),
        "country_code": region,
        # JPC scopes its casino catalog by a J-prefixed regionCode (JZA/JGH/JTZ), supplied in the
        # override; Betway uses the plain ISO code. Falls back to the ISO region either way.
        "region_code": ov.get("region_code") or region,
        "dial": DIAL_CODE.get(region, ""),
        "currency": CURRENCY.get(region, ""),
        "user_agent": _UA,
        # A Betway region is usable with just its domain: auth, casino search (v4) and launch all
        # work from the region's own site + bearer token — x-brand-id is NOT required (verified
        # 2026-07-17 for GH/NG/ZM/TZ). JackpotCity still needs its casino API base supplied.
        "configured": bool(origin) if is_bw else bool(origin and ov.get("casino_search_url")),
    }


def configured_pairs(brand: str = None) -> list:
    """The (brand, region) pairs that are actually testable — every SITE_ORIGIN entry whose
    region_config reports `configured`. Single source of truth for the auto-sweep scopes
    ('all products', 'all regions of a brand', 'betway'/'jpc only'). Order: brand, then the
    order regions appear in SITE_ORIGIN. Pass `brand` to filter to one product."""
    out = []
    for (b, r) in SITE_ORIGIN:
        if brand and b != brand:
            continue
        if region_config(b, r).get("configured"):
            out.append((b, r))
    return out


def add_country_code(username: str, region: str = "ZA") -> str:
    """Normalize a saved username into the backend MSISDN for the region's dialling code.
    Strips a leading '+', a leading national '0', and prefixes the ISD code if not already present."""
    code = DIAL_CODE.get((region or "ZA").upper(), "27")
    u = (username or "").strip()
    if u.startswith("+"):
        u = u[1:]
    if u.startswith(code):
        return u
    if u.startswith("0"):
        u = u[1:]
    return f"{code}{u}"


def auth_headers(cfg: dict) -> dict:
    """Auth request headers for a region config (origin/referer/x-brand-id from cfg)."""
    h = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": cfg.get("origin") or "",
        "referer": cfg.get("referer") or "",
        "user-agent": cfg.get("user_agent") or _UA,
    }
    if cfg.get("x_brand_id"):
        h["x-brand-id"] = cfg["x_brand_id"]
    return h


def casino_headers(cfg: dict) -> dict:
    """Headers for the casino search/launch/balance APIs (Authorization added per-request).
    x-brand-id is included when the region provides one — REQUIRED by JPC's casino API to scope
    the catalog; Betway leaves it unset and is unaffected."""
    h = {
        "accept": "*/*",
        "accept-encoding": "gzip, deflate, br, zstd",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": cfg.get("origin") or "",
        "referer": cfg.get("referer") or "",
        "user-agent": cfg.get("user_agent") or _UA,
    }
    if cfg.get("x_brand_id"):
        h["x-brand-id"] = cfg["x_brand_id"]
    return h
