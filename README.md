# Freqtrade Replay

Run your Freqtrade strategy against historical data at full speed — in seconds instead of weeks.

It works by tricking Freqtrade into thinking it is connected to a live exchange and that time is passing normally, while actually replaying stored candle data as fast as your CPU allows. The result is a standard Freqtrade SQLite database that you can open directly in FreqUI and analyze just like a real dry-run.

---

## Requirements

- Docker and Docker Compose
- Freqtrade `user_data/` folder with your strategy and `config.json`

---

## Setup

**1. Clone this repo into your `user_data` folder:**

```bash
cd /path/to/your/freqtrade/user_data
git clone https://github.com/saamy4r/Freqtrade_reply.git freqtrade_replay
```

**2. Add the replay service to your `docker-compose.yml`:**

```yaml
  replay:
    image: ghcr.io/saamy4r/freqtrade-replay:latest
    volumes:
      - "./user_data:/freqtrade/user_data"
    entrypoint:
      - python
      - /freqtrade/user_data/freqtrade_replay/cli.py
    profiles:
      - replay
```

**3. Make sure your `config.json` has an `api_server` section** so FreqUI can display results afterward:

```json
"initial_state": "running",
"db_url": "sqlite:////freqtrade/user_data/tradesv3.sqlite",
"api_server": {
    "enabled": true,
    "listen_ip_address": "0.0.0.0",
    "listen_port": 8080,
    "verbosity": "error",
    "enable_openapi": false,
    "jwt_secret_key": "supersecretjwt",
    "username": "freqtrade",
    "password": "yourpassword"
}
```

---

## Download Historical Data

Before running a replay you need candle data downloaded locally. **Always include `1m`** — the replay uses it for intra-candle stop-loss and take-profit accuracy (see [How It Works](#how-it-works)):

```bash
docker compose run --rm freqtrade download-data \
  --config user_data/config.json \
  --timeframes 1m 5m 15m 1h 4h \
  --timerange 20250101-20250601 \
  --trading-mode futures
```

> Pairs are read from `pair_whitelist` in your `config.json`.

---

## Run a Replay

```bash
docker compose --profile replay run --rm replay \
  --strategy MyStrategy \
  --timerange 20250301-20260501
```

That's it. When it finishes you will see a summary in the terminal:

```
  Closed trades: 42
  Win rate     : 28/42 (66.7%)
  Total P&L    : +183.24 USDT
```

### Optional flags

| Flag | Default | Description |
|---|---|---|
| `--strategy` | `` | Strategy class name |
| `--timerange` | required | Date range `YYYYMMDD-YYYYMMDD` |
| `--pairs` | from config whitelist | Override which pairs to trade |
| `--config` | `user_data/config.json` | Path to your config file |
| `--slippage` | `0.0005` | Simulated bid-ask spread (0.05%) |
| `--datadir` | `user_data/data/binance/futures` | Where your feather data files are |
| `--no-fresh` | off | Keep the existing DB instead of starting clean |
| `--report` | off | Write a standalone HTML report to a file |

---

## View Results in FreqUI

After the replay finishes, start the Freqtrade viewer:

```bash
docker compose up -d 
simply start a dry test of your strategy, then in the dashboard you will see the results.
```

Then open **http://localhost:8080** in your browser. Log in with the username and password from your `config.json`. You will see all trades on the chart exactly like a real dry-run.

---

## How It Works

Freqtrade's bot logic never talks to the exchange directly — it goes through a clean interface. Freqtrade Replay replaces that interface with:

- **Fake exchange** — serves candle data from local files instead of Binance
- **Virtual clock** — jumps forward in time instantly instead of waiting for real time
- **Real bot** — everything else (signals, DCA, stoploss, fees, order matching) runs exactly as in production

The output is a real Freqtrade database, fully compatible with FreqUI and all Freqtrade analysis tools.

### Why 1m data matters

Most backtesting tools check stop-losses and take-profits only at the candle close. That misses a lot of what actually happens inside a candle.

Consider a 1h candle: open=30, low=28, high=36, close=32. A live bot polling every few seconds would have seen the price drop to 28 and trigger a stop at 28.5 — or seen the price hit 35 and close a take-profit — long before the candle closed. A backtester that only looks at the close would miss both.

Freqtrade Replay solves this by stepping through time at **1-minute resolution** using real 1m candle data. This means:

- Stop-losses trigger when the 1m candle's low crosses the stop price — not just at the 1h close
- Take-profits and limit exits fire when the 1m high reaches the target
- The bot sees intra-candle price movements just like a live bot would

Your strategy's indicator logic still runs on its configured timeframe (15m, 1h, 4h, etc.) — 1m data is only used for order fill checking. This keeps signals identical to live while making fills accurate.

This is also why the results differ from freqtrade's built-in backtester: if your strategy uses a repainting indicator or has look-ahead bias, it will show up here just as it would in live trading — because the bot is running candle by candle, seeing only what was visible at each point in time.

---

## Tips

- If a pair is missing data the tool will **auto-download** it before starting. The auto-download always includes 1m data.
- If your strategy uses an **informative pair** (e.g. BTC as a filter) you do not need to add it to `--pairs` — it is detected and loaded automatically.
- If you have old downloaded data without 1m files, re-run the download command with `--timeframes 1m 5m 15m 1h 4h` to add them. The replay will still work without 1m data but stop/TP fills will be less accurate.
