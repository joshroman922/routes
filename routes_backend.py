"""
ROUTES backend — GitHub Actions version
========================================
Same job as the Apps Script version, but runs on GitHub Actions (like the SEC scanner),
so it deploys entirely from the phone with no Google Apps Script editor.

WHAT IT DOES (unchanged):
  - Watches your 10 stocks' prices
  - Reads the market regime from the VIX (calibrated bands)
  - Flags unusual volume (vs 30-day average)
  - Self-audits its own flags: each flag resolves EXACTLY once, from a pinned anchor,
    graded as "real move" only within a 5-day window, "noise" only after it expires
  - Writes state.json back into this repo, which the dashboard (index.html) reads

HONEST CEILING: it watches, tags, and scores. It does NOT predict price or decide trades.

DEPLOY: this runs inside GitHub Actions. The workflow commits state.json back to the repo
using the built-in GITHUB_TOKEN — so there is NO personal token to create or paste.
Data source is free Yahoo (no key). Optional: set a TD_KEY repo secret later for a
sturdier feed; without it, Yahoo is used.
"""

import os
import json
import time
from datetime import datetime, timezone
import urllib.request

TICKERS = ["MSFT", "V", "MA", "JPM", "NVDA", "GS", "GAP", "ESTC", "HOOD", "COIN"]
STATE_FILE = "state.json"

VOL_SPIKE = 1.8          # flag when today's volume > 1.8x its ~30-day average
REAL_MOVE_PCT = 3.0      # a flag resolves as a real move at >=3% from its anchor
WINDOW_DAYS = 5          # evaluation window before a quiet flag is graded "noise"
FLAG_CAP = 200

UA = {"User-Agent": "RoutesBackend joshroman922"}


def http_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_series_yahoo(symbol):
    """Return dict(price, prev, avg_vol, last_vol) or None."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=1d&range=1mo")
        j = http_json(url)
        res = j["chart"]["result"][0]
        meta = res["meta"]
        vols = [v for v in (res["indicators"]["quote"][0].get("volume") or []) if v]
        avg = sum(vols) / len(vols) if vols else None
        last = vols[-1] if vols else None
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        return {"price": price, "prev": prev, "avg_vol": avg, "last_vol": last}
    except Exception as e:
        print(f"  [!] {symbol} fetch error: {e}")
        return None


def fetch_price_yahoo(symbol):
    s = fetch_series_yahoo(symbol)
    return s["price"] if s else None


def read_regime():
    """Calibrated VIX bands. Keep last-good on failure (handled by caller)."""
    vix = fetch_price_yahoo("%5EVIX")
    if vix is None:
        return None
    if vix >= 30:
        return {"label": f"STORMY · VIX {vix:.1f}",
                "note": "High fear, big swings both ways. Smaller size / wider stops are common here — decide deliberately."}
    if vix >= 22:
        return {"label": f"ELEVATED · VIX {vix:.1f}",
                "note": "Choppier than usual — stops get hit more often. Your call whether to give room or size down."}
    if vix >= 15:
        return {"label": f"NORMAL · VIX {vix:.1f}",
                "note": "Ordinary conditions. Standard risk/reward."}
    return {"label": f"CALM · VIX {vix:.1f}",
            "note": "Low volatility. Stops behave; normal sizing."}


def main():
    state = load_state()
    state.setdefault("audit", {"flagsLogged": 0, "resolved": 0, "realMove": 0,
                               "moveUp": 0, "moveDown": 0, "noise": 0})
    state.setdefault("flags", [])
    state.setdefault("regime", {"label": "UNKNOWN", "note": "Waiting on data…"})

    # regime (keep last-good if fetch fails)
    reg = read_regime()
    if reg:
        state["regime"] = reg

    price_now = {}
    watch = []
    for t in TICKERS:
        q = fetch_series_yahoo(t)
        time.sleep(0.3)  # be polite to the feed
        if not q or q["price"] is None:
            continue
        price_now[t] = q["price"]
        spiking = bool(q["avg_vol"] and q["last_vol"] and q["last_vol"] > q["avg_vol"] * VOL_SPIKE)
        chg = ((q["price"] - q["prev"]) / q["prev"] * 100) if q["prev"] else None

        has_open = any(f["ticker"] == t and not f["resolved"] for f in state["flags"])
        if spiking and not has_open:
            state["flags"].append({
                "ticker": t, "anchor": q["price"],
                "date": datetime.now(timezone.utc).isoformat(), "resolved": False
            })
            state["audit"]["flagsLogged"] += 1

        watch.append({
            "ticker": t,
            "price": f"{q['price']:.2f}",
            "change": ("—" if chg is None else f"{'+' if chg >= 0 else ''}{chg:.2f}%"),
            "volume": "Volume expansion" if spiking else "Normal",
        })

    # resolve open flags — once each, from pinned anchor
    now = datetime.now(timezone.utc)
    for f in state["flags"]:
        if f["resolved"]:
            continue
        p = price_now.get(f["ticker"])
        if p is None:
            continue
        signed = (p - f["anchor"]) / f["anchor"] * 100
        try:
            age_days = (now - datetime.fromisoformat(f["date"])).days
        except Exception:
            age_days = 0
        if abs(signed) >= REAL_MOVE_PCT:
            f["resolved"] = True
            state["audit"]["resolved"] += 1
            state["audit"]["realMove"] += 1
            if signed > 0:
                state["audit"]["moveUp"] += 1
            else:
                state["audit"]["moveDown"] += 1
        elif age_days >= WINDOW_DAYS:
            f["resolved"] = True
            state["audit"]["resolved"] += 1
            state["audit"]["noise"] += 1

    # bound the flag list
    open_f = [f for f in state["flags"] if not f["resolved"]]
    done_f = [f for f in state["flags"] if f["resolved"]]
    state["flags"] = open_f + done_f[-FLAG_CAP:]

    # honest verdict
    R = state["audit"]["resolved"]
    M = state["audit"]["realMove"]
    if R < 10:
        state["audit"]["verdict"] = (f"Not enough resolved flags yet ({R}). "
                                     "Scorecard builds as flags fire and their 5-day windows close.")
    else:
        pct = round(M / R * 100)
        state["audit"]["verdict"] = (
            f"Volume flags preceded a real (≥3%) move {pct}% of the time — "
            f"{state['audit']['moveUp']} up, {state['audit']['moveDown']} down. "
            "Note: this only says something moved, NOT which way. A volume flag is not a buy signal."
        )

    state["watchlist"] = watch
    state["feed"] = "Yahoo (free)"
    state["status"] = "LIVE SYNC"
    state["lastRun"] = now.isoformat()

    save_state(state)
    print(f"[+] Wrote {STATE_FILE} — {len(watch)} tickers, "
          f"{len(open_f)} open flags, {R} resolved.")


if __name__ == "__main__":
    main()
