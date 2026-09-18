import asyncio
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from price_history import candles_to_bars, fetch_daily_history, fetch_today_bars

RTH_1030 = 1756909800  # 2025-09-03 10:30:00 ET, a Wednesday
_NY = ZoneInfo("America/New_York")


# -- candles_to_bars (pure mapping) -----------------------------------------

def test_candles_to_bars_maps_schwab_shape():
    candles = [
        {"datetime": RTH_1030 * 1000, "open": 10.0, "high": 10.6,
         "low": 9.9, "close": 10.4, "volume": 4000},
    ]
    bars = candles_to_bars(candles)
    assert bars == [{
        "ts": RTH_1030, "open": 10.0, "high": 10.6, "low": 9.9,
        "close": 10.4, "volume": 4000.0, "is_extended": False,
    }]


def test_candles_to_bars_flags_extended_hours():
    premarket_ts = RTH_1030 - 3 * 3600  # 07:30 ET, before the 09:30 open
    candles = [{"datetime": premarket_ts * 1000, "open": 1.0, "high": 1.0,
                "low": 1.0, "close": 1.0, "volume": 10}]
    bars = candles_to_bars(candles)
    assert bars[0]["is_extended"] is True


def test_candles_to_bars_drops_candles_with_no_datetime():
    candles = [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}]
    assert candles_to_bars(candles) == []


def test_candles_to_bars_defaults_missing_volume_to_zero():
    candles = [{"datetime": RTH_1030 * 1000, "open": 1.0, "high": 1.0,
                "low": 1.0, "close": 1.0}]
    assert candles_to_bars(candles)[0]["volume"] == 0.0


def test_candles_to_bars_sorts_by_ts():
    later = {"datetime": (RTH_1030 + 60) * 1000, "open": 2.0, "high": 2.0,
              "low": 2.0, "close": 2.0, "volume": 1}
    earlier = {"datetime": RTH_1030 * 1000, "open": 1.0, "high": 1.0,
                "low": 1.0, "close": 1.0, "volume": 1}
    bars = candles_to_bars([later, earlier])
    assert [b["ts"] for b in bars] == [RTH_1030, RTH_1030 + 60]


# -- fetch_today_bars (thin network call, fake client) ----------------------

class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakePriceHistoryNs:
    class PeriodType:
        DAY = "DAY_MARKER"

    class Period:
        ONE_DAY = "ONE_DAY_MARKER"

    class FrequencyType:
        MINUTE = "MINUTE_MARKER"
        DAILY = "DAILY_MARKER"

    class Frequency:
        EVERY_MINUTE = "EVERY_MINUTE_MARKER"
        DAILY = "DAILY_FREQ_MARKER"


class _FakeClient:
    PriceHistory = _FakePriceHistoryNs

    def __init__(self, response):
        self._response = response
        self.calls = []

    async def get_price_history(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return self._response


def test_fetch_today_bars_requests_explicit_start_end_for_current_session():
    # Regression test. period_type=DAY/period=ONE_DAY with no explicit date
    # range was confirmed -- live, against the real Schwab API, backfilling
    # QCLS on 2026-09-16 -- to return the PREVIOUS completed trading day
    # (9/15), not the current in-progress one, per get_price_history's own
    # docstring ("end_datetime: ... Default is previous trading day"). That
    # silently reproduced the exact cold-start VWAP bug this backfill exists
    # to fix, just one day later. An explicit start/end range sidesteps
    # Schwab's period-based default entirely.
    resp = _FakeResponse(200, {"candles": [
        {"datetime": RTH_1030 * 1000, "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1},
    ]})
    client = _FakeClient(resp)
    now = datetime(2026, 9, 16, 12, 0, 0, tzinfo=_NY)

    bars = asyncio.run(fetch_today_bars(client, "QCLS", now_fn=now.timestamp))

    assert len(client.calls) == 1
    symbol, kwargs = client.calls[0]
    assert symbol == "QCLS"
    assert kwargs["frequency_type"] == _FakePriceHistoryNs.FrequencyType.MINUTE
    assert kwargs["frequency"] == _FakePriceHistoryNs.Frequency.EVERY_MINUTE
    assert kwargs["need_extended_hours_data"] is True
    # period_type/period must be absent: get_price_history's own docstring
    # says period "should not be provided if start_datetime and
    # end_datetime" are -- mixing them is what caused the wrong-day bug.
    assert "period_type" not in kwargs
    assert "period" not in kwargs
    assert kwargs["start_datetime"] == datetime(2026, 9, 16, 0, 0, 0, tzinfo=_NY)
    assert kwargs["end_datetime"] == now
    assert bars == [{"ts": RTH_1030, "open": 1.0, "high": 1.0, "low": 1.0,
                     "close": 1.0, "volume": 1.0, "is_extended": False}]


def test_fetch_today_bars_raises_on_error_status():
    client = _FakeClient(_FakeResponse(500, {}))
    with pytest.raises(RuntimeError):
        asyncio.run(fetch_today_bars(client, "QCLS"))


def test_fetch_today_bars_returns_empty_for_empty_candles():
    client = _FakeClient(_FakeResponse(200, {"candles": []}))
    assert asyncio.run(fetch_today_bars(client, "QCLS")) == []


# -- fetch_daily_history (session-level volume gate baseline, specs.md
# section 12) ----------------------------------------------------------

def test_fetch_daily_history_requests_explicit_daily_range_ending_yesterday():
    # Same "explicit start/end range, not period_type" discipline
    # fetch_today_bars already established, for the same reason -- and
    # end_datetime is deliberately today's own NY midnight (EXCLUSIVE of
    # today), never today's still-forming partial-day volume.
    resp = _FakeResponse(200, {"candles": [
        {"datetime": (RTH_1030 - 86400) * 1000, "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 500_000},
    ]})
    client = _FakeClient(resp)
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=_NY)

    bars = asyncio.run(fetch_daily_history(client, "QCLS", lookback_days=20,
                                           now_fn=now.timestamp))

    assert len(client.calls) == 1
    symbol, kwargs = client.calls[0]
    assert symbol == "QCLS"
    assert kwargs["frequency_type"] == _FakePriceHistoryNs.FrequencyType.DAILY
    assert kwargs["frequency"] == _FakePriceHistoryNs.Frequency.DAILY
    assert kwargs["need_extended_hours_data"] is False
    assert "period_type" not in kwargs
    assert "period" not in kwargs
    assert kwargs["end_datetime"] == datetime(2026, 9, 18, 0, 0, 0, tzinfo=_NY)
    assert kwargs["start_datetime"] < kwargs["end_datetime"]
    assert len(bars) == 1


def test_fetch_daily_history_returns_at_most_lookback_days_most_recent():
    candles = [
        {"datetime": (RTH_1030 - 86400 * i) * 1000, "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1000.0 + i}
        for i in range(40)
    ]
    client = _FakeClient(_FakeResponse(200, {"candles": candles}))

    bars = asyncio.run(fetch_daily_history(client, "QCLS", lookback_days=20))

    assert len(bars) == 20
    all_bars = candles_to_bars(candles)
    assert bars == all_bars[-20:]  # the 20 MOST RECENT, oldest-first order kept


def test_fetch_daily_history_raises_on_error_status():
    client = _FakeClient(_FakeResponse(500, {}))
    with pytest.raises(RuntimeError):
        asyncio.run(fetch_daily_history(client, "QCLS"))


def test_fetch_daily_history_returns_empty_for_empty_candles():
    client = _FakeClient(_FakeResponse(200, {"candles": []}))
    assert asyncio.run(fetch_daily_history(client, "QCLS")) == []
