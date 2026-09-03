"""
Regenerate fixtures/replay_sample.jsonl.

Takes the synthetic AEHL session shape from core/demo.py (premarket run,
open reject, base, a push to ~8.69 that holds one bar and fails, a flush, a
weaker second push that also fails) and re-times it onto a real 10-second
grid anchored at a recent regular-session start, so the replayed demo shows
plausible timestamps, is_extended flags, and a session VWAP.

This is illustrative data for exercising the pipeline end to end without a
live Schwab token -- not real tape. Run:  python fixtures/make_sample.py
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "core"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "schwab-connector"))

from aggregator import is_extended_hours  # schwab-connector
from demo import build_session  # core

# Anchor: 2026-09-02 (Wed) 09:31:00 America/New_York -> a few minutes into RTH.
ANCHOR = int(datetime(2026, 9, 2, 9, 31, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())
OUT = os.path.join(HERE, "replay_sample.jsonl")


def main() -> None:
    session = build_session()  # ~40 bars, priced but untimed on a 10s grid
    lines = []
    for i, b in enumerate(session):
        ts = ANCHOR + i * 10
        lines.append(json.dumps({
            "ts": ts,
            "open": b["open"], "high": b["high"], "low": b["low"],
            "close": b["close"], "volume": b["volume"],
            "is_extended": is_extended_hours(ts),
        }))
    with open(OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {len(lines)} bars to {OUT} "
          f"({datetime.fromtimestamp(ANCHOR, ZoneInfo('America/New_York'))} "
          f"+ {10 * (len(lines) - 1)}s)")


if __name__ == "__main__":
    main()
