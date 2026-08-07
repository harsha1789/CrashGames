# Multi-region setup — what's wired, what's pending

Region selection now drives the backend end-to-end. `modules/utils.region_config(brand, region)` is
the single source of truth; `auth_handler`, `game_handler`, `iframe_handler`, and `app.py`
(`/api/balance`, `/api/games/search`) all consume it. **ZA (Betway) is fully configured and works.**

## How a launch flows (per region)
1. `AuthHandler.authenticate(user, pass, brand, region)` → POSTs to the region's `auth_url` with
   `countryCode` = region, username normalized to the region's dial code, and the `x-brand-id` header.
2. `GameHandler.search_game(name, token, brand, region)` → casino Search API with `regionCode` +
   `currency` for that region.
3. `IframeHandler.get_iframe_url(id, token, brand, region)` → casino launch API with `requestUrl` =
   the region's site origin → returns the playable iframe URL.

## Already derived (no action needed)
- **Site origins** (from the URLs you gave): Betway ZA/BW/GH/MW/MZ/NG/TZ, JPC ZA/GH/TZ.
- **Dial codes & currencies**: ZA 27/ZAR, BW 267/BWP, GH 233/GHS, MW 265/MWK, MZ 258/MZN,
  NG 234/NGN, TZ 255/TZS, ZM 260/ZMW.
- **Auth URL**: assumed `{origin}/appsynapse/auth/users/authenticate?nogeoredirect=1` per region.
- **Betway casino API**: assumed shared (`casinoapi*.betwayafrica.com`) across Betway regions.

## ⏳ Pending per region — fill in `REGION_OVERRIDES` in `modules/utils.py`
For **each** `(brand, region)` you want live, capture from a real logged-in session (browser
DevTools → Network) and add an override entry:

```python
REGION_OVERRIDES[("betway", "BW")] = {
    "x_brand_id":  "…",   # REQUIRED — the `x-brand-id` request header on the auth call
    "balance_url": "…",   # the wallet/balance GET endpoint for that region
    # "auth_url":  "…",   # only if the path differs from the assumed /appsynapse/... one
    # "casino_search_url" / "casino_launch_url": only if NOT the shared betwayafrica API
}
```

Plus the **credentials** (a funded/test account) for that brand+region — added in the UI / `accounts.json`.

| Brand | Region | Origin | x-brand-id | balance_url | auth path | casino API |
|-------|--------|--------|-----------|-------------|-----------|-----------|
| Betway | ZA | ✅ | ✅ | ✅ | ✅ | ✅ shared |
| Betway | BW/GH/MW/MZ/NG/TZ | ✅ | ⏳ | ⏳ | assumed ✅ | assumed ✅ |
| JackpotCity | ZA/GH/TZ | ✅ | ⏳ | ⏳ | ⚠️ assumed (verify — JPC may differ) | ⏳ (not betwayafrica) |

**JackpotCity caveat:** the auth path and casino Search/launch endpoints are assumed-same-as-Betway
and are almost certainly different. JPC regions will fail with a clear "not configured" message until
their `auth_url` + `casino_search_url` + `casino_launch_url` + `x_brand_id` are filled.

## Behaviour until filled
Selecting an unconfigured region fails **loudly and safely** — auth returns
`"{brand}/{region} not configured yet — missing x-brand-id"`, and search/launch return
`"no casino … endpoint configured"`. Nothing silently falls back to ZA (which would be wrong).
