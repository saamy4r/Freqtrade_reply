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

Before running a replay you need candle data downloaded locally. Run this once (or whenever you want fresher data):

```bash
docker compose run --rm freqtrade download-data \
  --config user_data/config.json \
  --timeframes 5m 15m 1h 4h \
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
- **Virtual clock** — jumps forward one candle at a time instantly instead of waiting for real time
- **Real bot** — everything else (signals, DCA, stoploss, fees, order matching) runs exactly as in production

The output is a real Freqtrade database, fully compatible with FreqUI and all Freqtrade analysis tools.

---

## Tips

- If a pair is missing data the tool will **auto-download** it before starting.
- If your strategy uses an **informative pair** (e.g. BTC as a filter) you do not need to add it to `--pairs` — it is detected and loaded automatically.
