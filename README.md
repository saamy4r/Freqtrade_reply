# Freqtrade Replay

Run your Freqtrade strategy against historical data at full speed — in minutes instead of months.

Freqtrade Replay patches the exchange interface and virtual clock so the real Freqtrade bot replays stored candle data as fast as your CPU allows. The result is a standard Freqtrade SQLite database, fully compatible with FreqUI and all Freqtrade analysis tools.

---

## Requirements

- Docker and Docker Compose
- Freqtrade `user_data/` folder with your strategy and `config.json`

---

## Setup

**1. Clone into your `user_data` folder:**

```bash
cd /path/to/your/freqtrade/user_data
git clone https://github.com/saamy4r/Freqtrade_reply.git freqtrade_replay
```

**2. Add the replay service to `docker-compose.yml`:**

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

---

## Usage

```bash
docker compose --profile replay run --rm replay \
  --strategy MyStrategy \
  --timerange 20250101-20260101
```

Missing data is downloaded automatically before the run starts. When finished, a summary is printed to the terminal:

```
  Closed trades: 42
  Win rate     : 28/42 (66.7%)
  Total P&L    : +183.24 USDT
```

### Flags

| Flag | Default | Description |
|---|---|---|
| `--timerange` | required | Date range as `YYYYMMDD-YYYYMMDD` |
| `--strategy` | | Strategy class name |
| `--pairs` | config whitelist | Override trading pairs |
| `--config` | `user_data/config.json` | Path to config file |
| `--sub-step` | `1m` | Intra-candle resolution: `1m`, `5m`, or `15m` |
| `--slippage` | `0.0005` | Simulated bid-ask spread (0.05%) |
| `--datadir` | `user_data/data/binance/futures` | Path to feather data files |
| `--no-fresh` | off | Keep existing DB instead of starting clean |
| `--report` | off | Write a standalone HTML report to a file |

---

## View Results in FreqUI

The replay writes a standard SQLite database (`tradesv3_replay.sqlite`) that any
freqtrade dry-run can serve to FreqUI. After the replay finishes, point a normal
freqtrade bot at that database to browse trades and charts:

```bash
freqtrade trade \
  --config user_data/config.json \
  --db-url sqlite:////freqtrade/user_data/tradesv3_replay.sqlite \
  --dry-run
```

Open FreqUI at the port from your `config.json`'s `api_server` section and log in
with its `username` / `password`. Because this viewer bot runs at real
wall-clock time, charts for the historical replay trades load from your local
feather data, and it never interferes with the replay (which has already
finished). Stop the viewer when done.

> A live, concurrent FreqUI was intentionally removed: a viewer bot running
> *during* the replay processes pairs at the current wall-clock time, which both
> pollutes the shared replay database with its own trades and cannot render
> charts for the historical (past-dated) replay trades.

### Isolation from your live bot

The replay writes to `tradesv3_replay.sqlite`, completely separate from `tradesv3.sqlite` used by your live bot. Pair locks, trades, and wallet state from the live bot never bleed into the replay.

---

## How It Works

Freqtrade Replay replaces three components at runtime:

- **Exchange** — serves OHLCV data from local feather files instead of Binance
- **Clock** — advances virtual time instantly instead of waiting for real time
- **Bot** — the real `FreqtradeBot` runs unchanged: signals, DCA, stoploss, fees, and order matching all behave as in production

### Intra-candle accuracy

Backtesting checks stops and exits only at candle close. A live bot checks continuously — so a wick that hits your stop and recovers would trigger live but be missed in a backtest.

By default the replay steps through time at 1-minute resolution using real 1m candles, so stops and take-profits fire the same way they would in production. The strategy itself still runs on its own timeframe (15m, 1h, etc.).

### Speed vs accuracy (`--sub-step`)

| | Interval | Stop/TP accuracy | Speed |
|---|---|---|---|
| `--sub-step 1m` | 1 minute | highest | baseline |
| `--sub-step 5m` | 5 minutes | good | ~5× faster |
| `--sub-step 15m` | 15 minutes | approximate | ~15× faster |

Use `--sub-step 5m` during development and `1m` for final validation.
