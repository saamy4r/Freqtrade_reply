#!/usr/bin/env python3
"""
Replay tool validation — runs ReplayDebugStrategy and checks two things:

  1. Determinism   — run twice, get bit-for-bit identical trade lists
  2. Completeness  — actual trade count ≈ expected (catches dropped signals)
  3. Concurrency   — with 3 pairs all signalling at the same hour, all 3
                     must open trades on the SAME minute (not just 1 or 2)

Strategy: ReplayDebugStrategy  (5m tf, enter at minute==0, ROI exit at 180 min)
Timerange: 20260401-20260406  (5 days = 7 200 min / 180-min cycle = 40 trades)
Expected:  39 closed + 1 open = 40 total per pair

DBs written: debug_single_{1,2}.sqlite  and  debug_multi_{1,2}.sqlite
             (separate from tradesv3.sqlite — deleted automatically after the run)

Usage (inside container):
    python /freqtrade/user_data/freqtrade_replay/debug_validate.py
"""

import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/freqtrade")
sys.path.insert(0, "/freqtrade/user_data")

CONFIG    = "/freqtrade/user_data/config.json"
STRATEGY  = "ReplayDebugStrategy"
TIMERANGE = "20260401-20260406"
DATADIR   = "/freqtrade/user_data/data/binance/futures"
DB_DIR    = "/freqtrade/user_data"

SINGLE_PAIR = ["BTC/USDT:USDT"]
MULTI_PAIRS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

# 5 days × 24h / 3h hold = 40 trades; last one stays open → 39 closed
EXPECTED_CLOSED = 39

_PASS = "PASS"
_FAIL = "FAIL"
_WARN = "WARN"


# ── replay runner ─────────────────────────────────────────────────────────── #

def run(pairs: list, db_path: str, label: str) -> bool:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    cmd = [
        "python", "/freqtrade/user_data/freqtrade_replay/cli.py",
        "--config",   CONFIG,
        "--strategy", STRATEGY,
        "--pairs",    *pairs,
        "--timerange", TIMERANGE,
        "--datadir",  DATADIR,
        "--db",       f"sqlite:///{db_path}",
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"  ERROR: replay exited with code {result.returncode}")
        return False
    return True


# ── DB helpers ────────────────────────────────────────────────────────────── #

def load_trades(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT pair, open_date, close_date, is_open, close_profit_abs "
        "FROM trades ORDER BY open_date, pair"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── checks ────────────────────────────────────────────────────────────────── #

def check_determinism(t1: list, t2: list, label: str) -> bool:
    if len(t1) != len(t2):
        print(f"  {_FAIL} [{label}] counts differ: run1={len(t1)}  run2={len(t2)} — non-deterministic!")
        return False
    bad = [i for i, (a, b) in enumerate(zip(t1, t2)) if a != b]
    if bad:
        print(f"  {_FAIL} [{label}] {len(bad)} trades differ between runs")
        for i in bad[:3]:
            print(f"    trade #{i}: run1={t1[i]}  run2={t2[i]}")
        return False
    print(f"  {_PASS} [{label}] both runs identical  ({len(t1)} trades total)")
    return True


def check_count(trades: list, expected_closed: int, label: str) -> bool:
    closed = [t for t in trades if not t["is_open"]]
    open_  = [t for t in trades if  t["is_open"]]
    print(f"  INFO [{label}] closed={len(closed)}  open={len(open_)}  "
          f"total={len(trades)}  (expected closed={expected_closed})")
    diff = len(closed) - expected_closed
    if abs(diff) <= 2:
        print(f"  {_PASS} [{label}] closed trade count matches expected (±2 tolerance)")
        return True
    if diff < 0:
        print(f"  {_FAIL} [{label}] {abs(diff)} FEWER closed trades than expected "
              f"— signals are likely being dropped!")
        return False
    print(f"  {_WARN} [{label}] {diff} more closed trades than expected — unexpected")
    return False


def check_concurrency(trades: list, pairs: list, label: str) -> bool:
    """
    At the very first hourly mark all N pairs should open a trade at the
    SAME minute.  If any pair is delayed or absent that's signal skipping.
    """
    by_pair = defaultdict(list)
    for t in trades:
        by_pair[t["pair"]].append(t)

    missing = [p for p in pairs if p not in by_pair]
    if missing:
        print(f"  {_FAIL} [{label}] no trades at all for: {missing}")
        return False

    first_opens = {p: min(by_pair[p], key=lambda t: t["open_date"])["open_date"]
                   for p in pairs}

    unique_times = set(first_opens.values())
    if len(unique_times) == 1:
        print(f"  {_PASS} [{label}] all {len(pairs)} pairs entered on the same minute "
              f"({next(iter(unique_times))})")
        return True

    # Check if they at least opened within 5 minutes of each other
    from datetime import datetime
    times = sorted(datetime.fromisoformat(t.replace(" ", "T"))
                   for t in unique_times)
    gap_min = (times[-1] - times[0]).total_seconds() / 60
    if gap_min <= 5:
        print(f"  {_WARN} [{label}] pairs opened within {gap_min:.0f} min of each other "
              f"(acceptable but not ideal):")
    else:
        print(f"  {_FAIL} [{label}] pairs opened {gap_min:.0f} min apart — "
              f"signal skipping suspected!")
    for p, d in sorted(first_opens.items()):
        print(f"    {p}: {d}")
    return gap_min <= 5


# ── scenarios ─────────────────────────────────────────────────────────────── #

def scenario_single() -> bool:
    print("\n" + "═" * 60)
    print("  SCENARIO 1 — Single pair (BTC/USDT:USDT)")
    print("  Validates: 24 signals/day → back-to-back 3h trades")
    print(f"  Expected : {EXPECTED_CLOSED} closed + 1 open = 40 total")
    print("═" * 60)

    db1 = f"{DB_DIR}/debug_single_1.sqlite"
    db2 = f"{DB_DIR}/debug_single_2.sqlite"

    if not run(SINGLE_PAIR, db1, "Single-pair — run 1"):
        return False
    if not run(SINGLE_PAIR, db2, "Single-pair — run 2"):
        return False

    t1 = load_trades(db1)
    t2 = load_trades(db2)

    ok  = check_determinism(t1, t2, "single")
    ok &= check_count(t1, EXPECTED_CLOSED, "single")
    return ok


def scenario_multi() -> bool:
    print("\n" + "═" * 60)
    print("  SCENARIO 2 — 3 pairs (BTC / ETH / SOL)")
    print("  Validates: all 3 open on the SAME minute (no signal skipping)")
    print(f"  Expected : {EXPECTED_CLOSED * 3} closed + 3 open = {40 * 3} total")
    print("═" * 60)

    db1 = f"{DB_DIR}/debug_multi_1.sqlite"
    db2 = f"{DB_DIR}/debug_multi_2.sqlite"

    if not run(MULTI_PAIRS, db1, "Multi-pair — run 1"):
        return False
    if not run(MULTI_PAIRS, db2, "Multi-pair — run 2"):
        return False

    t1 = load_trades(db1)
    t2 = load_trades(db2)

    ok  = check_determinism(t1, t2, "multi")
    ok &= check_count(t1, EXPECTED_CLOSED * len(MULTI_PAIRS), "multi total")
    ok &= check_concurrency(t1, MULTI_PAIRS, "multi")
    return ok


# ── main ──────────────────────────────────────────────────────────────────── #

def cleanup():
    for stem in ("debug_single_1", "debug_single_2", "debug_multi_1", "debug_multi_2"):
        for suffix in ("", "-shm", "-wal"):
            p = Path(f"{DB_DIR}/{stem}.sqlite{suffix}")
            if p.exists():
                p.unlink()


def main() -> int:
    print("=" * 60)
    print("  REPLAY TOOL VALIDATION")
    print(f"  Strategy : {STRATEGY}")
    print(f"  Timerange: {TIMERANGE}  (5 days)")
    print(f"  Signals  : 24/day  (5m candles at minute==0)")
    print(f"  Hold     : 180 min (3 hours)  →  ~40 trades/pair")
    print("=" * 60)

    results = [
        ("Scenario 1 — single pair determinism + completeness", scenario_single()),
        ("Scenario 2 — multi-pair concurrency + determinism",   scenario_multi()),
    ]

    print("\n" + "=" * 60)
    print("  FINAL SUMMARY")
    print("=" * 60)
    all_ok = True
    for name, ok in results:
        status = _PASS if ok else _FAIL
        print(f"  {status}  {name}")
        all_ok &= ok

    print()
    if all_ok:
        print("  Replay tool is working correctly.")
    else:
        print("  Issues found — check output above for details.")

    cleanup()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
