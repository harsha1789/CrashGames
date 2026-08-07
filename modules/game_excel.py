# modules/game_excel.py
import pandas as pd
from typing import List, Dict

COLUMNS_VARIANTS = {
    "gameName": ["Game Name", "GameName", "Name", "Game"],
    "provider": ["Provider", "Provider Name", "ProviderName"],
    "gameType": ["Game Type", "GameType", "Type"],
    "srNo": ["Sr. No.", "Sr No.", "Sr No", "SrNo", "Serial", "S.No"],
}

def _extract_value(row: dict, keys: list):
    for k in keys:
        if k in row and pd.notna(row[k]):
            return str(row[k]).strip()
    return None

def _norm(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


_GAME_HEADERS = {_norm(v) for v in COLUMNS_VARIANTS["gameName"]}


def _read_sheet(file_path: str) -> pd.DataFrame:
    """Read the sheet that actually holds the games. Team sheets put title/blank rows
    above the header and sometimes keep games on a non-first tab — scan every sheet's
    top rows for a Game Name header instead of trusting (sheet 0, row 0)."""
    df = pd.read_excel(file_path, engine="openpyxl")
    if any(_norm(c) in _GAME_HEADERS for c in df.columns):
        return df
    xl = pd.ExcelFile(file_path, engine="openpyxl")
    for sheet in xl.sheet_names:
        raw = xl.parse(sheet, header=None, nrows=15)
        for i in range(len(raw)):
            if any(_norm(v) in _GAME_HEADERS for v in raw.iloc[i].tolist()):
                return xl.parse(sheet, header=i)
    return df   # nothing better found — let the caller's column checks report it


def parse_excel(file_path: str, log_callback=None) -> List[Dict]:
    if log_callback:
        log_callback(f"Loading Excel file: {file_path}")

    df = _read_sheet(file_path)
    if df.empty:
        raise ValueError("No data found in Excel file")

    rows = []
    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        game_name = _extract_value(row_dict, COLUMNS_VARIANTS["gameName"])
        if not game_name:
            continue
        game = {
            "srNo": _extract_value(row_dict, COLUMNS_VARIANTS["srNo"]) or idx + 1,
            "provider": _extract_value(row_dict, COLUMNS_VARIANTS["provider"]) or "Unknown",
            "gameName": game_name,
            "gameType": _extract_value(row_dict, COLUMNS_VARIANTS["gameType"]) or "Unknown",
            "gameId": None,
            "minBetAmount": None,
            "iframeUrl": None,
            "status": "pending",
        }
        rows.append(game)
        if log_callback:
            log_callback(f"Row {idx+1}: loaded game '{game_name}'")

    if not rows:
        raise ValueError('No valid rows. Ensure there is a "Game Name" column with values.')

    if log_callback:
        log_callback(f"✅ Parsed {len(rows)} games from Excel.")
    return rows
