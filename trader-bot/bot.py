#!/usr/bin/env python3
"""Paper-trading crypto bot.

Pulls real market candles from Gemini's public API (no account or API key
needed), runs a moving-average crossover strategy, and simulates trades
against a fake cash balance. It never places real orders: there is no live
trading code in this file, and it refuses to start unless config mode is
"paper".

Usage:
    python3 bot.py run                 # loop forever, trading on each new candle
    python3 bot.py once                # check the market once and exit
    python3 bot.py status              # show the paper account
    python3 bot.py backtest [--file F] # replay history and report results
    python3 bot.py reset               # wipe the paper account back to starting cash
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
TRADES_PATH = os.path.join(HERE, "trades.csv")

API = "https://api.gemini.com/v2/candles/{symbol}/{tf}"
TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                     "1hr": 3600, "6hr": 21600, "1day": 86400}


# --- config & state -------------------------------------------------------

def load_config():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    if cfg.get("mode") != "paper":
        sys.exit("Refusing to start: config mode is %r. This bot only paper "
                 "trades. Live trading has not been built." % cfg.get("mode"))
    if cfg["timeframe"] not in TIMEFRAME_SECONDS:
        sys.exit("timeframe must be one of: " + ", ".join(TIMEFRAME_SECONDS))
    if cfg["fast_sma"] >= cfg["slow_sma"]:
        sys.exit("fast_sma must be smaller than slow_sma")
    return cfg


def new_account(cfg):
    return {"cash": cfg["starting_cash"], "qty": 0.0, "entry_price": None,
            "last_candle_ts": None, "started": now_iso()}


def load_state(cfg):
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return new_account(cfg)


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def ts_iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --- market data ----------------------------------------------------------

def fetch_candles(symbol, tf):
    """Return closed candles, oldest first, as [ts_ms, open, high, low, close, vol]."""
    url = API.format(symbol=symbol.lower(), tf=tf)
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trader-bot"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        candles = json.load(resp)
    candles.sort(key=lambda c: c[0])
    # The newest candle is still forming; only trade on finished ones.
    cutoff_ms = (time.time() - TIMEFRAME_SECONDS[tf]) * 1000
    return [c for c in candles if c[0] <= cutoff_ms]


def load_candles_file(path):
    with open(path) as f:
        candles = json.load(f)
    candles.sort(key=lambda c: c[0])
    return candles


# --- strategy -------------------------------------------------------------

def sma(values, n):
    return sum(values[-n:]) / n


def signal(closes, cfg):
    """'buy' when the fast average crosses above the slow one, 'sell' on the reverse."""
    fast, slow = cfg["fast_sma"], cfg["slow_sma"]
    if len(closes) < slow + 1:
        return None
    prev_fast, prev_slow = sma(closes[:-1], fast), sma(closes[:-1], slow)
    cur_fast, cur_slow = sma(closes, fast), sma(closes, slow)
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        return "buy"
    if prev_fast >= prev_slow and cur_fast < cur_slow:
        return "sell"
    return None


def risk_exit(state, price, cfg):
    """Stop-loss / take-profit check on an open position."""
    if state["qty"] <= 0 or not state["entry_price"]:
        return None
    change = price / state["entry_price"] - 1
    if change <= -cfg["stop_loss_pct"] / 100:
        return "stop-loss"
    if change >= cfg["take_profit_pct"] / 100:
        return "take-profit"
    return None


# --- paper broker ---------------------------------------------------------

def paper_buy(state, price, cfg):
    fill = price * (1 + cfg["slippage_pct"] / 100)
    spend = state["cash"] * cfg["position_pct"] / 100
    if spend < 1:
        return None
    fee = spend * cfg["fee_pct"] / 100
    qty = (spend - fee) / fill
    state["cash"] -= spend
    state["qty"] += qty
    state["entry_price"] = fill
    return {"side": "BUY", "price": fill, "qty": qty, "fee": fee, "value": spend}


def paper_sell(state, price, cfg):
    if state["qty"] <= 0:
        return None
    fill = price * (1 - cfg["slippage_pct"] / 100)
    gross = state["qty"] * fill
    fee = gross * cfg["fee_pct"] / 100
    entry = state["entry_price"]
    trade = {"side": "SELL", "price": fill, "qty": state["qty"], "fee": fee,
             "value": gross - fee,
             "pnl_pct": (fill / entry - 1) * 100 if entry else 0.0}
    state["cash"] += gross - fee
    state["qty"] = 0.0
    state["entry_price"] = None
    return trade


def equity(state, price):
    return state["cash"] + state["qty"] * price


def step(state, candles, cfg, log_trade):
    """Process the newest closed candle. Returns the trade made, if any."""
    last = candles[-1]
    price = last[4]
    closes = [c[4] for c in candles]
    reason = risk_exit(state, price, cfg)
    sig = "sell" if reason else signal(closes, cfg)
    reason = reason or (sig and "sma-cross")
    trade = None
    if sig == "buy" and state["qty"] == 0:
        trade = paper_buy(state, price, cfg)
    elif sig == "sell" and state["qty"] > 0:
        trade = paper_sell(state, price, cfg)
    if trade:
        trade.update(time=ts_iso(last[0]), reason=reason,
                     equity=equity(state, price))
        log_trade(trade)
    state["last_candle_ts"] = last[0]
    return trade


def write_trade_csv(trade):
    fields = ["time", "side", "reason", "price", "qty", "fee", "value",
              "pnl_pct", "equity"]
    new = not os.path.exists(TRADES_PATH)
    with open(TRADES_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(trade)


def describe(trade, symbol):
    s = "[PAPER] %s %s %.6f %s @ %.2f (%s)" % (
        trade["time"], trade["side"], trade["qty"], symbol, trade["price"],
        trade["reason"])
    if "pnl_pct" in trade:
        s += "  P/L %+.2f%%" % trade["pnl_pct"]
    return s + "  equity $%.2f" % trade["equity"]


# --- commands -------------------------------------------------------------

def check_market(cfg, state):
    candles = fetch_candles(cfg["symbol"], cfg["timeframe"])
    if not candles:
        print("No closed candles returned.")
        return
    if candles[-1][0] == state["last_candle_ts"]:
        return  # nothing new since last check
    trade = step(state, candles, cfg,
                 lambda t: (write_trade_csv(t), print(describe(t, cfg["symbol"]))))
    save_state(state)
    if not trade:
        price = candles[-1][4]
        print("%s  %s %.2f  no trade  equity $%.2f" % (
            now_iso(), cfg["symbol"], price, equity(state, price)))


def cmd_run(cfg):
    state = load_state(cfg)
    print("Paper trading %s on %s candles. Ctrl+C to stop." % (
        cfg["symbol"], cfg["timeframe"]))
    while True:
        try:
            check_market(cfg, state)
        except KeyboardInterrupt:
            raise
        except Exception as e:  # network blips shouldn't kill the bot
            print("%s  error: %s" % (now_iso(), e))
        time.sleep(cfg["poll_seconds"])


def cmd_once(cfg):
    check_market(cfg, load_state(cfg))


def cmd_status(cfg):
    state = load_state(cfg)
    try:
        price = fetch_candles(cfg["symbol"], cfg["timeframe"])[-1][4]
    except Exception as e:
        print("Could not fetch price (%s); showing stored values." % e)
        price = state["entry_price"] or 0
    eq = equity(state, price)
    start = cfg["starting_cash"]
    print("Mode:         PAPER (no real money)")
    print("Since:        %s" % state["started"])
    print("Cash:         $%.2f" % state["cash"])
    print("Holding:      %.6f %s" % (state["qty"], cfg["symbol"]))
    if state["entry_price"]:
        print("Entry price:  %.2f   (now %.2f)" % (state["entry_price"], price))
    print("Equity:       $%.2f   (%+.2f%% vs $%.2f start)" % (
        eq, (eq / start - 1) * 100, start))


def cmd_backtest(cfg, path):
    candles = load_candles_file(path) if path else fetch_candles(cfg["symbol"], cfg["timeframe"])
    state = new_account(cfg)
    trades = []
    for i in range(cfg["slow_sma"] + 1, len(candles) + 1):
        step(state, candles[:i], cfg, trades.append)
    for t in trades:
        print(describe(t, cfg["symbol"]))
    final = equity(state, candles[-1][4])
    start = cfg["starting_cash"]
    sells = [t for t in trades if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl_pct"] > 0]
    hold = (candles[-1][4] / candles[cfg["slow_sma"]][4] - 1) * 100
    print("\n%d candles  %s -> %s" % (len(candles), ts_iso(candles[0][0]), ts_iso(candles[-1][0])))
    print("Round trips:   %d  (wins %d)" % (len(sells), len(wins)))
    print("Final equity:  $%.2f  (%+.2f%%)" % (final, (final / start - 1) * 100))
    print("Buy & hold:    %+.2f%%" % hold)


def cmd_reset(cfg):
    for p in (STATE_PATH, TRADES_PATH):
        if os.path.exists(p):
            os.remove(p)
    save_state(new_account(cfg))
    print("Paper account reset to $%.2f." % cfg["starting_cash"])


def main():
    p = argparse.ArgumentParser(description="Paper-trading crypto bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "once", "status", "reset"):
        sub.add_parser(name)
    bt = sub.add_parser("backtest")
    bt.add_argument("--file", help="candles JSON file (default: fetch live history)")
    args = p.parse_args()
    cfg = load_config()
    try:
        if args.cmd == "backtest":
            cmd_backtest(cfg, args.file)
        else:
            {"run": cmd_run, "once": cmd_once, "status": cmd_status,
             "reset": cmd_reset}[args.cmd](cfg)
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        sys.exit("Could not reach Gemini market data: %s" % e)


if __name__ == "__main__":
    main()
