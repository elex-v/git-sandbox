import yfinance as yf

from portfolio_store import load_history, net_holdings

STOP_LOSS_PCT = 0.10  # flag if price is 10% below average cost basis
TAKE_PROFIT_PCT = 0.30  # flag if price is 30% above average cost basis
SMA_PERIOD_DAYS = 50


def get_price_and_sma(symbol, period_days=SMA_PERIOD_DAYS):
    hist = yf.Ticker(symbol).history(period="4mo")
    if hist.empty or len(hist) < period_days:
        return None, None
    price = float(hist['Close'].iloc[-1])
    sma = float(hist['Close'].tail(period_days).mean())
    return price, sma


def eps_declined_yoy(symbol):
    stmt = yf.Ticker(symbol).income_stmt
    if stmt is None or stmt.empty or 'Diluted EPS' not in stmt.index:
        return None
    eps_row = stmt.loc['Diluted EPS'].dropna()
    if len(eps_row) < 2:
        return None
    latest, prior = eps_row.iloc[0], eps_row.iloc[1]
    return bool(latest < prior)


def check_holding(symbol, position):
    """Returns (triggers, price, avg_cost), or None if price data couldn't be fetched."""
    avg_cost = position["cost"] / position["shares"] if position["shares"] else None
    price, sma = get_price_and_sma(symbol)
    if price is None:
        return None

    triggers = []
    if sma is not None and price < sma:
        triggers.append(f"Trend breakdown: price ${price:.2f} below {SMA_PERIOD_DAYS}d SMA ${sma:.2f}")

    if avg_cost:
        change_pct = (price - avg_cost) / avg_cost
        if change_pct <= -STOP_LOSS_PCT:
            triggers.append(f"Stop-loss: down {abs(change_pct) * 100:.1f}% from avg cost ${avg_cost:.2f}")
        if change_pct >= TAKE_PROFIT_PCT:
            triggers.append(f"Take-profit: up {change_pct * 100:.1f}% from avg cost ${avg_cost:.2f}")

    if position.get("asset_type") != "etf":
        try:
            declined = eps_declined_yoy(symbol)
        except Exception:
            declined = None
        if declined:
            triggers.append("Fundamental deterioration: most recent annual EPS declined YoY")

    return triggers, price, avg_cost


def build_sell_report():
    history = load_history()
    holdings = {k: v for k, v in net_holdings(history).items() if v["shares"] > 1e-6}

    lines = ["=== Sell Signals ==="]
    if not holdings:
        lines.append("  No open positions to check.")
        return "\n".join(lines)

    flagged_any = False
    for symbol in sorted(holdings):
        position = holdings[symbol]
        try:
            result = check_holding(symbol, position)
        except Exception as e:
            lines.append(f"  {symbol}: error checking ({e}), skipped.")
            continue

        if result is None:
            lines.append(f"  {symbol}: couldn't fetch price data, skipped.")
            continue

        triggers, price, avg_cost = result
        if not triggers:
            continue

        flagged_any = True
        gain_pct = ((price - avg_cost) / avg_cost * 100) if avg_cost else None
        header = f"  {symbol}: price ${price:.2f}"
        if gain_pct is not None:
            header += f" ({gain_pct:+.1f}% vs avg cost ${avg_cost:.2f})"
        lines.append(header)
        for trigger in triggers:
            lines.append(f"    - {trigger}")

    if not flagged_any:
        lines.append("  No sell signals triggered this week.")

    return "\n".join(lines)
