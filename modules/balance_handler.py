# modules/balance_handler.py
import logging
import requests
from modules.utils import JACKPOT_CITY_ENDPOINTS, HEADERS

logger = logging.getLogger(__name__)

class BalanceHandler:
    def get_balance(self, token: str, timeout: int = 15) -> dict:
        """
        Call balance endpoint using 'Bearer <token>' header.
        Returns { success: bool, cashBalance: float|None, message: str, raw: ... }
        """
        try:
            headers = {
                "accept": "*/*",
                "authorization": f"Bearer {token}",
                "origin": "https://www.jackpotcity.co.za",
                "referer": "https://www.jackpotcity.co.za/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
                "x-brand-id": "D76A0E62-B728-4A28-8134-C57A1A003199",
            }
            logger.debug("Balance headers: authorization present")

            resp = requests.get(JACKPOT_CITY_ENDPOINTS["BALANCE"], headers=headers, timeout=timeout, verify=False)
            logger.debug("Balance response status: %s", resp.status_code)

            # --- DEBUG: log first 200 chars of raw response
            logger.debug("Balance raw response (truncated): %s", resp.text[:200])

            try:
                data = resp.json()
            except Exception as e:
                logger.error("Invalid JSON from balance endpoint: %s", e)
                logger.error("Raw response was: %s", resp.text)
                return {
                    "success": False,
                    "message": f"Invalid JSON: {e}",
                    "raw": resp.text,
                }

            if resp.ok and data.get("isSuccessful"):
                cash = data.get("data", {}).get("cashBalance")
                logger.info("Balance fetched for token %s: %s", token[:8], cash)
                return {"success": True, "cashBalance": cash, "raw": data}
            else:
                logger.warning("Balance error response: %s", data)
                return {
                    "success": False,
                    "message": data.get("error") or f"Balance error (status {resp.status_code})",
                    "raw": data,
                }

        except requests.RequestException as e:
            logger.exception("Network error while fetching balance")
            return {"success": False, "message": f"Network error: {e}"}