"""
Pure, framework-free indicator math. Every function here takes plain data in
and returns plain data out - no I/O, no threading, no Qt, no network. This is
deliberate: this is exactly the layer that got welded into a 2,288-line UI
controller in ToS_Companion, which made it impossible to trust or test in
isolation. It doesn't happen again here - this module has zero knowledge that
a UI, a broker, or a stream even exist.

A "bar" is a plain dict: {"ts": <unix seconds>, "open": float, "high": float,
"low": float, "close": float, "volume": float, "is_extended": bool}
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field


def session_vwap(bars: list[dict]) -> list[float]:
    """
    Cumulative session VWAP, resetting at the first bar in the list (caller
    is responsible for passing only bars from the current session - this
    function doesn't know what a "session boundary" is, on purpose).
    Returns one VWAP value per input bar.
    """
    out = []
    cum_pv = 0.0
    cum_vol = 0.0
    for b in bars:
        typical = (b["high"] + b["low"] + b["close"]) / 3.0
        cum_pv += typical * b["volume"]
        cum_vol += b["volume"]
        out.append(cum_pv / cum_vol if cum_vol > 0 else b["close"])
    return out


@dataclass
class SessionVwapState:
    """Persistent, incremental sibling of `session_vwap` (specs.md
    section 28/29, build_state incremental architecture): identical
    cumulative-VWAP math, but `update()` is fed ONE new bar at a time and
    keeps running totals across calls, instead of the caller recomputing
    the full session's bars from scratch on every call. Carries no
    notion of a session BOUNDARY itself, same as `session_vwap` ("this
    function doesn't know what a session boundary is, on purpose") --
    resetting at a new session (a fresh `SessionVwapState()`) is the
    CALLER's responsibility, exactly as it already is for the full-
    recompute version via `session_bars_for_vwap`."""
    cum_pv: float = 0.0
    cum_vol: float = 0.0

    def update(self, bar: dict) -> float:
        """Fold in ONE new bar, return the current session VWAP -- same
        formula, same order of operations, as one iteration of
        `session_vwap`'s loop body, so results are bit-identical."""
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        self.cum_pv += typical * bar["volume"]
        self.cum_vol += bar["volume"]
        return self.cum_pv / self.cum_vol if self.cum_vol > 0 else bar["close"]

    @classmethod
    def from_bars(cls, bars: list[dict]) -> "SessionVwapState":
        """One-time rebuild from a full bar history -- used once per
        session start (a fresh watch's initial backfill batch, or a
        restart's one-time reconstruction from existing history), never
        per-bar thereafter. Lands in exactly the state continuous
        `update()` calls from empty would have produced."""
        state = cls()
        for b in bars:
            state.update(b)
        return state


def ema(values: list[float], period: int) -> list[float]:
    """Standard exponential moving average. First value seeds on itself."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema_time_aware(values: list[float], timestamps: list[float], period: int,
                   reference_interval_seconds: float = 10.0) -> list[float]:
    """Time-aware EMA (specs.md section 15, phase 3.6 stage 1): decays by
    REAL elapsed seconds between bars rather than by bar count, so it
    weights a value correctly even when bars aren't evenly spaced (a
    backfilled gap, a quiet stretch Schwab skips). `timestamps[i]` is
    bar i's own ts, parallel to `values[i]` -- kept as a separate list
    (not read off a bar dict) so this can run on a derived series (e.g. a
    macd line) that shares the underlying bars' timestamps without being
    its own list of bars.

    Derivation, shown in full in specs.md section 15: the bar-count EMA's
    recursion is out[i] = k*v[i] + (1-k)*out[i-1], k = 2/(period+1). The
    (1-k) factor is the fraction of the old value retained after ONE
    reference-length step. Generalizing to an arbitrary elapsed time dt,
    the retained fraction after dt seconds is (1-k)**(dt/reference_interval_
    seconds) (compounding the per-reference-step retention continuously),
    so the effective per-step weight on the new value is
    k_eff(dt) = 1 - (1-k)**(dt/reference_interval_seconds).
    At dt == reference_interval_seconds (every live bar), this reduces
    mathematically to EXACTLY k. The dt == reference_interval_seconds case
    is special-cased below to use k directly rather than going through
    `**`, because floating-point pow does not always round-trip losslessly
    at exponent 1.0 (e.g. period=5: 1-(1-2/6)**1.0 == 0.33333333333333326,
    not 0.3333333333333333) -- this guarantees BIT-EXACT equivalence with
    `ema()` on uniform-cadence data, not merely equal in real-number terms.
    """
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for i in range(1, len(values)):
        dt = timestamps[i] - timestamps[i - 1]
        if dt == reference_interval_seconds:
            k_eff = k
        else:
            k_eff = 1 - (1 - k) ** (dt / reference_interval_seconds)
        out.append(values[i] * k_eff + out[-1] * (1 - k_eff))
    return out


@dataclass
class EmaTimeAwareState:
    """Persistent, incremental sibling of `ema_time_aware` (specs.md
    section 28/29, build_state incremental architecture): `update()` is
    fed ONE new (value, ts) pair at a time and keeps only the last EMA
    value and its timestamp across calls, instead of the caller
    recomputing the full series from scratch (and discarding everything
    but `[-1]`) on every call. `value`/`last_ts` are `None` only before
    the first `update()` -- the exact same "first value seeds on itself"
    rule `ema_time_aware` uses for `out[0]`."""
    value: float | None = None
    last_ts: float | None = None
    period: int = 9
    reference_interval_seconds: float = 10.0

    def update(self, value: float, ts: float) -> float:
        """Same k_eff derivation, same operation order, as one iteration
        of `ema_time_aware`'s loop body -- bit-identical results."""
        if self.value is None:
            self.value = value
            self.last_ts = ts
            return self.value
        k = 2.0 / (self.period + 1)
        dt = ts - self.last_ts
        if dt == self.reference_interval_seconds:
            k_eff = k
        else:
            k_eff = 1 - (1 - k) ** (dt / self.reference_interval_seconds)
        self.value = value * k_eff + self.value * (1 - k_eff)
        self.last_ts = ts
        return self.value

    @classmethod
    def from_series(cls, values: list[float], timestamps: list[float], period: int,
                    reference_interval_seconds: float = 10.0) -> "EmaTimeAwareState":
        """One-time rebuild from a full series -- same use (a fresh
        watch's first backfill batch, or a restart reconstruction) as
        `SessionVwapState.from_bars`."""
        full = ema_time_aware(values, timestamps, period, reference_interval_seconds)
        return cls(
            value=full[-1] if full else None,
            last_ts=timestamps[-1] if timestamps else None,
            period=period, reference_interval_seconds=reference_interval_seconds,
        )


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Returns {'macd': [...], 'signal': [...], 'histogram': [...]}, one value
    per input close, using EMA seeded on the first value (matches common
    charting-platform behavior closely enough for signal purposes; not meant
    to bit-match any specific vendor's warmup convention)."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema(macd_line, signal)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def macd_time_aware(closes: list[float], timestamps: list[float], fast: int = 12,
                    slow: int = 26, signal: int = 9,
                    reference_interval_seconds: float = 10.0) -> dict:
    """Time-aware `macd` (phase 3.6 stage 3 part 2, specs.md section 19):
    composed directly from `ema_time_aware` -- exactly the composition
    stage 1 anticipated ("macd's own time-aware version deferred to a
    later stage, composable directly from ema_time_aware once needed").
    Same structure as `macd`, just every `ema` call replaced with
    `ema_time_aware` fed the SAME `timestamps` the input `closes` share
    (the derived macd line lives at those same real moments in time, so
    its own signal-line EMA decays against the same real gaps). Bit-exact
    equal to `macd` on uniform cadence, by the already-proven exactness
    of `ema_time_aware` itself -- each component call is bit-identical,
    so the subtractions producing `macd_line`/`histogram` are too."""
    fast_ema = ema_time_aware(closes, timestamps, fast, reference_interval_seconds)
    slow_ema = ema_time_aware(closes, timestamps, slow, reference_interval_seconds)
    macd_line = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_line = ema_time_aware(macd_line, timestamps, signal, reference_interval_seconds)
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


@dataclass
class MacdTimeAwareState:
    """Persistent, incremental sibling of `macd_time_aware` (specs.md
    section 28/29, build_state incremental architecture): composed
    directly from three `EmaTimeAwareState` legs (fast/slow/signal),
    exactly mirroring how `macd_time_aware` itself composes three
    `ema_time_aware` calls -- the signal leg is fed the DERIVED macd
    line (fast - slow), not raw closes, same as the full-recompute
    version."""
    fast: EmaTimeAwareState
    slow: EmaTimeAwareState
    signal: EmaTimeAwareState

    @classmethod
    def new(cls, fast: int = 12, slow: int = 26, signal: int = 9,
           reference_interval_seconds: float = 10.0) -> "MacdTimeAwareState":
        return cls(
            fast=EmaTimeAwareState(period=fast, reference_interval_seconds=reference_interval_seconds),
            slow=EmaTimeAwareState(period=slow, reference_interval_seconds=reference_interval_seconds),
            signal=EmaTimeAwareState(period=signal, reference_interval_seconds=reference_interval_seconds),
        )

    def update(self, close: float, ts: float) -> dict:
        fast_val = self.fast.update(close, ts)
        slow_val = self.slow.update(close, ts)
        macd_val = fast_val - slow_val
        signal_val = self.signal.update(macd_val, ts)
        histogram = macd_val - signal_val
        return {"macd": macd_val, "signal": signal_val, "histogram": histogram}

    @classmethod
    def from_series(cls, closes: list[float], timestamps: list[float], fast: int = 12,
                    slow: int = 26, signal: int = 9,
                    reference_interval_seconds: float = 10.0) -> "MacdTimeAwareState":
        """One-time rebuild from a full series, same use as
        `EmaTimeAwareState.from_series`. Recomputes the same three
        `ema_time_aware` legs `macd_time_aware` itself would, so each
        leg's seeded `value`/`last_ts` matches exactly what continuous
        `update()` calls from empty would have produced."""
        fast_full = ema_time_aware(closes, timestamps, fast, reference_interval_seconds)
        slow_full = ema_time_aware(closes, timestamps, slow, reference_interval_seconds)
        macd_line = [f - s for f, s in zip(fast_full, slow_full)]
        signal_full = ema_time_aware(macd_line, timestamps, signal, reference_interval_seconds)
        last_ts = timestamps[-1] if timestamps else None
        return cls(
            fast=EmaTimeAwareState(value=fast_full[-1] if fast_full else None, last_ts=last_ts,
                                   period=fast, reference_interval_seconds=reference_interval_seconds),
            slow=EmaTimeAwareState(value=slow_full[-1] if slow_full else None, last_ts=last_ts,
                                   period=slow, reference_interval_seconds=reference_interval_seconds),
            signal=EmaTimeAwareState(value=signal_full[-1] if signal_full else None, last_ts=last_ts,
                                     period=signal, reference_interval_seconds=reference_interval_seconds),
        )


def relative_volume(bars: list[dict], lookback: int = 20) -> list[float]:
    """
    Each bar's volume divided by the rolling average of the preceding
    `lookback` bars. First `lookback` bars return 1.0 (not enough history
    to judge). This is deliberately a same-timeframe rolling comparison,
    not a comparison to a stale daily aggregate or to "yesterday" - see the
    conversation notes on why raw day-over-day volume comparison misleads
    given intraday volume's natural U-shape.
    """
    out = []
    for i, b in enumerate(bars):
        if i < lookback:
            out.append(1.0)
            continue
        window = bars[i - lookback:i]
        avg = sum(w["volume"] for w in window) / lookback
        out.append(b["volume"] / avg if avg > 0 else 1.0)
    return out


def _bar_duration(bars: list[dict], i: int, reference_interval_seconds: float) -> float:
    """A bar's own real width: the gap to whatever bar comes right after
    it, or `reference_interval_seconds` for the newest bar in the list
    (no next bar exists yet to measure from -- assume live cadence)."""
    if i + 1 < len(bars):
        return bars[i + 1]["ts"] - bars[i]["ts"]
    return reference_interval_seconds


def relative_volume_time_aware(bars: list[dict], lookback_seconds: float = 200.0,
                               reference_interval_seconds: float = 10.0) -> list[float]:
    """Time-aware relative_volume (specs.md sections 15/16, phase 3.6):
    the rolling comparison window is a real elapsed-time span
    (`lookback_seconds`) rather than a fixed bar count, so it selects the
    same real history regardless of gaps in cadence. `lookback=20` bars at
    the live 10s cadence is `lookback_seconds=200.0` (20 * 10s) -- the
    default here.

    Window is every prior bar with `b["ts"] - lookback_seconds <= w["ts"]
    < b["ts"]` (matches the bar-count version's `bars[i-lookback:i]`
    slice exactly on uniform cadence: both are the `lookback`-bars/
    `lookback_seconds`-seconds strictly preceding the current bar).
    "Not enough history yet" is judged by real elapsed time since the
    first bar (`b["ts"] - bars[0]["ts"] < lookback_seconds`), the direct
    time-based analog of the bar-count version's `i < lookback` check.

    Mixed-cadence weighting (specs.md section 16): when every bar in the
    window has EXACTLY `reference_interval_seconds` width, this compares
    raw per-bar volume, bit-identical to stage 1 (and to `relative_
    volume` on uniform cadence -- proven in section 15). Once widths
    actually vary (a 60s backfilled bar alongside 10s live bars), raw
    per-bar volume is the wrong thing to average -- a 60s bar naturally
    carries ~6x a 10s bar's volume at the SAME underlying rate, so
    averaging them as equals misreads a normal rate as unusually high or
    low depending on how many wide bars happen to be in the window.
    Comparing volume RATES (volume / duration) instead -- both for the
    window's aggregate average and for the current bar itself -- fixes
    this, and still reduces to the exact same ratio as raw-volume
    comparison on uniform data (dividing both sides by the same constant
    duration cancels out), which is why the uniform branch below is kept
    as its own bit-exact path rather than relying on that algebraic
    cancellation to hold under floating point.
    """
    out = []
    for i, b in enumerate(bars):
        if b["ts"] - bars[0]["ts"] < lookback_seconds:
            out.append(1.0)
            continue
        window_idxs = [j for j in range(i)
                       if b["ts"] - lookback_seconds <= bars[j]["ts"] < b["ts"]]
        if not window_idxs:
            out.append(1.0)
            continue
        durations = [_bar_duration(bars, j, reference_interval_seconds) for j in window_idxs]
        if all(d == reference_interval_seconds for d in durations):
            avg = sum(bars[j]["volume"] for j in window_idxs) / len(window_idxs)
            cur_rate = b["volume"]
        else:
            total_vol = sum(bars[j]["volume"] for j in window_idxs)
            total_dur = sum(durations)
            avg = total_vol / total_dur
            cur_rate = b["volume"] / _bar_duration(bars, i, reference_interval_seconds)
        out.append(cur_rate / avg if avg > 0 else 1.0)
    return out


@dataclass(frozen=True)
class _RelvolWindowEntry:
    ts: float
    volume: float
    duration: float


@dataclass
class RelativeVolumeState:
    """Persistent, incremental sibling of `relative_volume_time_aware`
    (specs.md section 28/29, build_state incremental architecture -- the
    ORIGINAL dominant, quadratic-per-call cost the whole incident started
    from): `update()` is fed ONE new bar at a time, maintaining a sliding
    window (a `deque` plus running sums) instead of the caller rescanning
    ALL prior bars from index 0 on every call.

    Same one-bar-delayed lookahead problem as `EvaluateHoldTimeAwareState`
    (a bar's own duration, `_bar_duration`, depends on the NEXT bar's ts):
    the most recently processed bar stays `pending_bar` -- its own rate
    is computed using the reference-width ESTIMATE (matching the
    bar-count version's fallback for whichever bar is currently last)
    until `update()` is next called, at which point it's folded into the
    window with its REAL finalized duration (new_bar.ts - pending_bar.ts)
    -- exactly matching what a fresh full recompute over the now-longer
    bars list would produce, since real lookahead is always available for
    every window element (window bars are always j < i, so bars[j+1]
    always exists by the time index i is being computed) except the
    list's own last bar.

    The uniform-vs-mixed-duration branch (specs.md section 16) is
    tracked via a running `nonuniform_count` (incremented/decremented as
    a duration != `reference_interval_seconds` entry enters/leaves the
    window) instead of rechecking every window element's duration on
    every call."""
    lookback_seconds: float = 200.0
    reference_interval_seconds: float = 10.0
    first_ts: float | None = None
    pending_bar: dict | None = None
    window: deque = field(default_factory=deque)  # _RelvolWindowEntry, chronological
    sum_vol: float = 0.0
    sum_dur: float = 0.0
    nonuniform_count: int = 0

    def _evict_before(self, cutoff_ts: float) -> None:
        while self.window and self.window[0].ts < cutoff_ts:
            entry = self.window.popleft()
            self.sum_vol -= entry.volume
            self.sum_dur -= entry.duration
            if entry.duration != self.reference_interval_seconds:
                self.nonuniform_count -= 1

    def update(self, bar: dict) -> float:
        if self.first_ts is None:
            self.first_ts = bar["ts"]
        if self.pending_bar is not None:
            # The pending bar now has a real next bar -- fold it into the
            # window with its REAL finalized duration, same as a fresh
            # recompute over the now-longer bars list would for that
            # same index (window elements always have real lookahead).
            duration = bar["ts"] - self.pending_bar["ts"]
            entry = _RelvolWindowEntry(ts=self.pending_bar["ts"],
                                       volume=self.pending_bar["volume"],
                                       duration=duration)
            self.window.append(entry)
            self.sum_vol += entry.volume
            self.sum_dur += entry.duration
            if entry.duration != self.reference_interval_seconds:
                self.nonuniform_count += 1
        self.pending_bar = bar
        self._evict_before(bar["ts"] - self.lookback_seconds)

        if bar["ts"] - self.first_ts < self.lookback_seconds:
            return 1.0
        if not self.window:
            return 1.0
        if self.nonuniform_count == 0:
            avg = self.sum_vol / len(self.window)
            cur_rate = bar["volume"]
        else:
            avg = self.sum_vol / self.sum_dur
            # This bar is now the newest, so its own duration isn't
            # known yet -- the same reference-width estimate the
            # bar-count version's `_bar_duration` fallback uses.
            cur_rate = bar["volume"] / self.reference_interval_seconds
        return cur_rate / avg if avg > 0 else 1.0

    @classmethod
    def from_bars(cls, bars: list[dict], lookback_seconds: float = 200.0,
                  reference_interval_seconds: float = 10.0) -> "RelativeVolumeState":
        """One-time rebuild from a full bar history -- same use (a fresh
        watch's first backfill batch, or a restart reconstruction) as
        `SessionVwapState.from_bars`/`EvaluateHoldTimeAwareState.
        from_bars`. Builds the final window directly from the real,
        finalized durations of whichever bars fall in the LAST bar's own
        lookback window (every one of them has real lookahead available,
        since the full list is given up front) -- landing in exactly the
        state continuous `update()` calls from empty would have produced."""
        state = cls(lookback_seconds=lookback_seconds,
                    reference_interval_seconds=reference_interval_seconds)
        if not bars:
            return state
        state.first_ts = bars[0]["ts"]
        last = bars[-1]
        for j in range(len(bars) - 1):
            bj = bars[j]
            if last["ts"] - lookback_seconds <= bj["ts"] < last["ts"]:
                duration = _bar_duration(bars, j, reference_interval_seconds)
                entry = _RelvolWindowEntry(ts=bj["ts"], volume=bj["volume"], duration=duration)
                state.window.append(entry)
                state.sum_vol += entry.volume
                state.sum_dur += entry.duration
                if entry.duration != reference_interval_seconds:
                    state.nonuniform_count += 1
        state.pending_bar = last
        return state


def continuation_days(daily_bars: list[dict], *, lookback_days: int,
                      threshold_pct: float) -> list[dict]:
    """Days, within the most recent `lookback_days` of `daily_bars`, whose
    day-over-day % change (close vs. prior close, SIGNED -- a big move
    either direction, not just up) exceeds `threshold_pct` in magnitude --
    {"ts", "pct_change"} dicts, oldest first. `threshold_pct` is a
    FRACTION (0.5 = 50%), the same convention every other "_pct" strategy
    parameter in this project uses.

    A day needs a PRIOR close to compute a % change from, so the window
    actually spans `lookback_days` + 1 bars of raw daily history; fewer
    bars than that (a symbol too new to have full history, or `daily_bars`
    empty/missing entirely) simply yields fewer comparisons, never a
    crash -- this function has no concept of "data unavailable" on its
    own, that distinction belongs to whoever calls it with real vs. empty
    input (specs.md section 7's continuation-vs-fresh-day gap: "unknown,
    no data" must never be confused with "checked, and it's fresh")."""
    if len(daily_bars) < 2:
        return []
    recent = daily_bars[-(lookback_days + 1):]
    out = []
    for i in range(1, len(recent)):
        prev_close = recent[i - 1]["close"]
        if prev_close == 0:
            continue
        pct_change = (recent[i]["close"] - prev_close) / prev_close
        if abs(pct_change) > threshold_pct:
            out.append({"ts": recent[i]["ts"], "pct_change": pct_change})
    return out
