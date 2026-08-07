# modules/iframe_handler.py
import time
import requests
from modules.utils import (JACKPOT_CITY_ENDPOINTS, HEADERS, API_DELAYS, GAME_STATUSES,
                           region_config, casino_headers)

try:
    from curl_cffi import requests as cffi_requests
except Exception:
    cffi_requests = None


class IframeHandler:
    def get_iframe_url(self, game_id: int, bearer_token: str,
                       brand: str = "betway", region: str = "ZA",
                       category: str = "redtigerroyal") -> str | None:
        cfg = region_config(brand, region)
        url = cfg.get("casino_launch_url")
        if not url:
            raise RuntimeError(f"no casino launch endpoint configured for {brand}/{region}")
        origin = cfg.get("origin") or "https://www.betway.co.za"
        payload = {
            "gameId": str(game_id),
            "channel": "WebMobile",
            "language": "en",
            "isFeaturePhone": False,
            "isMobile": True,
            "isOperaMini": False,
            "requestUrl": origin,
            "returnUrl": origin,
            "timezone": "Asia/Calcutta",
            "launchPlatform": "synapse",
            "category": category,
            "vertical": "casino-games"
        }
        headers = casino_headers(cfg)
        headers["authorization"] = f"Bearer {bearer_token}"
        http = cffi_requests or requests
        kwargs = dict(headers=headers, json=payload, timeout=20, verify=False)
        if cffi_requests is not None:
            kwargs["impersonate"] = "chrome110"
        resp = http.post(url, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        return data.get("iframeUrl")

    def attach_iframes(self, games_list, bearer_token: str, log_callback=None):
        updated = []
        total = len(games_list)
        if log_callback:
            log_callback(f"Attaching iframes for {total} games...")

        for idx, g in enumerate(games_list, start=1):
            name = g.get("gameName")
            if g.get("status") == GAME_STATUSES["FOUND"] and g.get("gameId"):
                try:
                    if log_callback:
                        log_callback(f"[{idx}/{total}] Attaching iframe for {name}...")
                    url = self.get_iframe_url(g["gameId"], bearer_token)
                    g["iframeUrl"] = url
                    g["status"] = GAME_STATUSES["READY"] if url else GAME_STATUSES["IFRAME_FAILED"]
                    if log_callback:
                        if url:
                            log_callback(f"✅ Iframe attached for {name}")
                        else:
                            log_callback(f"❌ Iframe failed for {name}")
                except Exception as e:
                    g["status"] = GAME_STATUSES["IFRAME_ERROR"]
                    if log_callback:
                        log_callback(f"[ERROR] Iframe for {name}: {e}")
            updated.append(g)
            time.sleep(API_DELAYS["IFRAME"])
        return updated
