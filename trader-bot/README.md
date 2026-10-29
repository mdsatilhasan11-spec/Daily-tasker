# Paper Trader Bot

A crypto trading bot that **only paper trades**: it uses a fake $10,000 balance and real live prices. It never touches real money. There is no live-trading code in it, and it refuses to start if `config.json` says anything other than `"mode": "paper"`. Real trading gets added only when you explicitly decide to go live.

## How it works

1. Every `poll_seconds` it pulls candles from Gemini's public market-data API. This needs no account and no API key.
2. It acts only on **closed** candles, so it never reacts to a half-formed bar.
3. Strategy: moving-average crossover.
   - **Buy** when the 20-period average crosses above the 50-period average.
   - **Sell** when it crosses back below, or when the stop-loss (-3%) or take-profit (+6%) is hit.
4. Each simulated fill includes fees and slippage, so the results aren't flattering.
5. The account is saved to `state.json` and every trade is logged to `trades.csv`. You can stop and restart the bot without losing anything.

## Run it (Python 3.8+, no installs needed)

```bash
cd trader-bot
python3 bot.py run          # start paper trading (Ctrl+C to stop)
python3 bot.py status       # cash, holdings, profit/loss
python3 bot.py once         # single check, handy for cron
python3 bot.py backtest     # replay recent history
python3 bot.py backtest --file sample_data/btcusd_1hr.json
python3 bot.py reset        # back to starting cash
```

## Settings (`config.json`)

| key | meaning |
|---|---|
| `symbol` | Gemini pair, e.g. `BTCUSD`, `ETHUSD`, `SOLUSD` |
| `timeframe` | `1m 5m 15m 30m 1hr 6hr 1day` |
| `starting_cash` | paper dollars |
| `position_pct` | % of cash used per buy |
| `fast_sma` / `slow_sma` | moving-average lengths |
| `stop_loss_pct` / `take_profit_pct` | exit thresholds |
| `fee_pct` / `slippage_pct` | simulated trading costs |
| `poll_seconds` | how often to check |

## Honest note

On the included sample data (Aug 29 – Oct 29, 2026, BTC hourly), the default settings **lost 8.2%** while just holding BTC gained 2.6%. A simple crossover strategy gets chopped up in sideways markets. That's exactly why it should stay on paper until it has a track record you trust.
