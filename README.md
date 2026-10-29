# Freqtrade Replay

Run your Freqtrade strategy against historical data at full speed — in minutes instead of months.

Freqtrade Replay patches the exchange interface and virtual clock so the real Freqtrade bot replays stored candle data as fast as your CPU allows. The result is a standard Freqtrade SQLite database, fully compatible with FreqUI and all Freqtrade analysis tools.

---

## Quick start

You don't need to clone this repo. Everything is in the Docker image
`ghcr.io/saamy4r/freqtrade-replay`: the official `freqtradeorg/freqtrade:stable`
image, the replay code, and its extra dependencies.

### 1. What you need

- Docker
- A normal freqtrade `user_data/` folder containing:
  - `config.json`, with your exchange, `pair_whitelist` and a `StaticPairList`
  - your strategy in `user_data/strategies/`

Candle data does not have to be downloaded beforehand. The replay downloads
whatever is missing into `user_data/data/` on the first run.

### 2. Run a replay

From the folder that contains `user_data/`:

```bash
docker run --rm \
  -v "$PWD/user_data:/freqtrade/user_data" \
  ghcr.io/saamy4r/freqtrade-replay:latest \
  --strategy MyStrategy \
  --timerange 20250101-20250201
```

When the run finishes, a summary is printed:

```
  Closed trades: 42
  Win rate     : 28/42 (66.7%)
  Total P&L    : +183.24 USDT
```

All trades are saved in `user_data/tradesv3_replay.sqlite`.

### Or with Docker Compose

Add this service to the `docker-compose.yml` next to your `user_data/` folder:

```yaml
  replay:
    image: ghcr.io/saamy4r/freqtrade-replay:latest
    volumes:
      - "./user_data:/freqtrade/user_data"
    profiles:
      - replay
```

and run:

```bash
docker compose --profile replay run --rm replay \
  --strategy MyStrategy \
  --timerange 20250101-20250201
```

### Updating

```bash
docker pull ghcr.io/saamy4r/freqtrade-replay:latest
```

Every commit is also published as `:<git-sha>` if you want to pin an exact version.

---

## Options

Add any of these after the image name, or after `replay` with Compose:

| Flag | Default | Description |
|---|---|---|
| `--timerange` | required | `YYYYMMDD-YYYYMMDD` (or `YYYYMMDDHHMM-YYYYMMDDHHMM`) |
| `--strategy` | `MyStrategy` | Strategy class name |
| `--pairs` | config whitelist | Trade only these pairs, e.g. `--pairs BTC/USDT:USDT ETH/USDT:USDT` |
| `--config` | `user_data/config.json` | Path to the config inside the container |
| `--sub-step` | `1m` | Intra-candle resolution: `1m`, `5m`, or `15m` |
| `--slippage` | `0.0005` | Simulated bid-ask half-spread (0.05%) |
| `--workers` | all CPU cores | Processes for strategy analysis; `1` = fully serial |
| `--db` | `user_data/tradesv3_replay.sqlite` | Output database URL |
| `--datadir` | `user_data/data/binance/futures` | Folder with the `.feather` candle files |
| `--no-fresh` | off | Keep the existing replay DB instead of starting clean |
| `--report` | off | Also write an HTML report (equity curve + P&L per pair) |
| `--no-skip-idle` | off | Run every sub-step even when nothing can trade |

Paths are paths inside the container, where your `user_data` folder is
`/freqtrade/user_data`. For example, to get an HTML report in your `user_data`
folder:

```bash
... --report /freqtrade/user_data/replay_report.html
```

---

## View results in FreqUI

The replay database is a normal freqtrade database. To browse it in FreqUI, start
a regular freqtrade bot on it after the replay has finished:

```bash
docker run --rm -p 127.0.0.1:8080:8080 \
  -v "$PWD/user_data:/freqtrade/user_data" \
  freqtradeorg/freqtrade:stable trade \
  --config /freqtrade/user_data/config.json \
  --db-url sqlite:////freqtrade/user_data/tradesv3_replay.sqlite \
  --dry-run
```

Then open <http://127.0.0.1:8080> and log in with the `username` / `password`
from the `api_server` section of your `config.json`. That section must be enabled
with `"listen_ip_address": "0.0.0.0"` so the UI is reachable from outside the container.

> **Tip:** this viewer is a real dry-run bot running at the current time. Set
> `"initial_state": "stopped"` in the config you use for viewing, so it only
> shows the replay trades and never opens new ones.

### Isolation from your live bot

The replay writes to `tradesv3_replay.sqlite`, completely separate from the
`tradesv3.sqlite` your live bot uses. Trades, pair locks and wallet state never
mix between the two.

### Comparing two replays

`compare_replays.py` is in the image too. It reports whether two replay databases
contain identical trades, for example a serial run against a parallel run:

```bash
docker run --rm -v "$PWD/user_data:/freqtrade/user_data" \
  --entrypoint python ghcr.io/saamy4r/freqtrade-replay:latest \
  /opt/freqtrade_replay/freqtrade_replay/compare_replays.py \
  /freqtrade/user_data/replay_a.sqlite /freqtrade/user_data/replay_b.sqlite
```

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

### Multi-core (`--workers`)

The slow part of a replay is usually the strategy's indicator code, not the
trading. The replay therefore splits the work:

- **Analysis runs on all cores.** Worker processes compute each pair's
  analysed dataframe for upcoming candles ahead of time. Indicators depend
  only on candle data, so the workers never need to know about trades or capital.
- **Trading stays in one process.** A single, real `FreqtradeBot` owns the
  wallet, `max_open_trades`, pair locks, orders and fills. It uses the workers'
  dataframes instead of computing them, so capital and open-slot limits behave
  exactly as in a dry-run.

Callbacks such as `custom_stoploss` or `leverage` receive the same full
dataframe they would in a serial run. Use [`compare_replays.py`](#comparing-two-replays)
to check that a serial run (`--workers 1 --no-skip-idle`) and a parallel run
produced identical trades.

The speed-up scales with *physical* cores (hyper-threads add little). On a
4-core laptop it is about 2.8× for an indicator-heavy strategy.

On top of that, when there are no open trades and no entry signal, the replay
jumps straight to the next candle close, because nothing can happen in between.

Use `--workers 1` if your strategy's `populate_*` methods depend on open trades,
the wallet, or state stored on the strategy instance (`self.something`) across
candles. Workers work on copies of that state.

---

## Developing the replay itself

Only needed if you want to change the replay code. Clone the repo into
`user_data/` and point the entrypoint at your clone, so your edits are used
instead of the code in the image:

```bash
cd /path/to/your/freqtrade/user_data
git clone https://github.com/saamy4r/freqtrade_replay.git freqtrade_replay
```

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
