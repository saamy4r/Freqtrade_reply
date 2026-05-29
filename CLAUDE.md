# Freqtrade Replay — CLAUDE.md

This file is the authoritative guide for working on this codebase. Read it before making any changes.

---

## What this project does

Freqtrade Replay runs the real `FreqtradeBot` against historical OHLCV data at maximum CPU speed by replacing three components at runtime:

- **Exchange** (`ReplayExchange`) — serves local feather files instead of hitting Binance
- **Clock** (`VirtualClock`) — freezes and advances simulated time via freezegun instead of sleeping
- **Worker sleep** — patched to advance virtual time instead of blocking

Everything else — dry-run order creation, fee calculation, DCA, stoploss logic, wallet tracking, pair locks, persistence — is the **real unmodified Freqtrade code**. This is the fundamental design constraint: we never mock what we don't have to.

The output is a standard SQLite database fully compatible with FreqUI and all Freqtrade analysis tools.

---

## Repository layout

```
freqtrade_replay/
├── cli.py           Entry point, argument parsing
├── runner.py        Core orchestration: patches, main loop, summary, FreqUI config
├── clock.py         VirtualClock — wraps freezegun
├── exchange.py      ReplayExchange — offline OHLCV/ticker/orderbook
├── data_store.py    ReplayDataStore — loads feather files, serves time-gated slices
├── Dockerfile       FROM freqtradeorg/freqtrade:stable + freezegun + plotly
└── README.md        User-facing setup and usage docs
```

The project lives at `/freqtrade/user_data/freqtrade_replay/` inside the container, mounted from `./user_data/freqtrade_replay/` on the host.

---

## Docker setup

The `replay` service in `docker-compose.yml` (one level above `user_data/`):

### `replay` service
- Image: `ghcr.io/saamy4r/freqtrade-replay:latest` (built from `Dockerfile`)
- Profile: `replay`
- Entrypoint: `python /freqtrade/user_data/freqtrade_replay/cli.py`
- Run with: `docker compose --profile replay run --rm replay --strategy MyStrategy --timerange 20241101-20241115`

### Viewing results in FreqUI

There is **no** dedicated live-viewer service. A concurrent FreqUI bot was
deliberately removed: running a viewer at real wall-clock time *during* the
replay both pollutes the shared `tradesv3_replay.sqlite` with its own trades and
cannot render charts for the historical (past-dated) replay trades, because its
DataProvider only holds the current wall-clock window.

To browse results, run a normal freqtrade dry-run against the replay DB **after**
the replay finishes:

```
freqtrade trade --config user_data/config.json \
  --db-url sqlite:////freqtrade/user_data/tradesv3_replay.sqlite --dry-run
```

### `freqtrade` service (existing live bot, unrelated)
- Port: `127.0.0.1:8081:8080`
- Uses `tradesv3.sqlite` (completely separate from the replay)

### Database isolation
- **Replay** writes to `tradesv3_replay.sqlite`
- **Live bot** writes to `tradesv3.sqlite`
- These never share a database. This was a deliberate fix — sharing caused pair locks from the live strategy to bleed into the replay.

---

## Key files explained

### `clock.py` — VirtualClock

Wraps `freeze_time` from freezegun. `start(dt)` freezes all `datetime.now()`, `time.time()`, `time.sleep()` globally — including in third-party modules that do `from datetime import datetime`. `advance_to(dt)` moves the frozen time forward. `stop()` restores real time.

**Critical**: Save `time.monotonic` as `_wall_clock = time.monotonic` **before** calling `clock.start()`. After freezegun activates, `time.monotonic` returns simulated time, breaking all elapsed-time measurements. See `runner.py:338`.

### `data_store.py` — ReplayDataStore

Loads all timeframes (`1m`, `5m`, `15m`, `1h`, `4h`) from feather files at startup. Serves time-gated slices via binary search (`searchsorted`), always excluding the currently-open candle (`open_time < up_to`), which mirrors live bot behaviour (`drop_incomplete=True`).

- `get_candles()` — capped at 500 rows (matches live freqtrade download limit, avoids recomputing indicators over 14k+ rows)
- `get_last_price()` — finest resolution first (1m → 4h)
- `get_candle_ohlc()` — used by the exchange for intra-candle stop/limit fill detection
- `validate()` — checks warmup candles and replay range coverage before starting

### `exchange.py` — ReplayExchange

Subclasses `freqtrade.exchange.Exchange`. Overrides:

| Method | What it does |
|---|---|
| `_init_ccxt` | Returns `MagicMock` — no network connection ever attempted |
| `reload_markets` | No-op — called every `process()` loop |
| `refresh_latest_ohlcv` | Serves from `ReplayDataStore` gated by `VirtualClock` |
| `klines` | Cache lookup with fallback to store (handles undeclared informative TFs) |
| `fetch_ticker` | Synthesises bid/ask from last close ± `slippage_pct / 2` |
| `fetch_l2_order_book` | Synthetic order book for `_dry_is_price_crossed()` |
| `check_dry_limit_order_filled` | Uses candle high/low for deferred orders (stop accuracy) |
| `get_funding_fees` | Returns 0.0 — known divergence |
| `set_leverage`, `set_margin_mode` | No-ops |

**Intra-candle fill logic**: For deferred orders (existing open orders being re-checked), the exchange uses the last completed candle's `high`/`low` to determine if a stoploss or limit order crossed, not the synthetic close±spread book. This matches live bot accuracy and is why `--sub-step 1m` gives the most accurate results.

### `runner.py` — Core orchestration

`run_replay()` does, in order:

1. **`_drop_db()`** — deletes `tradesv3_replay.sqlite` (+ WAL files) when `fresh=True`
2. **Data validation** — validates all pairs, auto-downloads if data is missing or starts too late
3. **Standard TF preload** — downloads all standard timeframes proactively (strategies may call `dp.get_pair_dataframe()` for any TF without declaring it in `informative_pairs()`)
4. **`VirtualClock.start()`** — freeze time at `data_start` (= `start_dt - startup_candle_count × tf_secs`)
5. **`ReplayExchange` construction**
6. **Patch `ExchangeResolver.load_exchange`** → returns our exchange
7. **Patch `Worker._sleep`** → advances virtual clock instead of blocking
8. **Patch `Wallets.record_wallet_state`** → upsert semantics (avoids UNIQUE constraint collision when multiple records share the same day-floor timestamp)
9. **`FreqtradeBot(config)`** initialisation
10. **`_load_informative_pairs()`** — detects and loads informative pairs declared by the strategy
11. **Main loop** — steps at `sub_step` seconds (default 60s = 1m), calls `bot.process()` at each step
12. **Summary + HTML report** (optional)
13. **Restore all patches** in `finally` block

---

## Patches and their rationale

| Patch | Location | Why |
|---|---|---|
| `ExchangeResolver.load_exchange` | `runner.py` | Force `FreqtradeBot` to use `ReplayExchange` instead of connecting to Binance |
| `Worker._sleep` | `runner.py` | Advance virtual clock instead of sleeping; without this the loop runs at real-time speed |
| `Wallets.record_wallet_state` | `runner.py` | Avoid SQLite UNIQUE constraint on `(timestamp, currency)` when the day-floor timestamp repeats across candles |
| `VirtualClock` (freezegun) | `clock.py` | Freeze all time sources globally so `datetime.now()`, `time.time()`, etc. return simulation time |

All patches are restored in a `try/finally` block in `run_replay()`.

---

## Known divergences from live trading

1. **Slippage model** — simplified half-spread applied uniformly. Real slippage is order-size dependent and varies by liquidity.
2. **Order book depth** — synthetic; always has infinite liquidity at bid/ask. Market impact is not modelled.
3. **Intra-candle price path** — only high/low bounds are known; the actual price path within a candle is not simulated (a wick could touch the stoploss and recover, and the exact fill timing within the candle is unknown).

Funding fees were previously a known divergence but are now fully implemented using the local `funding_rate` and `mark` feather files downloaded alongside OHLCV data.

---

## Data requirements

- Feather files at `user_data/data/binance/futures/` named `{PAIR}-{TF}-futures.feather`
- All five timeframes required: `1m`, `5m`, `15m`, `1h`, `4h`
- Data must cover `startup_candle_count` candles before `start_dt` (default 50 × tf_secs worth of warmup)
- Auto-download fetches 90 days before `start_dt` for warmup coverage
- If a feather file starts after the required warmup start, it is deleted and re-downloaded

---

## CLI flags

| Flag | Default | Description |
|---|---|---|
| `--timerange` | required | `YYYYMMDD-YYYYMMDD` |
| `--strategy` | `MyStrategy` | Strategy class name |
| `--pairs` | config whitelist | Override trading pairs |
| `--config` | `user_data/config.json` | Path to freqtrade config |
| `--sub-step` | `1m` | Intra-candle resolution: `1m`, `5m`, `15m` |
| `--slippage` | `0.0005` | Half-spread fraction for bid/ask and order fills |
| `--db` | `tradesv3_replay.sqlite` | Output SQLite DB (do not point at the live bot's DB) |
| `--datadir` | `user_data/data/binance/futures` | Feather file directory |
| `--no-fresh` | off | Keep existing DB instead of wiping |
| `--report` | off | Write standalone HTML report (plotly equity curve + per-pair P&L) |

---

## Viewing results in FreqUI

The replay does not run its own FreqUI. After a run finishes, point a normal
freqtrade dry-run at the replay DB to browse trades and charts:

```
freqtrade trade --config user_data/config.json \
  --db-url sqlite:////freqtrade/user_data/tradesv3_replay.sqlite --dry-run
```

- Login: `username` / `password` from the `api_server` section of `config.json`
- The viewer runs at real wall-clock time but only reads the finished replay DB,
  so it never interferes with the replay
- Charts for the historical trades load from the local feather data

---

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Pair X unavailable after download` | Pair delisted or renamed | Remove from config whitelist |
| `need N warmup candles before ...` | Data file starts too late | Delete the feather file; auto-download will fetch from 90 days before start |
| `database is locked` | Two processes writing same SQLite | Check db_url — replay and viewer/live bot must use different files |
| `funding fees will be 0.0 for this pair` warning | No `funding_rate`/`mark` feather data for the pair | Re-run `download-data --trading-mode futures` to fetch funding + mark candles |

---

## Development notes

- The replay image is published to `ghcr.io/saamy4r/freqtrade-replay:latest` via GitHub Actions on push to `main`
- `freezegun` and `plotly` are the only extra dependencies beyond the base freqtrade image
- `debug_validate.py` contains a `ReplayDebugStrategy` for validating harness behaviour
- Do not add `api_server` to the config passed to `FreqtradeBot` in `runner.py` — it is explicitly popped to avoid spawning an API server inside the replay container
- Do not add `telegram` to that config either — same reason
- The `Wallets.record_wallet_state` patch uses SQLite `INSERT OR REPLACE` (upsert) semantics because freqtrade floors wallet timestamps to the day, causing UNIQUE collisions when the replay processes multiple candles in the same calendar day
