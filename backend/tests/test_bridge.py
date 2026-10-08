import os

os.environ["BRIDGE_SIM"] = "1"
os.environ["EXTERNAL_POLL_S"] = "3600"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.engines.base import TradeError, validate_stops  # noqa: E402
from app.timeframes import parse_timeframe, resample, ticks_to_tick_bars  # noqa: E402


def test_parse_timeframes():
    assert parse_timeframe("M1").seconds == 60
    assert parse_timeframe("H4").seconds == 14400
    assert parse_timeframe("MN1").seconds == 2592000
    assert parse_timeframe("5s").sub_minute
    assert parse_timeframe("250T").ticks == 250
    with pytest.raises(ValueError):
        parse_timeframe("abc")


def test_resample_and_tick_bars():
    bars = [{"time": t, "open": 1, "high": t, "low": 1, "close": t, "volume": 1} for t in range(0, 300, 60)]
    out = resample(bars, 180)
    assert [b["time"] for b in out] == [0, 180] and out[0]["high"] == 120 and out[0]["volume"] == 3
    ticks = [{"time_msc": 1000, "bid": 1.0, "ask": 1.1, "last": 0}] * 5
    tb = ticks_to_tick_bars(ticks, 2)
    assert [b["time"] for b in tb] == [1, 2, 3]  # strictly increasing despite equal seconds


def test_stop_validation():
    validate_stops(True, 100, 99, 101, 0)
    with pytest.raises(TradeError):
        validate_stops(True, 100, 101, 0, 0)
    with pytest.raises(TradeError):
        validate_stops(False, 100, 0, 101, 0)


def _req(ws, rid, **msg):
    ws.send_json({"id": rid, **msg})
    while True:
        m = ws.receive_json()
        if m.get("type") == "response" and m["id"] == rid:
            return m


def test_ws_end_to_end():
    from app.main import app
    with TestClient(app) as c:
        app.state.hub.feed.gave_up = True  # no external network in unit tests
        with c.websocket_connect("/ws") as ws:
            import time
            for _ in range(50):
                if app.state.hub.engine.connected:
                    break
                time.sleep(0.1)
            r = _req(ws, 1, type="subscribe", chartId="c1", symbol="USTEC", timeframe="5m", count=300)
            assert r["ok"], r
            assert r["data"]["symbol"] == "NAS100" and r["data"]["source"] == "mt5"  # alias routing
            assert len(r["data"]["bars"]) == 300
            r = _req(ws, 2, type="subscribe", chartId="c2", symbol="NAS100", timeframe="1s", count=200)
            assert r["ok"] and r["data"]["bars"]
            r = _req(ws, 3, type="order", symbol="NAS100", side="buy", kind="market", volume=1)
            assert r["ok"], r
            ticket = r["data"]["order"]
            r = _req(ws, 4, type="modify_sltp", ticket=ticket, sl=1, tp=0)
            assert r["ok"], r
            r = _req(ws, 5, type="modify_sltp", ticket=ticket, sl=10 ** 9, tp=0)
            assert not r["ok"] and "below" in r["error"]  # SL on wrong side is rejected
            r = _req(ws, 6, type="subscribe", chartId="c3", symbol="NASDAQ:AAPL", timeframe="1h")
            assert not r["ok"]  # external feed gave up -> clear error, no retry storm
            r = _req(ws, 7, type="close", ticket=ticket)
            assert r["ok"], r


def test_origin_guard():
    from app.main import _origin_allowed
    assert _origin_allowed("http://localhost:5173")
    assert not _origin_allowed("https://evil.example")
