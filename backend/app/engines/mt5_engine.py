"""Live MetaTrader 5 engine.

The MetaTrader5 package talks to a running terminal64.exe over IPC and is *not*
thread-safe, so every call is funnelled through a single-worker executor. All
public methods are coroutines and never block the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Callable

from ..timeframes import Timeframe, resample, ticks_to_tick_bars, ticks_to_time_bars
from .base import (ORDER_TYPES, POSITION_BUY, TradeError, normalize_volume, order_dict,
                   position_dict, validate_pending_price, validate_stops)

log = logging.getLogger("bridge.mt5")

# Bound at startup by bind_module(): either the native Windows `MetaTrader5` package
# or a RemoteMT5 proxy (engines/mt5_remote.py) talking to MT5 under Wine in the mt5 container.
mt5: Any = None


def bind_module(module: Any) -> None:
    global mt5
    mt5 = module


def _local(obj: Any) -> Any:
    """Copy RPyC netrefs (numpy arrays from the Wine container) into local memory once,
    instead of paying a network round-trip per element while iterating."""
    try:
        import rpyc
        if isinstance(obj, rpyc.core.netref.BaseNetref):
            return rpyc.classic.obtain(obj)
    except ImportError:
        pass
    return obj

NATIVE_TF = {
    60: "TIMEFRAME_M1", 120: "TIMEFRAME_M2", 180: "TIMEFRAME_M3", 240: "TIMEFRAME_M4",
    300: "TIMEFRAME_M5", 360: "TIMEFRAME_M6", 600: "TIMEFRAME_M10", 720: "TIMEFRAME_M12",
    900: "TIMEFRAME_M15", 1200: "TIMEFRAME_M20", 1800: "TIMEFRAME_M30", 3600: "TIMEFRAME_H1",
    7200: "TIMEFRAME_H2", 10800: "TIMEFRAME_H3", 14400: "TIMEFRAME_H4", 21600: "TIMEFRAME_H6",
    28800: "TIMEFRAME_H8", 43200: "TIMEFRAME_H12", 86400: "TIMEFRAME_D1",
    604800: "TIMEFRAME_W1", 2592000: "TIMEFRAME_MN1",
}
RETCODE_OK = {10008, 10009, 10010}  # PLACED, DONE, DONE_PARTIAL
MAX_TICKS = 3_000_000


def _ticks_to_dicts(arr: Any) -> list[dict]:
    arr = _local(arr)
    if arr is None:
        return []
    return [{"time_msc": int(t["time_msc"]), "bid": float(t["bid"]), "ask": float(t["ask"]),
             "last": float(t["last"]), "volume": float(t["volume"])} for t in arr]


class MT5Engine:
    sim = False

    def __init__(self, deviation: int = 20, default_path: str = "", portable: bool = False) -> None:
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")
        self.connected = False
        self.deviation = deviation
        self.default_path = default_path
        self.portable = portable

    async def _run(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._ex, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------ session
    async def login(self, server: str, login: int, password: str, path: str | None = None) -> dict:
        def _do() -> dict:
            kwargs = {}
            if path or self.default_path:
                kwargs["path"] = path or self.default_path
            if self.portable:
                kwargs["portable"] = True
            if not mt5.initialize(**kwargs):
                raise TradeError(f"mt5.initialize() failed: {mt5.last_error()}")
            if not mt5.login(int(login), password=password, server=server):
                err = mt5.last_error()
                mt5.shutdown()
                raise TradeError(f"mt5.login() failed: {err}")
            return self._account_sync()

        acct = await self._run(_do)
        self.connected = True
        log.info("MT5 logged in: %s @ %s", acct.get("login"), acct.get("server"))
        return acct

    async def logout(self) -> None:
        await self._run(mt5.shutdown)
        self.connected = False

    def _account_sync(self) -> dict:
        a = mt5.account_info()
        if a is None:
            return {}
        d = a._asdict()
        keys = ("login", "server", "name", "company", "currency", "balance", "equity", "profit",
                "margin", "margin_free", "margin_level", "leverage", "trade_mode", "trade_allowed")
        out = {k: d.get(k) for k in keys}
        out["account_type"] = {0: "demo", 1: "contest", 2: "real"}.get(d.get("trade_mode"), "?")
        return out

    async def account(self) -> dict | None:
        if not self.connected:
            return None
        return await self._run(self._account_sync)

    # ------------------------------------------------------------------ symbols
    def _symbol_sync(self, name: str) -> dict | None:
        info = mt5.symbol_info(name)
        if info is None:
            # Brokers love suffixes (EURUSD.m, EURUSDpro). Try a wildcard match.
            candidates = mt5.symbols_get(f"{name}*") or ()
            exact = [s for s in candidates if s.name.upper().startswith(name.upper())]
            if not exact:
                return None
            info = sorted(exact, key=lambda s: len(s.name))[0]
        if not info.visible:
            mt5.symbol_select(info.name, True)
        return {
            "symbol": info.name,
            "description": info.description,
            "digits": info.digits,
            "point": info.point,
            "tradable": info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED,
            "trade_mode": info.trade_mode,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "stops_level": info.trade_stops_level,
            "filling_mode": info.filling_mode,
            "contract_size": info.trade_contract_size,
            "currency_profit": info.currency_profit,
        }

    async def find_symbol(self, name: str) -> dict | None:
        if not self.connected:
            return None
        return await self._run(self._symbol_sync, name)

    # ------------------------------------------------------------------ data
    def _ticks_back(self, symbol: str, wanted: int, approx_seconds: int) -> list[dict]:
        """Fetch roughly the last `wanted` ticks, widening the lookback window as needed."""
        last = mt5.symbol_info_tick(symbol)
        if last is None:
            return []
        # Tick timestamps are broker-server time; anchor on the last tick, not local now().
        anchor = datetime.fromtimestamp(last.time, tz=timezone.utc)
        window = max(approx_seconds, 600)
        ticks = None
        for _ in range(8):
            ticks = mt5.copy_ticks_range(symbol, anchor - timedelta(seconds=window),
                                         anchor + timedelta(days=1), mt5.COPY_TICKS_ALL)
            if ticks is not None and (len(ticks) >= wanted or window > 14 * 86400):
                break
            window *= 3
        return _ticks_to_dicts(ticks[-min(wanted, MAX_TICKS):] if ticks is not None else None)

    def _rates_sync(self, symbol: str, tf: Timeframe, count: int) -> list[dict]:
        if tf.kind == "tick":
            ticks = self._ticks_back(symbol, count * tf.ticks, count * tf.ticks)
            return ticks_to_tick_bars(ticks, tf.ticks)[-count:]
        if tf.sub_minute:
            ticks = self._ticks_back(symbol, MAX_TICKS, count * tf.seconds)
            return ticks_to_time_bars(ticks, tf.seconds)[-count:]

        base = max(s for s in NATIVE_TF if tf.seconds % s == 0 and s <= tf.seconds)
        factor = tf.seconds // base
        rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, NATIVE_TF[base]), 0,
                                        min(count * factor, 100_000))
        rates = _local(rates)
        if rates is None:
            raise TradeError(f"copy_rates_from_pos failed: {mt5.last_error()}")
        bars = [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]),
                 "volume": float(r["real_volume"] or r["tick_volume"])} for r in rates]
        return (bars if factor == 1 else resample(bars, tf.seconds))[-count:]

    async def history(self, symbol: str, tf: Timeframe, count: int) -> list[dict]:
        return await self._run(self._rates_sync, symbol, tf, count)

    def _ticks_since_sync(self, symbols: list[str], last_msc: dict[str, int]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for sym in symbols:
            prev = last_msc.get(sym)
            if prev is None:
                t = mt5.symbol_info_tick(sym)
                ticks = [] if t is None else [{"time_msc": int(t.time_msc), "bid": t.bid, "ask": t.ask,
                                               "last": t.last, "volume": float(t.volume)}]
            else:
                raw = mt5.copy_ticks_from(sym, prev // 1000, 5000, mt5.COPY_TICKS_ALL)
                ticks = [t for t in _ticks_to_dicts(raw) if t["time_msc"] > prev]
            if ticks:
                out[sym] = ticks
        return out

    async def data_path(self) -> str:
        return await self._run(lambda: mt5.terminal_info().data_path)

    async def ping(self) -> bool:
        """Health probe: terminal reachable and still logged in."""
        def _do() -> bool:
            info = mt5.terminal_info()
            return bool(info and info.connected)
        try:
            return await asyncio.wait_for(self._run(_do), timeout=5)
        except Exception:
            return False

    async def ticks_since(self, symbols: list[str], last_msc: dict[str, int]) -> dict[str, list[dict]]:
        if not self.connected or not symbols:
            return {}
        return await self._run(self._ticks_since_sync, symbols, last_msc)

    # ------------------------------------------------------------------ positions (EA sync)
    async def positions(self) -> list[dict]:
        if not self.connected:
            return []
        rows = await self._run(mt5.positions_get)
        return [position_dict(p) for p in rows or ()]

    async def orders(self) -> list[dict]:
        if not self.connected:
            return []
        rows = await self._run(mt5.orders_get)
        return [order_dict(o) for o in rows or ()]

    # ------------------------------------------------------------------ risk math (Agent 2 sizing)
    async def calc_profit(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float:
        """Account-currency P/L of a hypothetical trade - exact, incl. FX conversion of the quote currency."""
        def _do() -> float:
            action = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
            v = mt5.order_calc_profit(action, symbol, float(volume), float(price_open), float(price_close))
            if v is None:
                raise TradeError(f"order_calc_profit failed: {mt5.last_error()}")
            return float(v)
        return await self._run(_do)

    async def calc_margin(self, symbol: str, side: str, volume: float, price: float) -> float:
        def _do() -> float:
            action = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
            v = mt5.order_calc_margin(action, symbol, float(volume), float(price))
            if v is None:
                raise TradeError(f"order_calc_margin failed: {mt5.last_error()}")
            return float(v)
        return await self._run(_do)

    async def tick(self, symbol: str) -> dict:
        def _do() -> dict:
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                raise TradeError(f"no tick for {symbol}")
            return {"bid": float(t.bid), "ask": float(t.ask), "time": int(t.time)}
        return await self._run(_do)

    # ------------------------------------------------------------------ trading
    @staticmethod
    def _filling(info: dict, pending: bool) -> int:
        if pending:
            return mt5.ORDER_FILLING_RETURN
        fm = info["filling_mode"]
        if fm & 1:  # SYMBOL_FILLING_FOK
            return mt5.ORDER_FILLING_FOK
        if fm & 2:  # SYMBOL_FILLING_IOC
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _send(request: dict) -> dict:
        res = mt5.order_send(request)
        if res is None:
            raise TradeError(f"order_send returned None: {mt5.last_error()}")
        d = res._asdict()
        d.pop("request", None)
        if d["retcode"] not in RETCODE_OK:
            raise TradeError(f"broker rejected request ({d['retcode']}): {d.get('comment')}")
        return {k: d.get(k) for k in ("retcode", "comment", "order", "deal", "price", "volume")}

    def _place_sync(self, req: dict) -> dict:
        info = self._symbol_sync(req["symbol"])
        if info is None:
            raise TradeError(f"unknown MT5 symbol {req['symbol']}")
        if not info["tradable"]:
            raise TradeError(f"{info['symbol']} is not tradable on this account")
        sym = info["symbol"]
        side, kind = req["side"], req["kind"]
        tick = mt5.symbol_info_tick(sym)
        digits, point = info["digits"], info["point"]
        volume = normalize_volume(float(req["volume"]), info["volume_min"], info["volume_max"],
                                  info["volume_step"])
        sl = round(float(req.get("sl") or 0), digits)
        tp = round(float(req.get("tp") or 0), digits)
        min_dist = info["stops_level"] * point
        request: dict[str, Any] = {
            "symbol": sym, "volume": volume, "type": ORDER_TYPES[(side, kind)],
            "sl": sl, "tp": tp, "magic": int(req.get("magic") or 0),
            "comment": (req.get("comment") or "TVBridge")[:31],
            "deviation": self.deviation,
        }
        if kind == "market":
            price = tick.ask if side == "buy" else tick.bid
            validate_stops(side == "buy", tick.bid if side == "buy" else tick.ask, sl, tp, min_dist)
            request.update(action=mt5.TRADE_ACTION_DEAL, price=price,
                           type_filling=self._filling(info, False), type_time=mt5.ORDER_TIME_GTC)
        else:
            price = round(float(req["price"]), digits)
            validate_pending_price(side, kind, price, tick.bid, tick.ask)
            validate_stops(side == "buy", price, sl, tp, min_dist)
            request.update(action=mt5.TRADE_ACTION_PENDING, price=price,
                           type_filling=self._filling(info, True), type_time=mt5.ORDER_TIME_GTC)
        return self._send(request)

    async def place_order(self, req: dict) -> dict:
        return await self._run(self._place_sync, req)

    def _modify_position_sync(self, ticket: int, sl: float, tp: float) -> dict:
        rows = mt5.positions_get(ticket=ticket)
        if not rows:
            raise TradeError(f"position #{ticket} not found (closed by EA?)")
        pos = rows[0]
        info = self._symbol_sync(pos.symbol)
        tick = mt5.symbol_info_tick(pos.symbol)
        digits = info["digits"]
        sl, tp = round(sl or 0, digits), round(tp or 0, digits)
        is_buy = pos.type == POSITION_BUY
        validate_stops(is_buy, tick.bid if is_buy else tick.ask, sl, tp, info["stops_level"] * info["point"])
        # TRADE_ACTION_SLTP updates both levels of the live position atomically.
        return self._send({"action": mt5.TRADE_ACTION_SLTP, "position": ticket,
                           "symbol": pos.symbol, "sl": sl, "tp": tp, "magic": pos.magic})

    async def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        return await self._run(self._modify_position_sync, ticket, sl, tp)

    def _modify_order_sync(self, ticket: int, price: float, sl: float, tp: float) -> dict:
        rows = mt5.orders_get(ticket=ticket)
        if not rows:
            raise TradeError(f"order #{ticket} not found")
        o = rows[0]
        info = self._symbol_sync(o.symbol)
        digits = info["digits"]
        price = round(price or o.price_open, digits)
        sl, tp = round(sl or 0, digits), round(tp or 0, digits)
        validate_stops(o.type % 2 == 0, price, sl, tp, info["stops_level"] * info["point"])
        return self._send({"action": mt5.TRADE_ACTION_MODIFY, "order": ticket, "symbol": o.symbol,
                           "price": price, "sl": sl, "tp": tp, "type_time": o.type_time,
                           "expiration": o.time_expiration})

    async def modify_order(self, ticket: int, price: float, sl: float, tp: float) -> dict:
        return await self._run(self._modify_order_sync, ticket, price, sl, tp)

    def _close_sync(self, ticket: int, volume: float | None) -> dict:
        rows = mt5.positions_get(ticket=ticket)
        if not rows:
            raise TradeError(f"position #{ticket} not found")
        pos = rows[0]
        info = self._symbol_sync(pos.symbol)
        tick = mt5.symbol_info_tick(pos.symbol)
        is_buy = pos.type == POSITION_BUY
        vol = pos.volume if not volume else normalize_volume(min(volume, pos.volume), info["volume_min"],
                                                             pos.volume, info["volume_step"])
        return self._send({
            "action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": pos.symbol,
            "volume": vol, "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_buy else tick.ask, "deviation": self.deviation,
            "magic": pos.magic, "comment": "TVBridge close",
            "type_filling": self._filling(info, False), "type_time": mt5.ORDER_TIME_GTC,
        })

    async def close_position(self, ticket: int, volume: float | None = None) -> dict:
        return await self._run(self._close_sync, ticket, volume)

    async def cancel_order(self, ticket: int) -> dict:
        return await self._run(self._send, {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
