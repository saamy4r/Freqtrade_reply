#!/usr/bin/env python3
"""
Freqtrade Replay Harness — CLI entry point.

Example (inside container):
    python /freqtrade/user_data/freqtrade_replay/cli.py \
        --config /freqtrade/user_data/config.json \
        --pairs "BTC/USDT:USDT" "ETH/USDT:USDT" \
        --timerange 20241101-20241115

Docker run (from project root):
    docker compose --profile replay run --rm replay \
        --config /freqtrade/user_data/config.json \
        --pairs "BTC/USDT:USDT" "ETH/USDT:USDT" \
        --timerange 20241101-20241115
"""

import argparse
import logging
import sys
from datetime import datetime, timezone

# Ensure freqtrade and our package are importable inside the container
sys.path.insert(0, "/freqtrade")
sys.path.insert(0, "/freqtrade/user_data")

from freqtrade_replay.runner import run_replay

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Suppress noisy subsystems
for noisy in (
    "freqtrade.exchange",
    "freqtrade.rpc",
    "freqtrade.plugins",
    "freqtrade.resolvers",
    "urllib3",
    "asyncio",
    "ccxt",
):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%d").replace(tzinfo=timezone.utc)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run Freqtrade dry-run loop against historical data at max speed."
    )
    p.add_argument("--config", required=True, help="Path to freqtrade config.json")
    p.add_argument(
        "--pairs", required=True, nargs="+",
        help='Trading pairs, e.g. "BTC/USDT:USDT" "ETH/USDT:USDT"',
    )
    p.add_argument(
        "--timerange", required=True,
        help="Date range as YYYYMMDD-YYYYMMDD, e.g. 20241101-20241115",
    )
    p.add_argument(
        "--strategy", default="GKD_FisherTransformV4",
        help="Strategy class name (default: GKD_FisherTransformV4)",
    )
    p.add_argument(
        "--slippage", type=float, default=0.0005,
        help="Bid-ask half-spread fraction (default 0.0005 = 0.05%%). "
             "Controls order fill price and entry/exit pricing.",
    )
    p.add_argument(
        "--db", default="sqlite:////freqtrade/user_data/tradesv3.sqlite",
        help="SQLite DB URL for replay results. Compatible with freqtrade analysis tools.",
    )
    p.add_argument(
        "--datadir", default="/freqtrade/user_data/data/binance/futures",
        help="Directory containing .feather data files.",
    )
    p.add_argument(
        "--no-fresh", action="store_true",
        help="Keep existing DB instead of deleting it before the run.",
    )
    p.add_argument(
        "--report",
        default=None,
        metavar="PATH",
        help="Write a standalone HTML report (equity curve + per-pair P&L) to PATH. "
             "Example: /freqtrade/user_data/replay_report.html",
    )

    args = p.parse_args()

    try:
        start_str, end_str = args.timerange.split("-")
        start_dt = _parse_dt(start_str)
        end_dt = _parse_dt(end_str)
    except ValueError:
        p.error("--timerange must be YYYYMMDD-YYYYMMDD")

    if start_dt >= end_dt:
        p.error("Start date must be before end date")

    run_replay(
        config_path=args.config,
        pairs=args.pairs,
        start_dt=start_dt,
        end_dt=end_dt,
        strategy=args.strategy,
        slippage_pct=args.slippage,
        db_url=args.db,
        datadir=args.datadir,
        fresh=not args.no_fresh,
        report_path=args.report,
    )


if __name__ == "__main__":
    main()
