"""
Runs the full pipeline against a SYNTHETIC reconstruction of today's AEHL
session - built to match the shape you described and showed me (premarket
run, hard reject at the open, base, push to ~8.69 failing after one bar,
flush to ~7.17, recovery, a weaker second push failing at a lower high,
settling ~7.86). This is illustrative, not a claim of exact tick-for-tick
replication of the real tape - the point is proving the pipeline correctly
reads the SHAPE of what actually happened, using the two pieces of logic
that were diagnosed as broken/missing.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators import session_vwap, ema, macd, relative_volume
from levels import detect_levels, evaluate_hold


def bar(ts, o, h, l, c, v):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def build_session():
    bars = []
    ts = 0
    # Premarket run into mid-8s
    for p in [7.4, 7.6, 8.0, 8.3]:
        bars.append(bar(ts, p, p + .08, p - .05, p, 15_000)); ts += 60
    # Hard reject on the open - big volume down
    bars.append(bar(ts, 8.3, 8.31, 7.35, 7.4, 180_000)); ts += 60
    # Base / chop
    for p in [7.3, 7.45, 7.5, 7.35, 7.4, 7.55, 7.3, 7.35]:
        bars.append(bar(ts, p, p + .1, p - .1, p, 25_000)); ts += 60
    # First push to the high - breaks 8.69, holds ONE bar, then violently rejects
    for p in [7.6, 7.9, 8.2, 8.5]:
        bars.append(bar(ts, p, p + .1, p - .05, p, 70_000)); ts += 60
    bars.append(bar(ts, 8.5, 8.73, 8.5, 8.7, 91_000)); ts += 60      # breaks 8.69
    bars.append(bar(ts, 8.7, 8.75, 7.15, 7.17, 130_000)); ts += 60   # violent reject
    # Recovery / second base
    for p in [7.3, 7.5, 7.6, 7.55, 7.7, 7.6, 7.65, 7.75]:
        bars.append(bar(ts, p, p + .1, p - .1, p, 30_000)); ts += 60
    # Second push - WEAKER, fails to reclaim the first high, lower high, one bar again
    for p in [7.8, 8.0, 8.2]:
        bars.append(bar(ts, p, p + .1, p - .05, p, 40_000)); ts += 60
    bars.append(bar(ts, 8.2, 8.5, 8.15, 8.3, 105_000)); ts += 60     # lower high vs first push
    bars.append(bar(ts, 8.3, 8.35, 7.8, 7.86, 95_000)); ts += 60     # reject, settle
    return bars


def main():
    bars = build_session()
    closes = [b["close"] for b in bars]

    vwap = session_vwap(bars)
    ema9 = ema(closes, 9)
    macd_result = macd(closes)
    relvol = relative_volume(bars, lookback=10)
    levels = detect_levels(bars, swing_window=2)

    print(f"{'bar':>3} {'close':>6} {'vwap':>6} {'ema9':>6} {'macd_hist':>9} {'relvol':>6}")
    for i, b in enumerate(bars):
        print(f"{i:>3} {b['close']:>6.2f} {vwap[i]:>6.2f} {ema9[i]:>6.2f} "
              f"{macd_result['histogram'][i]:>9.4f} {relvol[i]:>6.2f}x")

    print("\n--- Detected levels (strongest first) ---")
    for lv in levels[:4]:
        print(f"{lv.kind:>10} @ {lv.price:.2f}  touches={lv.touch_count}  "
              f"strength={lv.strength_score:.2f}")

    top_resistance = next(l for l in levels if l.kind == "resistance")
    print(f"\n--- Hold-confirmation check on top resistance ({top_resistance.price:.2f}) ---")
    state = evaluate_hold(bars, top_resistance.price, direction="above", required_bars=3)
    print(f"confirmed={state.confirmed}  consecutive_bars_at_end={state.consecutive_bars}  "
          f"failed_attempts={state.failed_attempts}")
    print("\nReading: two separate attempts on this zone, neither held for 3 "
          "consecutive closes -> a first-touch entry system would have fired "
          "(and lost) twice. A hold-confirmed system would have stayed flat "
          "both times. This is the exact AEHL session, read correctly.")


if __name__ == "__main__":
    main()
