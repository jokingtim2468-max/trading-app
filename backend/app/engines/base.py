"""Broker engine interface + helpers shared by the live MT5 engine and the simulator."""
from __future__ import annotations

import math
from typing import Any, Protocol

from ..timeframes import Timeframe

# MT5 enum values (identical in the simulator so the frontend never has to care).
POSITION_BUY, POSITION_SELL = 0, 1
ORDER_TYPES = {
    ("buy", "market"): 0, ("sell", "market"): 1,
    ("buy", "limit"): 2, ("sell", "limit"): 3,
    ("buy", "stop"): 4, ("sell", "stop"): 5,
}
ORDER_TYPE_NAMES = {0: "BUY", 1: "SELL", 2: "BUY LIMIT", 3: "SELL LIMIT", 4: "BUY STOP",
                    5: "SELL STOP", 6: "BUY STOP LIMIT", 7: "SELL STOP LIMIT"}


class TradeError(Exception):
    """Raised for requests rejected before they reach the broker, or by the broker."""


class BrokerEngine(Protocol):
    sim: bool
    connected: bool

    async def login(self, server: str, login: int, password: str, path: str | None = None) -> dict: ...
    async def logout(self) -> None: ...
    async def ping(self) -> bool: ...
    async def account(self) -> dict | None: ...
    async def find_symbol(self, name: str) -> dict | None: ...
    async def history(self, symbol: str, tf: Timeframe, count: int) -> list[dict]: ...
    async def ticks_since(self, symbols: list[str], last_msc: dict[str, int]) -> dict[str, list[dict]]: ...
    async def positions(self) -> list[dict]: ...
    async def orders(self) -> list[dict]: ...
    async def place_order(self, req: dict) -> dict: ...
    async def modify_position(self, ticket: int, sl: float, tp: float) -> dict: ...
    async def modify_order(self, ticket: int, price: float, sl: float, tp: float) -> dict: ...
    async def close_position(self, ticket: int, volume: float | None = None) -> dict: ...
    async def cancel_order(self, ticket: int) -> dict: ...


def normalize_volume(volume: float, vmin: float, vmax: float, step: float) -> float:
    if volume <= 0:
        raise TradeError("volume must be positive")
    steps = math.floor(volume / step + 1e-9)
    v = round(steps * step, 8)
    if v < vmin:
        raise TradeError(f"volume {volume} below broker minimum {vmin}")
    return min(v, vmax)


def validate_stops(is_buy: bool, ref_price: float, sl: float, tp: float, min_dist: float) -> None:
    """Reject SL/TP on the wrong side of the market before the broker does (0 = none).

    `ref_price` is the price the level will be *triggered* against: bid for buy
    positions, ask for sells (or the entry price for pending orders)."""
    if sl:
        if is_buy and sl > ref_price - min_dist:
            raise TradeError(f"BUY stop loss {sl} must be below {ref_price - min_dist:.10g}")
        if not is_buy and sl < ref_price + min_dist:
            raise TradeError(f"SELL stop loss {sl} must be above {ref_price + min_dist:.10g}")
    if tp:
        if is_buy and tp < ref_price + min_dist:
            raise TradeError(f"BUY take profit {tp} must be above {ref_price + min_dist:.10g}")
        if not is_buy and tp > ref_price - min_dist:
            raise TradeError(f"SELL take profit {tp} must be below {ref_price - min_dist:.10g}")


def validate_pending_price(side: str, kind: str, price: float, bid: float, ask: float) -> None:
    if kind == "limit":
        if side == "buy" and price >= ask:
            raise TradeError("BUY LIMIT must be below the current ask - use BUY STOP")
        if side == "sell" and price <= bid:
            raise TradeError("SELL LIMIT must be above the current bid - use SELL STOP")
    elif kind == "stop":
        if side == "buy" and price <= ask:
            raise TradeError("BUY STOP must be above the current ask - use BUY LIMIT")
        if side == "sell" and price >= bid:
            raise TradeError("SELL STOP must be below the current bid - use SELL LIMIT")


def position_dict(p: Any) -> dict:
    d = p._asdict() if hasattr(p, "_asdict") else dict(p)
    magic = int(d.get("magic", 0))
    return {
        "ticket": int(d["ticket"]),
        "symbol": d["symbol"],
        "type": int(d["type"]),
        "side": "buy" if int(d["type"]) == POSITION_BUY else "sell",
        "volume": float(d["volume"]),
        "price_open": float(d["price_open"]),
        "sl": float(d.get("sl", 0) or 0),
        "tp": float(d.get("tp", 0) or 0),
        "price_current": float(d.get("price_current", 0) or 0),
        "profit": round(float(d.get("profit", 0) or 0), 2),
        "swap": round(float(d.get("swap", 0) or 0), 2),
        "time": int(d.get("time", 0)),
        "magic": magic,
        "comment": d.get("comment", "") or "",
        "source": "manual" if magic == 0 else "ea",
        "label": "Manual" if magic == 0 else f"EA - {magic}",
    }


def order_dict(o: Any) -> dict:
    d = o._asdict() if hasattr(o, "_asdict") else dict(o)
    magic = int(d.get("magic", 0))
    otype = int(d["type"])
    return {
        "ticket": int(d["ticket"]),
        "symbol": d["symbol"],
        "type": otype,
        "type_name": ORDER_TYPE_NAMES.get(otype, str(otype)),
        "side": "buy" if otype % 2 == 0 else "sell",
        "volume": float(d.get("volume_current", d.get("volume_initial", 0))),
        "price_open": float(d["price_open"]),
        "sl": float(d.get("sl", 0) or 0),
        "tp": float(d.get("tp", 0) or 0),
        "time_setup": int(d.get("time_setup", 0)),
        "magic": magic,
        "comment": d.get("comment", "") or "",
        "source": "manual" if magic == 0 else "ea",
        "label": "Manual" if magic == 0 else f"EA - {magic}",
    }
