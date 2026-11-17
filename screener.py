import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import yfinance as yf
from yfinance import EquityQuery

from benchmark_report import build_benchmark_report
from portfolio_store import load_history, load_recommendations, net_holdings, save_recommendations
from sell_checker import build_sell_report

SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')
EMAIL_PASSWORD = os.environ.get('GMAIL_APP_KEY')

CAP_TIERS = [
    {"name": "Small-Cap", "min_cap": 700_000_000, "max_cap": 1_200_000_000, "budget": 500},
    {"name": "Mid-Cap", "min_cap": 1_200_000_000, "max_cap": 15_000_000_000, "budget": 300},
]

EPS_GROWTH_YEARS = 3
MAX_EPS_CHECKS = 150  # bounds runtime for the per-ticker EPS lookups, per tier
PAGE_SIZE = 250  # Yahoo's screener page size limit
US_EXCHANGES = ["NMS", "NYQ", "ASE"]  # Nasdaq, NYSE, NYSE American

MAX_EXTENSION_ABOVE_50SMA = 0.15  # skip names already >15% above their 50d SMA (chasing a breakout)
MIN_DOLLAR_VOLUME = 5_000_000  # avg $ volume/day, so a weekly order is negligible vs liquidity
EARNINGS_BLACKOUT_DAYS = 7  # skip names reporting earnings within the next week
MAX_PEG = 2.0  # trailing P/E divided by EPS CAGR%; keeps growth from being overpaid for

TOP_N_FOR_ALLOCATION = 5
REBUY_COOLDOWN_WEEKS = 8  # don't rebuy a name already confirmed-bought in the last ~2 months


def require_env(name, value):
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")


def get_candidates(min_cap, max_cap):
    query = EquityQuery('and', [
        EquityQuery('gte', ['intradaymarketcap', min_cap]),
        EquityQuery('lte', ['intradaymarketcap', max_cap]),
        EquityQuery('is-in', ['exchange', *US_EXCHANGES]),
    ])

    quotes = []
    offset = 0
    total = None
    while total is None or offset < total:
        page = yf.screen(query, size=PAGE_SIZE, offset=offset, sortField='intradaymarketcap', sortAsc=False)
        total = page.get('total', 0)
        quotes.extend(page.get('quotes', []))
        offset += PAGE_SIZE

    return quotes


def is_in_uptrend(quote):
    price = quote.get('regularMarketPrice')
    sma50 = quote.get('fiftyDayAverage')
    sma200 = quote.get('twoHundredDayAverage')
    if price is None or sma50 is None or sma200 is None:
        return False
    return price > sma50 and price > sma200 and sma50 > sma200


def is_overextended(quote):
    price = quote['regularMarketPrice']
    sma50 = quote['fiftyDayAverage']
    return (price - sma50) / sma50 > MAX_EXTENSION_ABOVE_50SMA


def has_sufficient_liquidity(quote):
    price = quote.get('regularMarketPrice')
    volume = quote.get('averageDailyVolume3Month')
    if price is None or volume is None:
        return False
    return price * volume >= MIN_DOLLAR_VOLUME


def has_upcoming_earnings(quote):
    ts = quote.get('earningsTimestampStart')
    if ts is None:
        return False  # unknown date; don't block on missing data
    earnings_date = datetime.fromtimestamp(ts, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return now <= earnings_date <= now + timedelta(days=EARNINGS_BLACKOUT_DAYS)


def passes_bulk_filters(quote):
    return (
        is_in_uptrend(quote)
        and not is_overextended(quote)
        and has_sufficient_liquidity(quote)
        and not has_upcoming_earnings(quote)
    )


def eps_growth_cagr(symbol, years=EPS_GROWTH_YEARS):
    stmt = yf.Ticker(symbol).income_stmt
    if stmt is None or stmt.empty or 'Diluted EPS' not in stmt.index:
        return None

    # columns are most-recent-year first; need years+1 data points to confirm `years` YoY increases
    eps_row = stmt.loc['Diluted EPS'].dropna()
    if len(eps_row) < years + 1:
        return None
    eps_values = list(eps_row.iloc[:years + 1])[::-1]  # oldest -> newest

    if not all(eps_values[i] < eps_values[i + 1] for i in range(len(eps_values) - 1)):
        return None

    oldest, newest = eps_values[0], eps_values[-1]
    if oldest <= 0:
        return None  # CAGR isn't meaningful starting from a loss

    return (newest / oldest) ** (1 / years) - 1


def compute_peg(quote, eps_cagr):
    trailing_pe = quote.get('trailingPE')
    if trailing_pe is None or trailing_pe <= 0 or eps_cagr <= 0:
        return None
    return trailing_pe / (eps_cagr * 100)


def recently_bought_symbols():
    history = load_history()
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=REBUY_COOLDOWN_WEEKS)).date()
    return {
        entry["symbol"] for entry in history
        if entry.get("transaction_type", "buy") == "buy"
        and datetime.fromisoformat(entry["date"]).date() >= cutoff
    }


def record_recommendations(tier_name, positions):
    """Log this week's picks so later confirmed buys can be attributed to the screener.
    Re-running on the same day doesn't add duplicates."""
    today = datetime.now(timezone.utc).date().isoformat()
    recommendations = load_recommendations()
    seen = {(r["date"], r["symbol"]) for r in recommendations}
    for pos in positions:
        if (today, pos["symbol"]) not in seen:
            recommendations.append({"date": today, "symbol": pos["symbol"], "tier": tier_name})
    save_recommendations(recommendations)


def allocate_budget(picks, budget):
    """Whole-share allocation across ranked picks: one share each where affordable,
    then greedily top up remaining cash into the best-ranked names that still fit."""
    positions = []
    remaining = budget
    for pick in picks:
        price = pick["price"]
        if price <= remaining:
            positions.append({**pick, "shares": 1, "cost": price})
            remaining -= price

    changed = True
    while changed and positions:
        changed = False
        for pos in positions:
            if pos["price"] <= remaining:
                pos["shares"] += 1
                pos["cost"] += pos["price"]
                remaining -= pos["price"]
                changed = True

    return positions, remaining


def screen_tier(tier, recent_symbols):
    print(f"\n[{tier['name']}] Screening ${tier['min_cap']:,} - ${tier['max_cap']:,}...")

    candidates = get_candidates(tier["min_cap"], tier["max_cap"])
    print(f"[{tier['name']}] {len(candidates)} candidates in cap range.")

    trending = [c for c in candidates if is_in_uptrend(c)]
    print(f"[{tier['name']}] {len(trending)} in an uptrend.")

    bulk_filtered = [c for c in trending if passes_bulk_filters(c)]
    print(f"[{tier['name']}] {len(bulk_filtered)} passed liquidity, extension, and earnings-date filters.")

    if len(bulk_filtered) > MAX_EPS_CHECKS:
        print(f"[{tier['name']}] Capping to the {MAX_EPS_CHECKS} largest by market cap to bound runtime.")
        bulk_filtered = bulk_filtered[:MAX_EPS_CHECKS]

    qualified = []
    for company in bulk_filtered:
        symbol = company.get("symbol")
        if not symbol:
            continue
        name = company.get("shortName", symbol)

        try:
            cagr = eps_growth_cagr(symbol)
            if cagr is None:
                continue
            peg = compute_peg(company, cagr)
            if peg is None or peg > MAX_PEG:
                continue
            qualified.append({
                "symbol": symbol,
                "name": name,
                "price": company.get("regularMarketPrice"),
                "market_cap": company.get("marketCap"),
                "eps_cagr": cagr,
                "peg": peg,
            })
            print(f"[{tier['name']}]   -> {symbol} qualifies (PEG {peg:.2f})")
        except Exception as e:
            print(f"[{tier['name']}]   -> Skipping {symbol}: {e}")

    qualified.sort(key=lambda c: c["peg"])
    print(f"[{tier['name']}] {len(qualified)} passed EPS growth + valuation checks.")

    eligible = [c for c in qualified if c["symbol"] not in recent_symbols]
    runners_up = [c for c in qualified if c["symbol"] in recent_symbols][:TOP_N_FOR_ALLOCATION]
    print(f"[{tier['name']}] {len(eligible)} eligible after excluding recently bought names.")

    picks = eligible[:TOP_N_FOR_ALLOCATION]
    positions, leftover = allocate_budget(picks, tier["budget"])

    funnel = {
        "total": len(candidates),
        "trend": len(trending),
        "bulk": len(bulk_filtered),
        "qualified": len(qualified),
        "eligible": len(eligible),
    }

    return positions, leftover, runners_up, funnel


def build_tier_section(tier, positions, leftover, runners_up, funnel):
    lines = [
        f"=== {tier['name']} (${tier['min_cap']:,} - ${tier['max_cap']:,}, ${tier['budget']} budget) ===",
        "Funnel: "
        f"{funnel['total']} in cap range -> {funnel['trend']} in uptrend -> "
        f"{funnel['bulk']} passed liquidity/extension/earnings filters -> "
        f"{funnel['qualified']} passed EPS growth + valuation checks -> "
        f"{funnel['eligible']} eligible after excluding recent buys",
        "",
    ]

    if positions:
        lines.append(f"Recommended buys (${tier['budget'] - leftover:,.2f} of ${tier['budget']}):")
        for pos in positions:
            lines.append(
                f"  {pos['symbol']} ({pos['name']}): {pos['shares']} share(s) @ ${pos['price']:.2f} "
                f"= ${pos['cost']:.2f} | market cap ${pos['market_cap']:,.0f} | "
                f"PEG {pos['peg']:.2f} | {EPS_GROWTH_YEARS}y EPS CAGR {pos['eps_cagr'] * 100:.1f}%"
            )
        if leftover > 0.01:
            lines.append(f"  Leftover cash (no affordable whole shares left): ${leftover:.2f}")
    else:
        lines.append(f"No positions this week -- ${tier['budget']} stays in cash.")

    if runners_up:
        lines.append("")
        lines.append("Also qualified but skipped (recently bought, within cooldown):")
        for r in runners_up:
            lines.append(f"  {r['symbol']} ({r['name']}): PEG {r['peg']:.2f}, market cap ${r['market_cap']:,.0f}")

    return "\n".join(lines)


def build_holdings_summary():
    history = load_history()
    lines = ["=== Current Holdings (net of confirmed buys and sells) ==="]
    if not history:
        lines.append("  No confirmed holdings yet.")
        return "\n".join(lines)

    by_symbol = {k: v for k, v in net_holdings(history).items() if v["shares"] > 1e-6}
    if not by_symbol:
        lines.append("  No open positions (everything bought has since been sold).")
        return "\n".join(lines)

    for symbol in sorted(by_symbol):
        shares = by_symbol[symbol]["shares"]
        cost = by_symbol[symbol]["cost"]
        avg_price = cost / shares if shares else 0
        lines.append(f"  {symbol}: {shares:g} share(s), avg cost ${avg_price:.2f}, total ${cost:,.2f}")

    total_cost = sum(agg["cost"] for agg in by_symbol.values())
    lines.append(f"  Total invested (net of sells): ${total_cost:,.2f}")
    return "\n".join(lines)


def main():
    require_env("SENDER_EMAIL", SENDER_EMAIL)
    require_env("RECEIVER_EMAIL", RECEIVER_EMAIL)
    require_env("GMAIL_APP_KEY", EMAIL_PASSWORD)

    recent_symbols = recently_bought_symbols()

    sections = []
    any_positions = False
    total_invested = 0.0
    total_budget = sum(tier["budget"] for tier in CAP_TIERS)

    for tier in CAP_TIERS:
        positions, leftover, runners_up, funnel = screen_tier(tier, recent_symbols)
        record_recommendations(tier["name"], positions)
        if positions:
            any_positions = True
            total_invested += tier["budget"] - leftover
        sections.append(build_tier_section(tier, positions, leftover, runners_up, funnel))

    body = (
        f"Total: ${total_invested:,.2f} of ${total_budget} recommended this week.\n"
        "This is a recommendation, not a recorded purchase. Forward the brokerage "
        "confirmation email once you buy, and it'll be picked up automatically.\n\n"
        + "\n\n".join(sections)
        + "\n\n" + build_holdings_summary()
        + "\n\n" + build_benchmark_report()
        + "\n\n" + build_sell_report()
    )

    print("\nSending weekly summary email...")
    msg = EmailMessage()
    msg.set_content(body)
    msg['Subject'] = 'Weekly Conviction Buy List' if any_positions else 'Weekly Screener: No Buys This Week'
    msg['From'] = SENDER_EMAIL
    msg['To'] = RECEIVER_EMAIL

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(SENDER_EMAIL, EMAIL_PASSWORD)
        server.send_message(msg)
    print("Email sent successfully!")


if __name__ == "__main__":
    main()
