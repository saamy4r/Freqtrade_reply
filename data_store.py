"""
ReplayDataStore — loads OHLCV feather files and serves time-gated slices.

Rules:
- get_candles() returns all CLOSED candles whose open_time < up_to.
  The currently-open candle (open_time == up_to) is excluded, exactly
  mirroring live bot behaviour (drop_incomplete=True).
- get_last_price() uses the finest available timeframe (1m > 5m > 15m > 1h > 4h)
  so that order-fill price is as accurate as possible within each candle.
- calculate_funding_fees() uses local funding_rate and mark feather files.
  Formula: Σ(funding_rate × mark_price × amount) per funding interval,
  negated for longs (identical to freqtrade's calculate_funding_fees).
"""

import logging
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h"]


def _normalise_dt(series: "pd.Series") -> "pd.Series":
    """Coerce a datetime column to UTC nanoseconds, regardless of source precision."""
    if series.dt.tz is None:
        series = series.dt.tz_localize("UTC")
    return series.dt.as_unit("ns")


class ReplayDataStore:
    def __init__(self, data_dir: str | Path, pairs: list[str], trading_mode: str = "futures") -> None:
        self._data_dir = Path(data_dir)
        self._pairs = list(pairs)
        self._trading_mode = trading_mode
        # pair -> timeframe -> DataFrame (sorted ascending by 'date')
        self._candles: dict[str, dict[str, pd.DataFrame]] = {}
        # pair -> DataFrame  (funding rate; open column = rate fraction)
        self._funding_rates: dict[str, pd.DataFrame] = {}
        # pair -> DataFrame  (mark price; open column = mark price in quote)
        self._mark_prices: dict[str, pd.DataFrame] = {}
        self._load_all()

    # ------------------------------------------------------------------
    # Internal loading
    # ------------------------------------------------------------------

    def _filename(self, pair: str, tf: str) -> Path:
        # "BTC/USDT:USDT" → "BTC_USDT_USDT-1h-futures.feather"
        base = pair.replace("/", "_").replace(":", "_")
        return self._data_dir / f"{base}-{tf}-{self._trading_mode}.feather"

    def _load_all(self) -> None:
        for pair in self._pairs:
            self._load_pair(pair)

    def _load_pair(self, pair: str) -> None:
        self._candles.setdefault(pair, {})
        base = pair.replace("/", "_").replace(":", "_")

        for tf in TIMEFRAMES:
            path = self._filename(pair, tf)
            if not path.exists():
                continue
            df = pd.read_feather(path)
            df = df.sort_values("date").reset_index(drop=True)
            if df["date"].dt.tz is None:
                df["date"] = df["date"].dt.tz_localize("UTC")
            self._candles[pair][tf] = df
            logger.info(
                "Loaded %s %s: %d candles  %s → %s",
                pair, tf, len(df),
                df.iloc[0]["date"].isoformat(), df.iloc[-1]["date"].isoformat(),
            )

        # Funding rate — try 8h (Binance default) then 1h
        for tf in ("8h", "1h"):
            path = self._data_dir / f"{base}-{tf}-funding_rate.feather"
            if not path.exists():
                continue
            df = pd.read_feather(path)[["date", "open"]].sort_values("date").reset_index(drop=True)
            df["date"] = _normalise_dt(df["date"])
            self._funding_rates[pair] = df
            logger.info("Loaded %s funding_rate (%s): %d rows", pair, tf, len(df))
            break

        # Mark price — 1h on Binance, but fall back to 8h (base freqtrade default)
        for tf in ("1h", "8h"):
            path = self._data_dir / f"{base}-{tf}-mark.feather"
            if not path.exists():
                continue
            df = pd.read_feather(path)[["date", "open"]].sort_values("date").reset_index(drop=True)
            df["date"] = _normalise_dt(df["date"])
            self._mark_prices[pair] = df
            logger.info("Loaded %s mark price (%s): %d rows", pair, tf, len(df))
            break

        # Funding fees silently evaluate to 0.0 when either input is missing.
        # Warn loudly so the user knows the replay is diverging from live results.
        if self._trading_mode == "futures":
            missing = []
            if pair not in self._funding_rates:
                missing.append("funding_rate")
            if pair not in self._mark_prices:
                missing.append("mark")
            if missing:
                logger.warning(
                    "%s: no %s data found in %s — funding fees will be 0.0 for this pair "
                    "(re-run download-data with --trading-mode futures to fetch it)",
                    pair, " and ".join(missing), self._data_dir,
                )

    def load_extra_pair(self, pair: str) -> bool:
        """Load data for an informative pair not in the original pairs list.
        Returns True if at least one timeframe was found on disk."""
        if self._candles.get(pair):
            return True
        self._load_pair(pair)
        return bool(self._candles.get(pair))

    def has_pair(self, pair: str) -> bool:
        return bool(self._candles.get(pair))

    def has_timeframe(self, pair: str, tf: str) -> bool:
        return self._candles.get(pair, {}).get(tf) is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    # Maximum rows returned to the strategy per call — limits indicator recomputation cost.
    # 500 > startup_candle_count (250), so warmup is always sufficient.
    MAX_CANDLES = 500

    def get_candles(self, pair: str, tf: str, up_to: datetime) -> pd.DataFrame:
        """
        All closed candles with open_time < up_to, capped at MAX_CANDLES most recent.

        Capping mirrors live-bot behaviour (Freqtrade downloads ~500 candles, not all history)
        and avoids recomputing indicators over 14k+ rows on every process() call.
        """
        df = self._candles.get(pair, {}).get(tf)
        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        up_to_ts = pd.Timestamp(up_to)
        # Binary search — O(log n) instead of O(n) boolean mask
        idx = int(df["date"].searchsorted(up_to_ts, side="left"))
        start = max(0, idx - self.MAX_CANDLES)
        return df.iloc[start:idx].reset_index(drop=True)

    def get_last_price(self, pair: str, up_to: datetime) -> float:
        """Last known close price strictly before up_to, finest resolution first."""
        up_to_ts = pd.Timestamp(up_to)
        for tf in TIMEFRAMES:
            df = self._candles.get(pair, {}).get(tf)
            if df is None or df.empty:
                continue
            # Binary search — O(log n)
            idx = int(df["date"].searchsorted(up_to_ts, side="left")) - 1
            if idx >= 0:
                return float(df.iloc[idx]["close"])
        raise ValueError(f"No price data for {pair} before {up_to}")

    def get_candle_ohlc(self, pair: str, tf: str, up_to: datetime) -> dict | None:
        """OHLC dict for the last completed candle of timeframe `tf` before `up_to`.
        Used by the exchange for intra-candle stop/limit fill detection."""
        df = self._candles.get(pair, {}).get(tf)
        if df is None or df.empty:
            return None
        up_to_ts = pd.Timestamp(up_to)
        idx = int(df["date"].searchsorted(up_to_ts, side="left")) - 1
        if idx < 0:
            return None
        row = df.iloc[idx]
        return {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }

    def calculate_funding_fees(
        self,
        pair: str,
        amount: float,
        is_short: bool,
        open_date: datetime,
        close_date: datetime,
    ) -> float:
        """
        Sum funding fees for a trade over [open_date, close_date].

        fee = Σ (funding_rate × mark_price × amount) for each funding event in the window.
        Positive for shorts when rate > 0 (shorts receive), negative for longs (longs pay).
        Identical to freqtrade's calculate_funding_fees formula.
        """
        funding_df = self._funding_rates.get(pair)
        mark_df = self._mark_prices.get(pair)

        if funding_df is None or funding_df.empty or mark_df is None or mark_df.empty:
            return 0.0

        def _to_ts(dt: datetime) -> pd.Timestamp:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return pd.Timestamp(dt)

        open_ts = _to_ts(open_date)
        close_ts = _to_ts(close_date)

        fund_slice = funding_df[
            (funding_df["date"] >= open_ts) & (funding_df["date"] <= close_ts)
        ].copy()

        if fund_slice.empty:
            return 0.0

        # Match each funding event to the nearest mark price (within 1h tolerance)
        merged = pd.merge_asof(
            fund_slice.rename(columns={"open": "fund_rate"}).sort_values("date"),
            mark_df.rename(columns={"open": "mark_price"}).sort_values("date"),
            on="date",
            tolerance=pd.Timedelta("1h"),
            direction="nearest",
        )

        merged = merged.dropna(subset=["mark_price"])
        if merged.empty:
            return 0.0

        fees = float((merged["fund_rate"] * merged["mark_price"] * amount).sum())
        if math.isnan(fees):
            fees = 0.0

        return fees if is_short else -fees

    def validate(
        self,
        pair: str,
        tf: str,
        start_dt: datetime,
        end_dt: datetime,
        startup_count: int,
    ) -> None:
        df = self._candles.get(pair, {}).get(tf)
        if df is None:
            raise ValueError(f"No {tf} data for {pair}. Check {self._filename(pair, tf)}")

        start_ts = pd.Timestamp(start_dt)
        end_ts = pd.Timestamp(end_dt)

        warmup = df[df["date"] < start_ts]
        if len(warmup) < startup_count:
            raise ValueError(
                f"{pair} {tf}: need {startup_count} warmup candles before {start_dt}, "
                f"have {len(warmup)}. Download more historical data."
            )

        replay_range = df[(df["date"] >= start_ts) & (df["date"] < end_ts)]
        if replay_range.empty:
            raise ValueError(f"{pair} {tf}: no data in range {start_dt} → {end_dt}")

        logger.info(
            "Validated %s %s: %d warmup + %d replay candles",
            pair, tf, len(warmup), len(replay_range),
        )
