"""
provider_sweep.py — the heart of the auto-DSC: enumerate every provider a (brand, region)
offers, then pick ONE game per provider to test, honouring the weekly rotation.

Both brands expose the same casino API shape (Betway on casinoapic.betwayafrica.com, JPC on
apic.jpc.africa) — we derive the host from region_config().casino_search_url, so this is fully
brand/region-generic:
  • providers:      GET {base}/api/v4/Gaming/Game/Categories   -> {"providers": [...]}
  • provider games: GET {base}/api/v3/Gaming/Provider/Games/?provider=... -> {"data": [...]}
regionCode/currency come from region_config (J-prefixed for JPC). x-brand-id is sent when the
region needs it (JPC). Calls go through curl_cffi so the WAF doesn't challenge them.

list_crash_games() below has a fallback: the curated "crashgames" Categories bucket (recon
2026-07-27) only comes back populated for Betway ZA — every other brand/region we've checked
returns 0 categories from that same endpoint even though crash titles are demonstrably live
there (GameHandler.search_game finds Aviator/JetX/Spaceman/Crash everywhere). Rather than wait
on that curation being fixed upstream, list_crash_games() falls back to walking every provider
(get_providers + list_provider_games) and classifying crash titles itself when the bucket is empty.
"""
import re
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = {"impersonate": "chrome110"}
except Exception:
    import requests as _http
    _IMPERSONATE = {}

from modules.utils import region_config, casino_headers


def _casino_base(cfg):
    """https://{casino-host} from the region's search URL (casinoapic… / apic.jpc.africa…)."""
    u = urlparse(cfg.get("casino_search_url") or "")
    return f"{u.scheme}://{u.netloc}" if u.netloc else None


def _headers(cfg, token):
    h = casino_headers(cfg)          # carries x-brand-id when the region needs it (JPC)
    h["authorization"] = f"Bearer {token}"
    return h


def _categories_payload(brand, region, token):
    """Raw /api/v4/Gaming/Game/Categories response — {"providers":[...], "categories":[...],
    "themes":[...]}. Shared by get_providers() (reads .providers) and list_crash_games()
    (reads .categories) so both hit the endpoint the same way instead of duplicating it."""
    cfg = region_config(brand, region)
    base = _casino_base(cfg)
    if not base:
        raise RuntimeError(f"no casino API base for {brand}/{region}")
    params = {"languageCode": "en-US", "channel": "WebDesktop", "currency": cfg["currency"],
              "regionCode": cfg["region_code"], "vertical": "casino-games", "environment": "Production"}
    r = _http.get(f"{base}/api/v4/Gaming/Game/Categories", headers=_headers(cfg, token),
                  params=params, timeout=20, verify=False, **_IMPERSONATE)
    r.raise_for_status()
    return r.json() or {}


def get_providers(brand, region, token):
    """Every provider offered for this (brand, region), in the API's own order."""
    return _categories_payload(brand, region, token).get("providers") or []


def list_crash_games(brand, region, token):
    """Every crash title for this (brand, region). Fast path: the Categories payload's curated
    'crashgames' bucket (id/name/provider/minBetAmount included) — one call, no provider walk.
    That bucket is currently only populated for Betway ZA (recon 2026-07-27), so when it's
    empty/missing we fall back to walking every provider and classifying crash titles ourselves
    (see module docstring for why). Returns [{"id","name","provider","minBetAmount"}],
    unavailable titles dropped."""
    cats = _categories_payload(brand, region, token).get("categories") or []
    bucket = next((c for c in cats if (c.get("name") or "").lower() == "crashgames"), None)
    if bucket:
        out = []
        for g in bucket.get("games") or []:
            if g.get("unavailable"):
                continue
            out.append({"id": g.get("id"), "name": g.get("name"),
                        "provider": g.get("provider"), "minBetAmount": g.get("minBetAmount")})
        if out:
            return out
    return _crash_games_via_provider_walk(brand, region, token)


def _game_provider_and_type(g):
    """Best-effort provider/type extraction, mirroring GameHandler.search_game()'s field
    checks so the two crash-classification paths (search vs. catalog walk) agree."""
    provider = next((g.get(k) for k in
                     ("provider", "providerName", "providerTitle", "vendor", "studio")
                     if g.get(k)), None)
    game_type = next((g.get(k) for k in
                      ("gameType", "category", "categoryName", "categories", "type",
                       "genre", "subVertical")
                      if g.get(k)), None)
    if isinstance(game_type, (list, tuple)):
        game_type = ", ".join(str(x) for x in game_type)
    return provider, game_type


def _is_crash_game(g):
    """Conservative crash check for the provider-walk fallback: unlike _is_slot()'s permissive
    default, a game with NO type-ish field at all is NOT treated as crash — these results drive
    real wagers, so a false positive is worse than a missed title."""
    _, game_type = _game_provider_and_type(g)
    return bool(game_type) and "crash" in str(game_type).lower()


def _crash_games_via_provider_walk(brand, region, token, max_workers=8):
    """Fallback used by list_crash_games() when the curated 'crashgames' Categories bucket is
    empty for this (brand, region). Walks every provider concurrently (list_provider_games is a
    plain synchronous HTTP call, and there can be 40+ providers) and keeps only games that look
    like crash titles. De-duplicated by game id."""
    providers = get_providers(brand, region, token)
    if not providers:
        return []
    found = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(providers))) as pool:
        futures = {pool.submit(list_provider_games, brand, region, token, p): p for p in providers}
        for fut in as_completed(futures):
            provider = futures[fut]
            try:
                games = fut.result()
            except Exception:
                continue
            for g in games:
                if g.get("unavailable") or g.get("id") is None or not _is_crash_game(g):
                    continue
                provider_name, _ = _game_provider_and_type(g)
                found.setdefault(g["id"], {"id": g.get("id"), "name": g.get("name"),
                                           "provider": provider_name or provider,
                                           "minBetAmount": g.get("minBetAmount")})
    return list(found.values())


def list_provider_games(brand, region, token, provider, limit=1000):
    """All games a provider offers for this (brand, region)."""
    cfg = region_config(brand, region)
    base = _casino_base(cfg)
    params = {"regionCode": cfg["region_code"], "provider": provider, "channel": "WebDesktop",
              "languageCode": "en-US", "limit": limit, "skip": 0, "vertical": "casino-games",
              "currency": cfg["currency"], "appVersion": 0, "environment": "Production"}
    r = _http.get(f"{base}/api/v3/Gaming/Provider/Games/", headers=_headers(cfg, token),
                  params=params, timeout=25, verify=False, **_IMPERSONATE)
    r.raise_for_status()
    j = r.json()
    return (j.get("data") if isinstance(j, dict) else j) or []


def _is_slot(g):
    """DSC targets slots. gameType varies ('Slots','Video Slots',…); treat live/table/crash as
    non-slot. Best-effort — a missing gameType is allowed through (the DSC run classifies it)."""
    gt = (g.get("gameType") or "").lower()
    if not gt:
        return True
    return "slot" in gt or gt in ("", "video slots")


def pick_game(brand, region, token, provider, exclude_ids=(), prefer_new=True):
    """Choose ONE game for this provider to test, honouring rotation.
    Rules, in order: skip games in `exclude_ids` (the weekly-cooldown set) and any 'unavailable'
    game; prefer a NEW / promoted / featured title (that's the 'top game that shows when you click
    a provider'), else the API's first (top) game. Returns the game dict or None if the provider
    has nothing testable left this week."""
    games = [g for g in list_provider_games(brand, region, token, provider)
             if str(g.get("id")) not in set(map(str, exclude_ids))
             and not g.get("unavailable") and _is_slot(g)]
    if not games:
        return None
    if prefer_new:
        for key in ("isNew", "isPromoted", "featured"):
            hit = next((g for g in games if g.get(key)), None)
            if hit:
                return hit
    return games[0]


if __name__ == "__main__":
    # Live smoke test across ALL configured brand/regions (uses AuthHandler for tokens).
    import sys, os, json
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from modules.auth_handler import AuthHandler

    # Credentials live in provider_sweep_accounts.json (gitignored), not in source.
    _accts_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "provider_sweep_accounts.json")
    with open(_accts_path, "r", encoding="utf-8") as _fh:
        ACCTS = [tuple(row) for row in json.load(_fh)]
    for brand, region, u, pw in ACCTS:
        auth = AuthHandler().authenticate(u, pw, brand=brand, region=region)
        if not auth.get("success"):
            print(f"[{brand}/{region}] AUTH FAIL: {str(auth.get('message'))[:60]}"); continue
        tok = auth["token"]
        try:
            provs = get_providers(brand, region, tok)
            first = provs[0] if provs else None
            g = pick_game(brand, region, tok, first, exclude_ids=[]) if first else None
            print(f"[{brand}/{region}] {len(provs)} providers | e.g. pick for '{first}': "
                  f"{g.get('name') if g else None} (id={g.get('id') if g else None})")
        except Exception as e:
            print(f"[{brand}/{region}] ERROR: {str(e)[:80]}")
