"""
ParallelAnalyzer — runs strategy analysis on all CPU cores while the portfolio
stays in one process.

Why the work splits this way
----------------------------
Everything that depends on portfolio state (capital, max_open_trades, pair
locks, open orders, fills) lives in the single main-process FreqtradeBot and
is never shared or synchronised.  What runs in parallel is only the strategy
analysis (populate_indicators / entry / exit), which is a pure function of
(pair, candle data, virtual time, live-window size) — none of which depend on
open trades.  Profiling shows this is ~95% of replay time for indicator-heavy
strategies.

How
---
Pool workers are forked from the fully-initialised main process, so each
inherits an exact copy of the bot, strategy, ReplayExchange, data store and
virtual clock.  For a task (pair, t) a worker moves its own copy of the clock
to t, refreshes candles exactly as the main loop would at t, runs the original
analyze_pair() and returns the full analysed dataframe.

The main process schedules tasks in replay order with a bounded lookahead and
replaces strategy.analyze_pair with a lookup: at each new candle it installs
the worker's dataframe into the DataProvider instead of computing it.  Because
the dataframe is the full one computed from the same inputs, every callback
(custom_stoploss, leverage, confirm_trade_entry, iloc[-2] lookups) sees
exactly what it would in a serial replay.  Any mismatch (data gap, strategy
error, unscheduled pair) falls back to the original analyze_pair.

Assumption: populate_* must not depend on mutable strategy instance state or
on open trades/wallets — workers' copies of that state are frozen at fork
time.  Use --workers 1 for strategies that do.
"""

import logging
import multiprocessing as mp
from datetime import datetime, timezone

from freqtrade.enums import CandleType

logger = logging.getLogger(__name__)

# Set in the parent immediately before the pool forks; each worker inherits its
# own copy of the bot (and through it the exchange, store and clock).
_worker_bot = None
_worker_clock = None


def analysis_step_times(start_dt: datetime, end_dt: datetime, sub_step: int, tf_secs: int) -> list[int]:
    """
    Main-loop step times (epoch seconds) at which a new strategy candle first
    becomes visible: the first step, plus the first sub-step grid point at or
    after each candle close.  These are exactly the steps at which serial
    process_only_new_candles re-analyses.
    """
    start, end = int(start_dt.timestamp()), int(end_dt.timestamp())
    times = {start}
    close = (start // tf_secs + 1) * tf_secs
    while close < end:
        t = start + -(-(close - start) // sub_step) * sub_step
        if t < end:
            times.add(t)
        close += tf_secs
    return sorted(times)


def _init_worker() -> None:
    from freqtrade.persistence import Trade, init_db

    # The parent's pooled SQLite connections were copied by fork; discard them
    # without closing (they belong to the parent) and give the worker a private
    # in-memory DB so nothing it does can touch the replay database.
    try:
        Trade.session.get_bind().dispose(close=False)
    except Exception:
        pass
    init_db("sqlite://")
    logging.getLogger().setLevel(logging.WARNING)
    _single_thread_libs()


def _single_thread_libs() -> None:
    """Parallelism comes from the pool; BLAS / arrow / numexpr thread pools in
    every worker would only oversubscribe the cores."""
    try:
        from threadpoolctl import threadpool_limits
        threadpool_limits(1)
    except ImportError:
        pass
    try:
        import pyarrow
        pyarrow.set_cpu_count(1)
    except ImportError:
        pass
    try:
        import numexpr
        numexpr.set_num_threads(1)
    except ImportError:
        pass


def _analyze_task(pair: str, step: int):
    bot, clock = _worker_bot, _worker_clock
    clock.advance_to(datetime.fromtimestamp(step, tz=timezone.utc))
    tf = bot.strategy.timeframe
    candle_type = bot.config.get("candle_type_def", CandleType.SPOT)
    # Refresh exactly what the main loop's process() refreshes at this step, so
    # any cross-pair dp lookup inside populate_* sees the same candles.
    bot.dataprovider.refresh(
        bot.pairlists.create_pair_list(bot.active_pair_whitelist),
        bot.strategy.gather_informative_pairs(),
    )
    df = bot.dataprovider.ohlcv(pair, tf, candle_type=candle_type)
    if df.empty:
        return None, None
    latest = df.iloc[-1]["date"]
    bot.strategy.analyze_pair(pair)
    adf, _ = bot.dataprovider.get_analyzed_dataframe(pair, tf)
    if adf.empty or adf.iloc[-1]["date"] != latest:
        return latest, None  # analysis failed — main process falls back
    return latest, adf


class ParallelAnalyzer:
    def __init__(
        self,
        bot,
        clock,
        pairs: list[str],
        start_dt: datetime,
        end_dt: datetime,
        sub_step: int,
        workers: int,
    ) -> None:
        global _worker_bot, _worker_clock
        from freqtrade.exchange import timeframe_to_seconds

        self._bot = bot
        self._clock = clock
        self._tf = bot.strategy.timeframe
        self._candle_type = bot.config.get("candle_type_def", CandleType.SPOT)
        steps = analysis_step_times(start_dt, end_dt, sub_step, timeframe_to_seconds(self._tf))
        self._queue = [(step, pair) for step in steps for pair in pairs]
        self._next = 0
        self._inflight: dict = {}  # (pair, step) -> AsyncResult
        self._max_inflight = max(4 * workers, 2 * len(pairs))
        self._seen: dict[str, object] = {}
        self.hits = 0
        self.misses = 0

        _worker_bot, _worker_clock = bot, clock
        self._pool = mp.get_context("fork").Pool(workers, initializer=_init_worker)
        _worker_bot = _worker_clock = None

        # Installed after the fork so workers keep the original method.
        self._original = bot.strategy.analyze_pair
        bot.strategy.analyze_pair = self._analyze_pair
        logger.info(
            "Parallel analysis: %d workers, %d analysis tasks (%d pairs × %d candles)",
            workers, len(self._queue), len(pairs), len(steps),
        )

    def _top_up(self, now: int) -> None:
        # Tasks for steps already passed will never be consumed.
        for key in [k for k in self._inflight if k[1] < now]:
            del self._inflight[key]
        while self._next < len(self._queue) and self._queue[self._next][0] < now:
            self._next += 1
        while self._next < len(self._queue) and len(self._inflight) < self._max_inflight:
            step, pair = self._queue[self._next]
            self._next += 1
            self._inflight[(pair, step)] = self._pool.apply_async(_analyze_task, (pair, step))

    def _analyze_pair(self, pair: str) -> None:
        dp = self._bot.dataprovider
        df = dp.ohlcv(pair, self._tf, candle_type=self._candle_type)
        if df.empty:
            return self._original(pair)
        latest = df.iloc[-1]["date"]
        if self._seen.get(pair) == latest:
            return  # same candle — serial process_only_new_candles skips too

        now = int(self._clock.now().timestamp())
        self._top_up(now)
        pending = self._inflight.pop((pair, now), None)
        adf = None
        if pending is not None:
            try:
                task_latest, adf = pending.get()
            except Exception as exc:
                logger.warning("Worker analysis of %s failed (%s) — analysing in main process", pair, exc)
                task_latest, adf = None, None
            if task_latest != latest:
                adf = None
        if adf is None:
            self.misses += 1
            self._original(pair)
        else:
            self.hits += 1
            dp._set_cached_df(pair, self._tf, adf, candle_type=self._candle_type)
        self._seen[pair] = latest
        self._top_up(now)

    def close(self) -> None:
        self._bot.strategy.analyze_pair = self._original
        self._pool.terminate()
        self._pool.join()
        logger.info(
            "Parallel analysis: %d candles from workers, %d analysed in main process",
            self.hits, self.misses,
        )
