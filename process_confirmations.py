import email
import imaplib
import os
import re
from datetime import datetime

from portfolio_store import detect_asset_type, load_history, save_history

SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')
EMAIL_PASSWORD = os.environ.get('GMAIL_APP_KEY')

IMAP_HOST = "imap.gmail.com"


def require_env(name, value):
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")


def extract_sender_address(msg):
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", msg.get("From", ""))
    return match.group(0).lower() if match else ""


def get_body_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition") or "")
            if part.get_content_type() == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        for part in msg.walk():  # fall back to stripped HTML if no plain-text part exists
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                html = part.get_payload(decode=True).decode(charset, errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    charset = msg.get_content_charset() or "utf-8"
    payload = msg.get_payload(decode=True)
    return payload.decode(charset, errors="replace") if payload else ""


def extract_candidate_bodies(msg):
    """Return every body text worth checking: the message's own body, plus the body
    of any full email forwarded as a message/rfc822 attachment (Gmail's 'Forward as
    attachment' option, as opposed to the default inline-quoted forward)."""
    bodies = [get_body_text(msg)]
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "message/rfc822":
                payload = part.get_payload()
                sub_messages = payload if isinstance(payload, list) else [payload]
                for sub_msg in sub_messages:
                    bodies.append(get_body_text(sub_msg))
    return bodies


def parse_confirmation(body):
    # some "plain text" parts still contain literal HTML markup (e.g. <b> tags
    # around each value), so strip tags regardless of which MIME part this came
    # from, then collapse whitespace/newlines left behind by that stripping.
    # Gmail's plain-text forward also renders bold as *...* around each value.
    body = re.sub(r"<[^>]+>", " ", body)
    body = body.replace("*", " ")
    body = re.sub(r"\s+", " ", body)

    if "brokerage order execution" not in body:
        return None

    date_m = re.search(r"executed on (\d{2}/\d{2}/\d{4})", body)
    type_m = re.search(r"Transaction type:\s*(\w+)", body)
    symbol_m = re.search(r"Security:.*?\(([A-Z.]{1,6})\)", body)
    qty_m = re.search(r"Quantity:\s*([\d,.]+)\s*share", body)
    price_m = re.search(r"Price:\*?\s*\$([\d,]+\.?\d*)", body)

    if not all([date_m, type_m, symbol_m, qty_m, price_m]):
        return None

    return {
        "date": datetime.strptime(date_m.group(1), "%m/%d/%Y").date().isoformat(),
        "transaction_type": type_m.group(1),
        "symbol": symbol_m.group(1),
        "shares": float(qty_m.group(1).replace(",", "")),
        "price": float(price_m.group(1).replace(",", "")),
    }


def main():
    require_env("SENDER_EMAIL", SENDER_EMAIL)
    require_env("RECEIVER_EMAIL", RECEIVER_EMAIL)
    require_env("GMAIL_APP_KEY", EMAIL_PASSWORD)

    full_rescan = os.environ.get("FULL_RESCAN", "").lower() in ("true", "1", "yes")

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    imap.login(SENDER_EMAIL, EMAIL_PASSWORD)
    imap.select("INBOX")

    search_criteria = "ALL" if full_rescan else "UNSEEN"
    if full_rescan:
        print("FULL_RESCAN enabled: checking every message, not just unread ones.")
    status, data = imap.search(None, search_criteria)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")
    message_ids = data[0].split()
    print(f"Found {len(message_ids)} unread message(s) to check.")

    history = load_history()

    def dup_key(entry):
        return (entry["date"], entry["symbol"], entry.get("transaction_type", "buy"),
                float(entry["shares"]), float(entry["price"]))

    recorded_keys = {dup_key(e) for e in history}
    new_entries = []

    for msg_id in message_ids:
        status, msg_data = imap.fetch(msg_id, "(BODY.PEEK[])")  # PEEK avoids marking as read on fetch
        if status != "OK" or not msg_data or msg_data[0] is None:
            continue
        msg = email.message_from_bytes(msg_data[0][1])

        sender = extract_sender_address(msg)
        if sender != RECEIVER_EMAIL.lower():
            print(f"  -> Ignoring message from untrusted sender: {sender or '(unknown)'}")
            continue  # leave unread; don't trust or discard mail from anyone but the account owner

        # a single message can contain several confirmations, e.g. Gmail's multi-select
        # "Forward as attachment" bundling several original emails into one outgoing message
        candidate_bodies = [b for b in extract_candidate_bodies(msg) if "brokerage order execution" in b]
        if not candidate_bodies:
            imap.store(msg_id, '+FLAGS', '\\Seen')  # not a confirmation at all; stop rechecking it
            continue

        print(f"  -> Found {len(candidate_bodies)} confirmation(s) in msg {msg_id.decode()}.")
        any_unparsed = False

        for body in candidate_bodies:
            parsed = parse_confirmation(body)
            if parsed is None:
                normalized = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
                print("     Couldn't parse one confirmation in this message; leaving the whole message unread.")
                print(f"     Normalized text for debugging: {normalized[:500]!r}")
                any_unparsed = True
                continue

            transaction_type = parsed["transaction_type"].strip().lower()
            if transaction_type not in ("buy", "sell"):
                print(f"     Skipping unsupported transaction type '{parsed['transaction_type']}' "
                      f"for {parsed['symbol']}.")
                continue

            entry = {
                "date": parsed["date"],
                "symbol": parsed["symbol"],
                "transaction_type": transaction_type,
                "shares": parsed["shares"],
                "price": parsed["price"],
                "cost": round(parsed["shares"] * parsed["price"], 2),
                "source": "brokerage-confirmation",
                "asset_type": detect_asset_type(parsed["symbol"]),
            }
            key = dup_key(entry)
            if key in recorded_keys:
                print(f"     Skipping duplicate (already recorded): {transaction_type} {entry['symbol']} "
                      f"x{entry['shares']} @ ${entry['price']:.2f} on {entry['date']}.")
                continue

            recorded_keys.add(key)
            new_entries.append(entry)
            print(f"     Recorded confirmed {transaction_type}: {entry['symbol']} x{entry['shares']} "
                  f"@ ${entry['price']:.2f}")

        if any_unparsed:
            continue  # leave the whole message unread; the dup-key check makes a retry safe
        imap.store(msg_id, '+FLAGS', '\\Seen')

    imap.logout()

    if new_entries:
        save_history(history + new_entries)
        print(f"Added {len(new_entries)} confirmed purchase(s) to portfolio history.")
    else:
        print("No new confirmed purchases found.")


if __name__ == "__main__":
    main()
