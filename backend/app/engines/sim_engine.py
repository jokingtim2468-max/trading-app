"""Simulated MT5 broker - used automatically on Linux/macOS (no MetaTrader5 wheel) or
when BRIDGE_SIM=1. It mimics the live engine's API, generates realistic random-walk
ticks, fills market/pending orders, triggers SL/TP, and runs two fake Expert
Advisors (magic 555123 and 777001) so EA synchronisation can be tested end-to-end
without a Windows box or a broker account.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from collections import deque

from ..timeframes import Timeframe, resample, ticks_to_tick_bars, ticks_to_time_bars
from .base import (ORDER_TYPES, TradeError, normalize_volume, order_dict, position_dict,
                   validate_pending_price, validate_stops)

log = logging.getLogger("bridge.sim")

SPECS = {
    # name: (start price, digits, per-tick sigma, spread points, contract size, description)
    "EURUSD": (1.0850, 5, 0.00003, 8, 100_000, "Euro vs US Dollar"),
    "GBPUSD": (1.2700, 5, 0.00004, 10, 100_000, "Great Britain Pound vs US Dollar"),
    "USDJPY": (149.50, 3, 0.004, 12, 100_000, "US Dollar vs Japanese Yen"),
    "XAUUSD": (2400.00, 2, 0.18, 25, 100, "Gold vs US Dollar"),
    "BTCUSD": (65000.0, 2, 9.0, 1500, 1, "Bitcoin vs US Dollar"),
    "NAS100": (19800.0, 2, 1.1, 120, 1, "Nasdaq 100 Index CFD"),
    "US500": (5650.0, 2, 0.32, 50, 1, "S&P 500 Index CFD"),
}
HISTORY_DAYS = 12
LIVE_TICK_SECONDS = 3600  # pre-generated tick history so 1s/5s/tick charts open populated
EA_BOTS = [
    {"magic": 555123, "comment": "TrendBot v2", "symbols": ["NAS100", "US500", "XAUUSD"]},
    {"magic": 777001, "comment": "GridScalper", "symbols": ["NAS100"]},
]


class _Book:
    def __init__(self, name: str) -> None:
        price, digits, sigma, spread, contract, desc = SPECS[name]
        self.name, self.digits, self.sigma, self.contract, self.desc = name, digits, sigma, contract, desc
        self.point = 10 ** -digits
        self.spread = spread * self.point
        self.price = price
        self.drift = 0.0
        self.m1: list[dict] = []
        self.ticks: deque[dict] = deque(maxlen=250_000)

    def step(self, scale: float = 1.0) -> float:
        # Random walk with slowly-changing drift and mild mean reversion -> trends + ranges.
        self.drift = 0.995 * self.drift + random.gauss(0, self.sigma * 0.02)
        shock = random.gauss(0, self.sigma) * scale
        self.price = max(self.point, self.price + shock + self.drift * scale)
        return round(self.price, self.digits)

    def add_tick(self, ts: float) -> dict:
        bid = self.step()
        tick = {"time_msc": int(ts * 1000), "bid": bid, "ask": round(bid + self.spread, self.digits),
                "last": 0.0, "volume": 0.0}
        self.ticks.append(tick)
        bucket = int(ts) - int(ts) % 60
        if self.m1 and self.m1[-1]["time"] == bucket:
            b = self.m1[-1]
            b["high"], b["low"], b["close"] = max(b["high"], bid), min(b["low"], bid), bid
            b["volume"] += 1
        else:
            self.m1.append({"time": bucket, "open": bid, "high": bid, "low": bid, "close": bid, "volume": 1})
        return tick

    def seed(self, now: float) -> None:
        start = int(now) - HISTORY_DAYS * 86400
        start -= start % 60
        tick_start = now - LIVE_TICK_SECONDS
        t = start
        while t < tick_start - 60:
            o = round(self.price, self.digits)
            hi = lo = o
            for _ in range(12):
                p = self.step(scale=1.6)
                hi, lo = max(hi, p), min(lo, p)
            c = round(self.price, self.digits)
            self.m1.append({"time": t, "open": o, "high": hi, "low": lo, "close": c,
                            "volume": random.randint(20, 400)})
            t += 60
        ts = tick_start
        while ts < now:
            self.add_tick(ts)
            ts += random.uniform(0.25, 1.4)

    @property
    def bid(self) -> float:
        return self.ticks[-1]["bid"]

    @property
    def ask(self) -> float:
        return self.ticks[-1]["ask"]


class SimEngine:
    sim = True

    def __init__(self, deviation: int = 20) -> None:
        self.connected = False
        self.deviation = deviation
        self.books: dict[str, _Book] = {}
        self._positions: dict[int, dict] = {}
        self._orders: dict[int, dict] = {}
        self._tickets = itertools.count(50_000_001)
        self._tasks: list[asyncio.Task] = []
        self._acct = {"login": 1000001, "server": "SIM-Demo", "name": "Simulator", "company": "TVBridge Sim",
                      "currency": "USD", "balance": 10_000.0, "leverage": 100, "trade_mode": 0,
                      "trade_allowed": True, "account_type": "demo"}

    # ------------------------------------------------------------------ session
    async def login(self, server: str, login: int, password: str, path: str | None = None) -> dict:
        if not self.books:
            now = time.time()
            for name in SPECS:
                book = _Book(name)
                book.seed(now)
                self.books[name] = book
            self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._ea_loop())]
        self._acct.update(login=int(login or 1000001), server=server or "SIM-Demo")
        self.connected = True
        log.info("SIM broker online (%d symbols)", len(self.books))
        return await self.account()

    async def logout(self) -> None:
        self.connected = False

    async def ping(self) -> bool:
        return self.connected

    async def account(self) -> dict | None:
        if not self.connected:
            return None
        self._mark_to_market()
        profit = sum(p["profit"] for p in self._positions.values())
        margin = sum(p["volume"] * self.books[p["symbol"]].contract * self._usd_price(p["symbol"], p["price_current"])
                     / self._acct["leverage"] for p in self._positions.values())
        equity = self._acct["balance"] + profit
        return {**self._acct, "profit": round(profit, 2), "equity": round(equity, 2), "margin": round(margin, 2),
                "margin_free": round(equity - margin, 2),
                "margin_level": round(equity / margin * 100, 2) if margin else 0.0}

    # ------------------------------------------------------------------ symbols / data
    async def find_symbol(self, name: str) -> dict | None:
        if not self.connected:
            return None
        key = name.upper().split(".")[0]
        book = self.books.get(key)
        if book is None:
            return None
        return {"symbol": book.name, "description": book.desc, "digits": book.digits, "point": book.point,
                "tradable": True, "trade_mode": 4, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01, "stops_level": 0, "filling_mode": 2, "contract_size": book.contract,
                "currency_profit": "USD"}

    async def history(self, symbol: str, tf: Timeframe, count: int) -> list[dict]:
        book = self.books[symbol]
        if tf.kind == "tick":
            return ticks_to_tick_bars(list(book.ticks)[-count * tf.ticks:], tf.ticks)[-count:]
        if tf.sub_minute:
            return ticks_to_time_bars(book.ticks, tf.seconds)[-count:]
        m1 = book.m1[-count * (tf.seconds // 60) - 1:] if tf.seconds < 86400 else book.m1
        return [dict(b) for b in resample(m1, tf.seconds)][-count:] if tf.seconds > 60 else [dict(b) for b in m1[-count:]]

    async def ticks_since(self, symbols: list[str], last_msc: dict[str, int]) -> dict[str, list[dict]]:
        out = {}
        for sym in symbols:
            book = self.books.get(sym)
            if book is None or not book.ticks:
                continue
            prev = last_msc.get(sym)
            if prev is None:
                out[sym] = [dict(book.ticks[-1])]
                continue
            fresh = []
            for t in reversed(book.ticks):
                if t["time_msc"] <= prev:
                    break
                fresh.append(dict(t))
            if fresh:
                out[sym] = fresh[::-1]
        return out

    # ------------------------------------------------------------------ positions
    def _usd_price(self, symbol: str, price: float) -> float:
        return 1.0 if symbol.startswith("USD") else price

    def _mark_to_market(self) -> None:
        for p in self._positions.values():
            book = self.books[p["symbol"]]
            cur = book.bid if p["type"] == 0 else book.ask
            diff = (cur - p["price_open"]) * (1 if p["type"] == 0 else -1)
            profit = diff * p["volume"] * book.contract
            if p["symbol"].startswith("USD"):
                profit /= cur
            p["price_current"], p["profit"] = cur, round(profit, 2)

    async def positions(self) -> list[dict]:
        if not self.connected:
            return []
        self._mark_to_market()
        return [position_dict(p) for p in self._positions.values()]

    async def orders(self) -> list[dict]:
        if not self.connected:
            return []
        return [order_dict(o) for o in self._orders.values()]

    # ------------------------------------------------------------------ risk math (Agent 2 sizing)
    async def calc_profit(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float:
        book = self.books[symbol]
        diff = (price_close - price_open) * (1 if side == "buy" else -1)
        profit = diff * volume * book.contract
        return profit / price_close if symbol.startswith("USD") else profit

    async def calc_margin(self, symbol: str, side: str, volume: float, price: float) -> float:
        return volume * self.books[symbol].contract * self._usd_price(symbol, price) / self._acct["leverage"]

    async def tick(self, symbol: str) -> dict:
        book = self.books[symbol]
        return {"bid": book.bid, "ask": book.ask, "time": int(book.ticks[-1]["time_msc"] // 1000)}

    # ------------------------------------------------------------------ trading
    def _open(self, symbol: str, side: str, volume: float, sl: float, tp: float, magic: int, comment: str,
              price: float | None = None) -> dict:
        book = self.books[symbol]
        ticket = next(self._tickets)
        fill = price if price is not None else (book.ask if side == "buy" else book.bid)
        self._positions[ticket] = {"ticket": ticket, "symbol": symbol, "type": 0 if side == "buy" else 1,
                                   "volume": volume, "price_open": fill, "sl": sl, "tp": tp,
                                   "price_current": fill, "profit": 0.0, "swap": 0.0, "time": int(time.time()),
                                   "magic": magic, "comment": comment}
        return {"retcode": 10009, "comment": "Request executed", "order": ticket, "deal": ticket,
                "price": fill, "volume": volume}

    async def place_order(self, req: dict) -> dict:
        info = await self.find_symbol(req["symbol"])
        if info is None:
            raise TradeError(f"unknown symbol {req['symbol']}")
        book = self.books[info["symbol"]]
        side, kind = req["side"], req["kind"]
        volume = normalize_volume(float(req["volume"]), 0.01, 100.0, 0.01)
        sl = round(float(req.get("sl") or 0), book.digits)
        tp = round(float(req.get("tp") or 0), book.digits)
        magic, comment = int(req.get("magic") or 0), (req.get("comment") or "TVBridge")[:31]
        if kind == "market":
            validate_stops(side == "buy", book.bid if side == "buy" else book.ask, sl, tp, 0)
            return self._open(book.name, side, volume, sl, tp, magic, comment)
        price = round(float(req["price"]), book.digits)
        validate_pending_price(side, kind, price, book.bid, book.ask)
        validate_stops(side == "buy", price, sl, tp, 0)
        ticket = next(self._tickets)
        self._orders[ticket] = {"ticket": ticket, "symbol": book.name, "type": ORDER_TYPES[(side, kind)],
                                "volume_current": volume, "price_open": price, "sl": sl, "tp": tp,
                                "time_setup": int(time.time()), "magic": magic, "comment": comment}
        return {"retcode": 10008, "comment": "Order placed", "order": ticket, "deal": 0, "price": price,
                "volume": volume}

    async def modify_position(self, ticket: int, sl: float, tp: float) -> dict:
        p = self._positions.get(ticket)
        if p is None:
            raise TradeError(f"position #{ticket} not found (closed by EA?)")
        book = self.books[p["symbol"]]
        sl, tp = round(sl or 0, book.digits), round(tp or 0, book.digits)
        is_buy = p["type"] == 0
        validate_stops(is_buy, book.bid if is_buy else book.ask, sl, tp, 0)
        p["sl"], p["tp"] = sl, tp
        return {"retcode": 10009, "comment": "Request executed", "order": 0, "deal": 0, "price": 0, "volume": 0}

    async def modify_order(self, ticket: int, price: float, sl: float, tp: float) -> dict:
        o = self._orders.get(ticket)
        if o is None:
            raise TradeError(f"order #{ticket} not found")
        book = self.books[o["symbol"]]
        price = round(price or o["price_open"], book.digits)
        side = "buy" if o["type"] % 2 == 0 else "sell"
        kind = "limit" if o["type"] in (2, 3) else "stop"
        validate_pending_price(side, kind, price, book.bid, book.ask)
        sl, tp = round(sl or 0, book.digits), round(tp or 0, book.digits)
        validate_stops(side == "buy", price, sl, tp, 0)
        o.update(price_open=price, sl=sl, tp=tp)
        return {"retcode": 10009, "comment": "Request executed", "order": ticket, "deal": 0, "price": price,
                "volume": o["volume_current"]}

    async def close_position(self, ticket: int, volume: float | None = None) -> dict:
        p = self._positions.get(ticket)
        if p is None:
            raise TradeError(f"position #{ticket} not found")
        self._mark_to_market()
        vol = p["volume"] if not volume else min(round(volume, 2), p["volume"])
        share = vol / p["volume"]
        realised = round(p["profit"] * share, 2)
        self._acct["balance"] = round(self._acct["balance"] + realised, 2)
        if vol >= p["volume"] - 1e-9:
            del self._positions[ticket]
        else:
            p["volume"] = round(p["volume"] - vol, 2)
        return {"retcode": 10009, "comment": f"Closed, P/L {realised}", "order": ticket, "deal": ticket,
                "price": p["price_current"], "volume": vol}

    async def cancel_order(self, ticket: int) -> dict:
        if self._orders.pop(ticket, None) is None:
            raise TradeError(f"order #{ticket} not found")
        return {"retcode": 10009, "comment": "Order cancelled", "order": ticket, "deal": 0, "price": 0, "volume": 0}

    # ------------------------------------------------------------------ simulation loops
    def _check_triggers(self, book: _Book) -> None:
        for ticket, o in list(self._orders.items()):
            if o["symbol"] != book.name:
                continue
            t, px = o["type"], o["price_open"]
            hit = ((t == 2 and book.ask <= px) or (t == 3 and book.bid >= px) or
                   (t == 4 and book.ask >= px) or (t == 5 and book.bid <= px))
            if hit:
                del self._orders[ticket]
                self._open(book.name, "buy" if t % 2 == 0 else "sell", o["volume_current"], o["sl"], o["tp"],
                           o["magic"], o["comment"], price=px)
        for ticket, p in list(self._positions.items()):
            if p["symbol"] != book.name:
                continue
            is_buy = p["type"] == 0
            px = book.bid if is_buy else book.ask
            sl_hit = p["sl"] and (px <= p["sl"] if is_buy else px >= p["sl"])
            tp_hit = p["tp"] and (px >= p["tp"] if is_buy else px <= p["tp"])
            if sl_hit or tp_hit:
                asyncio.get_running_loop().create_task(self.close_position(ticket))

    async def _tick_loop(self) -> None:
        names = list(self.books)
        while True:
            await asyncio.sleep(random.uniform(0.08, 0.25))
            book = self.books[random.choice(names)]
            book.add_tick(time.time())
            self._check_triggers(book)

    async def _ea_loop(self) -> None:
        """Two toy EAs: a trend follower that trails its stop, and a grid that drops limit orders."""
        while True:
            await asyncio.sleep(1.0)
            if not self.connected:
                continue
            try:
                trend, grid = EA_BOTS
                for sym in trend["symbols"]:
                    book = self.books[sym]
                    mine = [p for p in self._positions.values() if p["magic"] == trend["magic"] and p["symbol"] == sym]
                    risk = book.sigma * 120
                    if not mine and random.random() < 0.04:
                        side = "buy" if book.drift >= 0 else "sell"
                        ref = book.ask if side == "buy" else book.bid
                        sgn = 1 if side == "buy" else -1
                        self._open(sym, side, {"NAS100": 1.0, "US500": 2.0}.get(sym, 0.10),
                                   round(ref - sgn * risk, book.digits), round(ref + sgn * 2 * risk, book.digits),
                                   trend["magic"], trend["comment"])
                    for p in mine:  # trailing stop - the UI must reflect this without a refresh
                        is_buy = p["type"] == 0
                        cur = book.bid if is_buy else book.ask
                        new_sl = round(cur - risk if is_buy else cur + risk, book.digits)
                        if (is_buy and new_sl > p["sl"] + book.sigma * 10) or \
                                (not is_buy and new_sl < p["sl"] - book.sigma * 10):
                            p["sl"] = new_sl
                for sym in grid["symbols"]:
                    book = self.books[sym]
                    pend = [o for o in self._orders.values() if o["magic"] == grid["magic"]]
                    if len(pend) < 2 and random.random() < 0.05:
                        gap = book.sigma * random.randint(40, 90)
                        side = random.choice(["buy", "sell"])
                        px = round(book.bid - gap if side == "buy" else book.ask + gap, book.digits)
                        sgn = 1 if side == "buy" else -1
                        await self.place_order({"symbol": sym, "side": side, "kind": "limit", "volume": 0.5,
                                                "price": px, "sl": px - sgn * gap, "tp": px + sgn * gap * 1.5,
                                                "magic": grid["magic"], "comment": grid["comment"]})
                    for p in [p for p in self._positions.values() if p["magic"] == grid["magic"]]:
                        if time.time() - p["time"] > 240 and random.random() < 0.05:
                            await self.close_position(p["ticket"])
            except (TradeError, KeyError) as exc:  # a toy EA must never kill the loop
                log.debug("sim EA: %s", exc)
            except Exception:  # pragma: no cover
                log.exception("sim EA crashed")
                await asyncio.sleep(5)

