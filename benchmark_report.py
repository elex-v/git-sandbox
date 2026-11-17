from datetime import date, timedelta

import yfinance as yf

from portfolio_store import load_history, load_recommendations

BENCHMARK_SYMBOL = "IWM"  # Russell 2000 ETF
PICK_MATCH_WINDOW_DAYS = 14  # a buy counts as a screener pick if recommended up to 2 weeks before
MIN_PICKS_TO_JUDGE = 15
MIN_DAYS_TO_JUDGE = 90


def screener_trades(history, recommendations):
    """Confirmed buys of a symbol the screener recommended shortly beforehand, plus any
    later sells of those symbols, oldest first. Manual buys (ETFs, CMCSA, etc.) are excluded."""
    rec_dates = {}
    for rec in recommendations:
        rec_dates.setdefault(rec["symbol"], []).append(date.fromisoformat(rec["date"]))

    def is_screener_buy(entry):
        buy_date = date.fromisoformat(entry["date"])
        return any(
            timedelta(0) <= buy_date - d <= timedelta(days=PICK_MATCH_WINDOW_DAYS)
            for d in rec_dates.get(entry["symbol"], [])
        )

    trades = []
    screener_symbols = set()
    for entry in sorted(history, key=lambda e: e["date"]):
        if entry.get("transaction_type", "buy") == "buy":
            if is_screener_buy(entry):
                screener_symbols.add(entry["symbol"])
                trades.append(entry)
        elif entry["symbol"] in screener_symbols:
            trades.append(entry)
    return trades


def closes(symbols, start):
    data = yf.download(sorted(symbols), start=start.isoformat(), progress=False, auto_adjust=True)["Close"]
    return data.ffill()


def close_on_or_before(series, day):
    series = series.dropna()
    return float(series.loc[:day.isoformat()].iloc[-1])


def build_benchmark_report():
    lines = [f"=== Screener Picks vs {BENCHMARK_SYMBOL} ==="]
    trades = screener_trades(load_history(), load_recommendations())
    if not trades:
        lines.append("  No confirmed buys of screener picks yet.")
        return "\n".join(lines)

    first_day = date.fromisoformat(trades[0]["date"])
    prices = closes({t["symbol"] for t in trades} | {BENCHMARK_SYMBOL}, first_day - timedelta(days=7))
    bench = prices[BENCHMARK_SYMBOL]

    # Shadow portfolio: every dollar put into a pick also "buys" IWM that same day; every
    # sell of a pick sells the same fraction of that pick's shadow IWM shares.
    positions = {}  # symbol -> open shares, their cost basis, and the matching shadow IWM shares
    invested = realized = bench_realized = 0.0
    for t in trades:
        pos = positions.setdefault(t["symbol"], {"shares": 0.0, "cost": 0.0, "bench_shares": 0.0})
        bench_price = close_on_or_before(bench, date.fromisoformat(t["date"]))
        if t.get("transaction_type", "buy") == "buy":
            pos["shares"] += t["shares"]
            pos["cost"] += t["cost"]
            pos["bench_shares"] += t["cost"] / bench_price
            invested += t["cost"]
        else:
            fraction = min(t["shares"] / pos["shares"], 1.0) if pos["shares"] else 0.0
            sold_bench = pos["bench_shares"] * fraction
            pos["shares"] -= t["shares"]
            pos["cost"] -= pos["cost"] * fraction
            pos["bench_shares"] -= sold_bench
            realized += t["shares"] * t["price"]
            bench_realized += sold_bench * bench_price

    bench_now = float(bench.dropna().iloc[-1])
    value, bench_value = realized, bench_realized
    for symbol in sorted(positions):
        p = positions[symbol]
        if p["shares"] <= 1e-6:
            continue
        pos_value = p["shares"] * float(prices[symbol].dropna().iloc[-1])
        pos_bench_value = p["bench_shares"] * bench_now
        value += pos_value
        bench_value += pos_bench_value
        lines.append(
            f"  {symbol}: {(pos_value / p['cost'] - 1) * 100:+.1f}% vs {BENCHMARK_SYMBOL} "
            f"{(pos_bench_value / p['cost'] - 1) * 100:+.1f}% on the same dollars (${p['cost']:,.2f} cost)"
        )

    pick_return = value / invested - 1
    bench_return = bench_value / invested - 1
    lines.append(
        f"  Picks: ${invested:,.2f} invested -> ${value:,.2f} ({pick_return * 100:+.1f}%) | "
        f"same dollars in {BENCHMARK_SYMBOL} on the same days -> ${bench_value:,.2f} ({bench_return * 100:+.1f}%)"
    )
    lines.append(f"  Difference: {(pick_return - bench_return) * 100:+.1f} percentage points vs {BENCHMARK_SYMBOL}")

    buys = [t for t in trades if t.get("transaction_type", "buy") == "buy"]
    days_held = (date.today() - first_day).days
    if len({t["symbol"] for t in buys}) < MIN_PICKS_TO_JUDGE or days_held < MIN_DAYS_TO_JUDGE:
        lines.append(
            f"  Too early to judge the strategy: {len({t['symbol'] for t in buys})} pick(s) over {days_held} day(s) "
            f"(wait for {MIN_PICKS_TO_JUDGE}+ picks and {MIN_DAYS_TO_JUDGE}+ days)."
        )
    return "\n".join(lines)
