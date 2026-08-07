# modules/game_handler.py
import time
import requests
from modules.utils import (JACKPOT_CITY_ENDPOINTS, HEADERS, API_DELAYS, GAME_STATUSES,
                           region_config, casino_headers)

# curl_cffi impersonates a real Chrome TLS fingerprint so the WAF doesn't block the request
# (the plain `requests` calls below get challenged). Used by the typeahead search.
try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None


class GameHandler:
    def search_and_update(self, games_list, bearer_token: str, log_callback=None,
                          brand="betway", region="ZA"):
        updated = []
        total = len(games_list)
        if log_callback:
            log_callback(f"Starting search for {total} games...")

        for idx, game in enumerate(games_list, start=1):
            name = game["gameName"]
            try:
                if log_callback:
                    log_callback(f"[{idx}/{total}] Searching game '{name}'...")
                info = self.search_game(name, bearer_token, brand=brand, region=region)
                if info:
                    game["gameId"] = info.get("id")
                    game["minBetAmount"] = info.get("minBetAmount")
                    game["status"] = GAME_STATUSES["FOUND"]
                    if log_callback:
                        log_callback(f"✅ Found {name} | ID={game['gameId']} | minBet={game['minBetAmount']}")
                else:
                    game["status"] = GAME_STATUSES["NOT_FOUND"]
                    if log_callback:
                        log_callback(f"❌ Game not found: {name}")
                updated.append(game)
            except Exception as e:
                game["status"] = GAME_STATUSES["ERROR"]
                updated.append(game)
                if log_callback:
                    log_callback(f"[ERROR] {name}: {e}")
            time.sleep(API_DELAYS["SEARCH"])
        return updated

    def _search_raw(self, query: str, bearer_token: str, brand: str, region: str, limit: int):
        """Hit the casino Search API for a (brand, region) and return the raw game list.
        Uses curl_cffi (Chrome impersonation) so the WAF doesn't challenge the request."""
        cfg = region_config(brand, region)
        url = cfg.get("casino_search_url")
        if not url:
            raise RuntimeError(f"no casino search endpoint configured for {brand}/{region}")
        params = {
            "search": query.strip(),
            "languageCode": "en-US",
            "channel": "WebMobile",
            "skip": 0,
            "limit": limit,
            "currency": cfg["currency"],
            "regionCode": cfg["region_code"],
            "vertical": "casino-games",
            "environment": "Production",
        }
        headers = casino_headers(cfg)
        headers["authorization"] = f"Bearer {bearer_token}"
        http = cffi_requests or requests
        kwargs = dict(headers=headers, params=params, timeout=15, verify=False)
        if cffi_requests is not None:
            kwargs["impersonate"] = "chrome110"
        resp = http.get(url, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        # v4 Search (all regions) nests games under verticals[].data[]; v3 (legacy, ZA-only)
        # returned a flat data[]. Accept either so the shape can't silently return zero games.
        games = data.get("data") or []
        if not games and isinstance(data.get("verticals"), list):
            for v in data["verticals"]:
                games += v.get("data") or []
        return games

    def search_games_list(self, query: str, bearer_token: str, region: str = "ZA",
                          brand: str = "betway", currency: str = None, limit: int = 10):
        """Live typeahead search: return a LIST of matching games (not just one best match).
        Returns [{"id", "name", "minBetAmount"}], ordered as the API returns them."""
        if not query or not query.strip():
            return []
        games = self._search_raw(query, bearer_token, brand, region, limit)
        out = []
        for g in games:
            name = g.get("name")
            if not name:
                continue
            out.append({"id": g.get("id"), "name": name, "minBetAmount": g.get("minBetAmount")})
        return out[:limit]

    def search_game(self, game_name: str, bearer_token: str, brand: str = "betway", region: str = "ZA"):
        games = self._search_raw(game_name, bearer_token, brand, region, 15)
        lower = game_name.lower()
        match = next((g for g in games if g.get("name", "").lower() == lower), None)
        if not match:
            match = next((g for g in games if lower in g.get("name", "").lower()), None)
        if not match:
            match = next((g for g in games if g.get("name", "").lower() in lower), None)
        if match:
            # Provider is best-effort (field name varies by API shape); the DSC report uses it.
            provider = next((match.get(k) for k in
                             ("provider", "providerName", "providerTitle", "vendor", "studio")
                             if match.get(k)), None)
            # Game type/category (also best-effort) — DSC uses it to skip the bet flow
            # on non-slot games (live/table/crash) instead of failing them.
            game_type = next((match.get(k) for k in
                              ("gameType", "category", "categoryName", "categories",
                               "type", "genre", "subVertical")
                              if match.get(k)), None)
            if isinstance(game_type, (list, tuple)):
                game_type = ", ".join(str(x) for x in game_type)
            return {"id": match.get("id"), "minBetAmount": match.get("minBetAmount"),
                    "provider": provider, "game_type": game_type}
        return None
