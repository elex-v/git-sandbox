import argparse
from datetime import date

from portfolio_store import detect_asset_type, load_history, save_history


def main():
    parser = argparse.ArgumentParser(description="Manually add a buy or sell to the portfolio history.")
    parser.add_argument("symbol", help="Ticker symbol, e.g. AAPL or VOO")
    parser.add_argument("shares", type=float, help="Number of shares")
    parser.add_argument("price", type=float, help="Price per share")
    parser.add_argument("--date", default=date.today().isoformat(),
                         help="Transaction date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--sell", action="store_true", help="Record this as a sell instead of a buy")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    asset_type = detect_asset_type(symbol)
    transaction_type = "sell" if args.sell else "buy"

    entry = {
        "date": args.date,
        "symbol": symbol,
        "transaction_type": transaction_type,
        "shares": args.shares,
        "price": args.price,
        "cost": round(args.shares * args.price, 2),
        "source": "manual",
        "asset_type": asset_type,
    }

    history = load_history()
    history.append(entry)
    save_history(history)

    print(f"Added: {transaction_type} {symbol} x{args.shares} @ ${args.price:.2f} ({asset_type}) on {args.date}")
    print("Now commit and push data/portfolio_history.json so the screener/sell-checker picks it up:")
    print(f"  git add data/portfolio_history.json && git commit -m 'Add manual {transaction_type}: {symbol}' && git push")


if __name__ == "__main__":
    main()
