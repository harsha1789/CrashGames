# modules/account_manager.py
import json
import os
import time
from datetime import datetime
from typing import List, Dict, Optional

BASE = os.path.dirname(os.path.dirname(__file__))  # project root
ACCOUNTS_PATH = os.path.join(BASE, "data/accounts.json")
DETAILS_PATH = os.path.join(BASE, "data/accountdetails.json")

def load_accounts() -> List[Dict]:
    """Load credentials only (username/password). Returns list of dicts."""
    if not os.path.exists(ACCOUNTS_PATH):
        return []
    with open(ACCOUNTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_accounts(accounts: List[Dict]):
    with open(ACCOUNTS_PATH, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)

def append_account(username: str, password: str):
    accounts = load_accounts()
    # avoid duplicates
    for a in accounts:
        if a.get("username") == username:
            raise ValueError("Account already exists")
    accounts.append({"username": username, "password": password})
    save_accounts(accounts)

# accountdetails.json helpers
def load_account_details() -> List[Dict]:
    if not os.path.exists(DETAILS_PATH):
        return []
    with open(DETAILS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_account_details(details: List[Dict]):
    with open(DETAILS_PATH, "w", encoding="utf-8") as f:
        json.dump(details, f, indent=2)

def update_account_detail(username: str, token: Optional[str], balance: Optional[float], extra: Optional[Dict] = None):
    """
    Update or add account detail entry. Saves updatedAt timestamp.
    """
    details = load_account_details()
    found = False
    for d in details:
        if d.get("username") == username:
            d["token"] = token
            d["balance"] = balance
            d["updatedAt"] = datetime.utcnow().isoformat()
            if extra:
                d.update(extra)
            found = True
            break
    if not found:
        entry = {"username": username, "token": token, "balance": balance, "updatedAt": datetime.utcnow().isoformat()}
        if extra:
            entry.update(extra)
        details.append(entry)
    save_account_details(details)

def get_account_detail(username: str) -> Optional[Dict]:
    for d in load_account_details():
        if d.get("username") == username:
            return d
    return None
