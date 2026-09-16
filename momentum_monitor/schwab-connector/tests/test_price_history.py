import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from price_history import candles_to_bars, fetch_today_bars

RTH_1030 = 1756909800  # 2025-09-03 10:30:00 ET, a Wednesday


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

    class Frequency:
        EVERY_MINUTE = "EVERY_MINUTE_MARKER"


class _FakeClient:
    PriceHistory = _FakePriceHistoryNs

    def __init__(self, response):
        self._response = response
        self.calls = []

    async def get_price_history(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        return self._response


def test_fetch_today_bars_requests_current_day_minute_granularity():
    resp = _FakeResponse(200, {"candles": [
        {"datetime": RTH_1030 * 1000, "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1},
    ]})
    client = _FakeClient(resp)

    bars = asyncio.run(fetch_today_bars(client, "QCLS"))

    assert len(client.calls) == 1
    symbol, kwargs = client.calls[0]
    assert symbol == "QCLS"
    assert kwargs["period_type"] == _FakePriceHistoryNs.PeriodType.DAY
    assert kwargs["period"] == _FakePriceHistoryNs.Period.ONE_DAY
    assert kwargs["frequency_type"] == _FakePriceHistoryNs.FrequencyType.MINUTE
    assert kwargs["frequency"] == _FakePriceHistoryNs.Frequency.EVERY_MINUTE
    assert kwargs["need_extended_hours_data"] is True
    # No explicit date range: periodType=day/period=1 alone means "current
    # trading day", matching what a chart's default "1 Day" view shows.
    assert "start_datetime" not in kwargs
    assert "end_datetime" not in kwargs
    assert bars == [{"ts": RTH_1030, "open": 1.0, "high": 1.0, "low": 1.0,
                     "close": 1.0, "volume": 1.0, "is_extended": False}]


def test_fetch_today_bars_raises_on_error_status():
    client = _FakeClient(_FakeResponse(500, {}))
    with pytest.raises(RuntimeError):
        asyncio.run(fetch_today_bars(client, "QCLS"))


def test_fetch_today_bars_returns_empty_for_empty_candles():
    client = _FakeClient(_FakeResponse(200, {"candles": []}))
    assert asyncio.run(fetch_today_bars(client, "QCLS")) == []
