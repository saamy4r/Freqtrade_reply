#!/usr/bin/env python3
"""
Compare the trades of two replay databases — used to check that a parallel
replay (--workers N) produces exactly the same trades as a serial one
(--workers 1 --no-skip-idle).

Usage:
    python compare_replays.py serial.sqlite parallel.sqlite
Exit code 0 when identical, 1 otherwise.
"""

import sqlite3
import sys

COLUMNS = (
    "pair", "is_short", "open_date", "close_date", "open_rate", "close_rate",
    "amount", "stake_amount", "leverage", "enter_tag", "exit_reason", "close_profit_abs",
)
FLOAT_TOL = 1e-9


def load(path: str) -> list[tuple]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(
            f"SELECT {', '.join(COLUMNS)} FROM trades ORDER BY open_date, pair, id"
        ).fetchall()
    finally:
        con.close()


def same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= FLOAT_TOL * max(1.0, abs(a), abs(b))
    return a == b


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = load(sys.argv[1]), load(sys.argv[2])
    print(f"{sys.argv[1]}: {len(a)} trades")
    print(f"{sys.argv[2]}: {len(b)} trades")
    for i, (ra, rb) in enumerate(zip(a, b)):
        diffs = [c for c, x, y in zip(COLUMNS, ra, rb) if not same(x, y)]
        if diffs:
            print(f"First difference at trade #{i + 1}:")
            for c, x, y in zip(COLUMNS, ra, rb):
                print(f"  {'*' if c in diffs else ' '} {c:<17} {x!s:<28} {y!s}")
            return 1
    if len(a) != len(b):
        print(f"Trade counts differ; first {min(len(a), len(b))} trades are identical.")
        return 1
    print("IDENTICAL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
