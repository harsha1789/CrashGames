# modules/auth_handler.py
import os
import secrets
import logging
from curl_cffi import requests
from modules.utils import region_config, auth_headers, add_country_code

logger = logging.getLogger(__name__)

class AuthHandler:
    def authenticate(self, username: str, password: str, brand: str = "betway",
                     region: str = "ZA", timeout: int = 15) -> dict:
        """
        Attempt login for a (brand, region). Returns { success, token, message, raw }.
        Region-aware: domain, countryCode, dialling-code username and x-brand-id all come from
        region_config(brand, region). If that region has no x-brand-id configured yet, fail early
        with a clear message rather than sending a ZA request that would be rejected.
        """
        try:
            cfg = region_config(brand, region)
            if not cfg.get("auth_url"):
                return {"success": False, "message": f"no auth endpoint configured for {brand}/{region}"}
            # x-brand-id is NOT required — Betway auth accepts domain + credentials alone
            # (verified 2026-07-17: GH/NG/ZM/TZ all return a token without it). It is still
            # sent when known (see auth_headers), but its absence no longer blocks a region.
            if not cfg.get("configured"):
                return {"success": False,
                        "message": f"{brand}/{region} not configured — no site origin/casino "
                                   f"endpoint (add it to modules/utils.py)"}
            # Username: normalized to the region dial code by default. Some accounts authenticate
            # ONLY with the raw number as the site actually sends it (e.g. the ZA account 830099887,
            # whose live browser payload used the number as-is, NOT 27-prefixed) — set
            # GAMEGUARD_USERNAME_RAW=1 to pass it through unprefixed.
            if os.environ.get("GAMEGUARD_USERNAME_RAW"):
                formatted = (username or "").strip().lstrip("+")
            else:
                formatted = add_country_code(username, region)
            # sessionMetadata.uip is the CLIENT-reported IP; sessionTrackingToken a per-session id.
            # MUST be unique per login: two authentications sharing one sessionTrackingToken get
            # treated server-side as the same device/tab holding two sessions, which surfaces
            # downstream as a live-game "session held by another tab/browser" disconnect (confirmed
            # 2026-07-23 — a hardcoded literal here caused instant DISCONNECTED on the crash vertical
            # whenever two accounts authenticated close together). Generate a fresh one per call;
            # GAMEGUARD_SESSION_TRACKING_TOKEN still overrides for reproducing a specific captured session.
            # Betway geo-gates by region: a ZA account authenticated from a non-ZA IP is rejected
            # (HTTP 401 InvalidUserNameOrPassword — the exact failure seen with the hardcoded India
            # IP below). Override these with values captured from a real logged-in session for the
            # target region:
            #   GAMEGUARD_UIP                    — the client IP the site reports (must match the region)
            #   GAMEGUARD_SESSION_TRACKING_TOKEN — the sessionMetadata.sessionTrackingToken
            # IMPORTANT: if Betway validates the TRUE connecting IP (not just this field), the run
            # must ALSO egress from a same-region IP (VPN/proxy) — a payload override alone won't do it.
            payload = {
                "username": formatted,
                "password": password,
                "countryCode": cfg["country_code"],
                "sessionMetadata": {
                    "sessionTrackingToken": os.environ.get(
                        "GAMEGUARD_SESSION_TRACKING_TOKEN", secrets.token_hex(20).upper()),
                    "appType": "",
                    "appsFlyerExternalRef": "",
                    "uip": os.environ.get("GAMEGUARD_UIP", "115.110.105.36"),
                }
            }
            logger.debug("Auth payload for %s/%s %s: %s", brand, region, username,
                         {"username": formatted, "password": "****"})
            resp = requests.post(cfg["auth_url"], headers=auth_headers(cfg), json=payload,
                                 timeout=timeout, verify=False, impersonate="chrome110")
            logger.debug("Auth response status for %s: %s", username, resp.status_code)
            try:
                data = resp.json()
            except Exception as e:
                # The server didn't return JSON. Surface the status + a body snippet so the real
                # cause is visible (empty body, Cloudflare/WAF challenge, redirect, rate-limit).
                body = (resp.text or "").strip()
                ctype = resp.headers.get("content-type", "?")
                snippet = body[:300].replace("\n", " ") if body else "<EMPTY BODY>"
                low = body.lower()
                if not body:
                    hint = "empty response body"
                elif "<html" in low or "text/html" in ctype:
                    hint = "HTML page (likely Cloudflare/WAF block, captcha, or a redirect — not the API)"
                elif resp.status_code in (401, 403):
                    hint = "rejected (401/403): bad credentials, blocked IP/geo, or stale x-brand-id/token"
                elif resp.status_code == 429:
                    hint = "rate limited (429): back off and retry"
                elif resp.status_code >= 500:
                    hint = f"server error {resp.status_code}: upstream is down/unstable"
                else:
                    hint = "non-JSON response"
                logger.error("Non-JSON auth response for %s: status=%s ctype=%s body=%r",
                             username, resp.status_code, ctype, snippet)
                return {"success": False,
                        "message": f"Invalid JSON ({hint}) [HTTP {resp.status_code}, {ctype}]: {snippet}",
                        "status": resp.status_code, "raw": resp.text}

            token = None
            if data.get("token"):
                token = data["token"]
            elif data.get("access_token"):
                token = data["access_token"]
            elif data.get("data", {}).get("jwtToken"):
                token = data["data"]["jwtToken"]
            elif data.get("Token"):
                token = data["Token"]

            if resp.ok and token:
                logger.info("Auth success for %s", username)
                return {"success": True, "token": token, "raw": data}
            else:
                err = data.get("error") or data
                logger.warning("Auth failed for %s: %s", username, err)
                return {"success": False, "message": str(err), "raw": data}
        except requests.RequestsError as e:
            # curl_cffi's base request error (NOT requests.RequestException — that name only
            # exists in curl_cffi.requests.exceptions, and referencing it here crashed the
            # 2026-07-10 batch when a DNS failure needed this handler).
            logger.exception("Network error during auth for %s", username)
            return {"success": False, "message": f"Network error: {e}"}
