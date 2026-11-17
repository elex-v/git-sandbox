import json
import os

import yfinance as yf

HISTORY_PATH = "data/portfolio_history.json"
RECOMMENDATIONS_PATH = "data/recommendations.json"


def _load_json_list(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_history():
    return _load_json_list(HISTORY_PATH)


def save_history(history):
    _save_json(HISTORY_PATH, history)


def load_recommendations():
    return _load_json_list(RECOMMENDATIONS_PATH)


def save_recommendations(recommendations):
    _save_json(RECOMMENDATIONS_PATH, recommendations)


def detect_asset_type(symbol):
    try:
        quote_type = yf.Ticker(symbol).info.get("quoteType")
    except Exception:
        quote_type = None
    return "etf" if quote_type == "ETF" else "equity"


def net_holdings(history):
    """Aggregate history into net per-symbol positions: buys add shares/cost,
    sells subtract them. Older entries without transaction_type are all buys."""
    by_symbol = {}
    for entry in history:
        sign = -1 if entry.get("transaction_type", "buy") == "sell" else 1
        agg = by_symbol.setdefault(
            entry["symbol"], {"shares": 0.0, "cost": 0.0, "asset_type": entry.get("asset_type", "equity")}
        )
        agg["shares"] += sign * entry["shares"]
        agg["cost"] += sign * entry["cost"]
    return by_symbol
