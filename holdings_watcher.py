import os
import json
from datetime import datetime, timezone
import urllib.request

DISCORD = os.getenv("DISCORD_WEBHOOK")
HOLDINGS_FILE = "holdings.json"
STATE_FILE = "holdings_alert_state.json"
BIG_MOVE_PCT = 6.0
UA = {"User-Agent": "HoldingsWatcher joshroman922"}


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def send_discord(embed):
    if not DISCORD:
        print("[!] No DISCORD_WEBHOOK set"); print(embed); return
    data = json.dumps({"embeds": [embed]}).encode()
    req = urllib.request.Request(DISCORD, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10); print("[+] Discord sent")
    except Exception as e:
        print(f"[!] Discord error: {e}")


def price_of(symbol):
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=1d&range=1d")
        j = http_json(url)
        return j["chart"]["result"][0]["meta"].get("regularMarketPrice")
    except Exception as e:
        print(f"  [!] {symbol}: {e}")
        return None


def alert(ticker, kind, price, buy, note):
    colors = {"TARGET": 5763719, "STOP": 15158332, "MOVE_UP": 5763719, "MOVE_DOWN": 15158332}
    titles = {
        "TARGET": f"\U0001F3AF {ticker} HIT YOUR TARGET",
        "STOP":   f"\U0001F6D1 {ticker} HIT YOUR STOP",
        "MOVE_UP":   f"\U0001F4C8 {ticker} is UP a lot",
        "MOVE_DOWN": f"\U0001F4C9 {ticker} is DOWN a lot",
    }
    pnl = ((price - buy) / buy * 100) if buy else None
    fields = [
        {"name": "Now", "value": f"${price:.2f}", "inline": True},
        {"name": "You paid", "value": f"${buy:.2f}", "inline": True},
    ]
    if pnl is not None:
        fields.append({"name": "You're", "value": f"{'+' if pnl>=0 else ''}{pnl:.1f}%", "inline": True})
    fields.append({"name": "What this means", "value": note, "inline": False})
    send_discord({
        "title": titles[kind],
        "color": colors[kind],
        "fields": fields,
        "footer": {"text": "Your rule fired - YOU decide whether to sell. Not financial advice."},
    })
    print(f"[!] {ticker} {kind} @ {price}")


def main():
    holdings = load_json(HOLDINGS_FILE, {})
    if not holdings:
        print("[i] holdings.json is empty - add what you own to start watching.")
        return
    state = load_json(STATE_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for ticker, info in holdings.items():
        if ticker.startswith("_"):
            continue
        price = price_of(ticker)
        if price is None:
            continue
        buy = info.get("buy")
        target = info.get("target")
        stop = info.get("stop")
        st = state.setdefault(ticker, {})

        if target and price >= target and not st.get("target"):
            alert(ticker, "TARGET", price, buy,
                  "It reached the price you set to take profit. Consider selling to lock the gain - your call.")
            st["target"] = True

        if stop and price <= stop and not st.get("stop"):
            alert(ticker, "STOP", price, buy,
                  "It dropped to your cut-loss level. This is the alert that protects you from a bigger loss - decide fast.")
            st["stop"] = True

        if buy:
            move = (price - buy) / buy * 100
            day_key = f"move:{today}"
            if abs(move) >= BIG_MOVE_PCT and st.get(day_key) != round(move):
                if move > 0:
                    alert(ticker, "MOVE_UP", price, buy,
                          f"It's up {move:.1f}% from your buy. Big up moves can keep going OR snap back - go look and decide.")
                else:
                    alert(ticker, "MOVE_DOWN", price, buy,
                          f"It's down {abs(move):.1f}% from your buy. Check why - news? Decide whether your reason to hold still stands.")
                st[day_key] = round(move)

    save_json(STATE_FILE, state)
    print(f"[+] Checked {len(holdings)} holdings.")


if __name__ == "__main__":
    main()
