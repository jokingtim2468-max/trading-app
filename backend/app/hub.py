"""Connection hub: per-client chart subscriptions, dual-feed routing, the MT5 tick
stream, EA position synchronisation and health monitoring."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

from .config import settings
from .engines.base import BrokerEngine
from .external import ExternalFeed
from .timeframes import Timeframe

log = logging.getLogger("bridge.hub")


@dataclass
class Sub:
    chart_id: str
    symbol: str          # MT5 symbol name, or the user's external symbol
    source: str          # "mt5" | "external"
    tf: Timeframe
    provider: str = ""   # external provider that served history
    native: str = ""     # provider-native symbol


@dataclass(eq=False)
class Client:
    ws: WebSocket
    subs: dict[str, Sub] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, msg: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(msg, separators=(",", ":")))


class Hub:
    def __init__(self, engine: BrokerEngine, feed: ExternalFeed) -> None:
        self.engine = engine
        self.feed = feed
        self.clients: set[Client] = set()
        self.last_msc: dict[str, int] = {}
        self._positions_sig = ""
        self._prev_positions: dict[int, dict] = {}
        self._prev_orders: dict[int, dict] = {}
        self.mt5_health = {"status": "starting", "error": ""}
        self.tv_account: dict = {}
        self._tasks: list[asyncio.Task] = []
        self.agents: Any = None  # AgentSystem, attached in main.lifespan

    # ------------------------------------------------------------------ plumbing
    def start(self) -> None:
        for coro in (self._tick_loop(), self._positions_loop(), self._external_loop(), self._health_loop()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    async def broadcast(self, msg: dict, only: set[Client] | None = None) -> None:
        targets = only if only is not None else self.clients
        dead = []
        for c in list(targets):
            try:
                await c.send(msg)
            except Exception:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)

    async def notify(self, level: str, message: str, key: str = "") -> None:
        await self.broadcast({"type": "notify", "level": level, "message": message, "key": key})

    def status(self) -> dict:
        return {"type": "status", "pinned": ["NAS100", "US500", "XAUUSD"],
                "mt5": {"connected": self.engine.connected, "sim": self.engine.sim, **self.mt5_health,
                        "mode": "sim" if self.engine.sim else ("docker" if settings.mt5_rpyc_host else "native")},
                "tv": {"mode": "anonymous", **self.tv_account},
                "external": self.feed.snapshot()}

    async def push_status(self) -> None:
        await self.broadcast(self.status())

    def startup_alerts(self) -> list[dict]:
        """Notifications a freshly connected UI should see immediately."""
        out = []
        if self.mt5_health["status"] == "down":
            out.append({"type": "notify", "level": "error", "key": "mt5",
                        "message": f"MetaTrader 5 is down: {self.mt5_health['error'] or 'not connected'}"})
        snap = self.feed.snapshot()
        if snap["gave_up"]:
            out.append({"type": "notify", "level": "error", "key": "external",
                        "message": "TradingView and all 4 fallback data sources are unavailable. "
                                   "External symbols are disabled until you press Retry."})
        else:
            tv = snap["providers"][0]
            if tv["status"] in ("down", "unavailable"):
                alive = [p["label"] for p in snap["providers"] if p["status"] == "ok"]
                out.append({"type": "notify", "level": "warn", "key": "tv",
                            "message": f"TradingView feed is down ({tv['last_error'] or tv['status']}). "
                                       f"Using fallback: {', '.join(alive) or 'none'}"})
        return out

    # ------------------------------------------------------------------ loops
    async def _tick_loop(self) -> None:
        """Stream every MT5 tick (not just snapshots) for symbols someone is charting."""
        while True:
            await asyncio.sleep(settings.tick_poll_s)
            symbols = {s.symbol for c in self.clients for s in c.subs.values() if s.source == "mt5"}
            if not symbols or not self.engine.connected:
                continue
            try:
                fresh = await self.engine.ticks_since(sorted(symbols), self.last_msc)
            except Exception as exc:
                log.warning("tick poll failed: %s", exc)
                await asyncio.sleep(1)
                continue
            for sym, ticks in fresh.items():
                self.last_msc[sym] = ticks[-1]["time_msc"]
                msg = {"type": "ticks", "symbol": sym,
                       "ticks": [[t["time_msc"], t["bid"], t["ask"], t["last"]] for t in ticks]}
                await self.broadcast(msg, {c for c in self.clients
                                           if any(s.symbol == sym and s.source == "mt5" for s in c.subs.values())})

    async def _positions_loop(self) -> None:
        """MT5 is the single source of truth: poll positions/orders and push on any change,
        so EA opens/modifies/closes appear in the UI within one poll interval."""
        while True:
            await asyncio.sleep(settings.positions_poll_s)
            if not self.engine.connected or not self.clients:
                continue
            try:
                positions, orders, account = await asyncio.gather(
                    self.engine.positions(), self.engine.orders(), self.engine.account())
            except Exception as exc:
                log.warning("positions poll failed: %s", exc)
                await asyncio.sleep(1)
                continue
            payload = {"type": "positions", "positions": positions, "orders": orders, "account": account}
            sig = json.dumps(payload, sort_keys=True)
            if sig == self._positions_sig:
                continue
            self._positions_sig = sig
            await self.broadcast(payload)
            await self._emit_trade_events(positions, orders)

    async def _emit_trade_events(self, positions: list[dict], orders: list[dict]) -> None:
        now_p = {p["ticket"]: p for p in positions}
        now_o = {o["ticket"]: o for o in orders}
        first_run = not self._prev_positions and not self._prev_orders and not hasattr(self, "_seeded")
        self._seeded = True
        events = []
        if not first_run:
            for t, p in now_p.items():
                old = self._prev_positions.get(t)
                desc = f"{p['label']} {p['side'].upper()} {p['volume']} {p['symbol']}"
                if old is None:
                    events.append(f"{desc} opened @ {p['price_open']}")
                elif (old["sl"], old["tp"]) != (p["sl"], p["tp"]):
                    events.append(f"{desc} #{t} SL {old['sl']}→{p['sl']} TP {old['tp']}→{p['tp']}")
            for t, p in self._prev_positions.items():
                if t not in now_p:
                    events.append(f"{p['label']} closed {p['side'].upper()} {p['volume']} {p['symbol']} "
                                  f"(last P/L {p['profit']})")
            for t, o in now_o.items():
                if t not in self._prev_orders:
                    events.append(f"{o['label']} placed {o['type_name']} {o['volume']} {o['symbol']} @ {o['price_open']}")
        self._prev_positions, self._prev_orders = now_p, now_o
        for e in events:
            await self.broadcast({"type": "trade_event", "message": e, "time": time.time()})

    async def _external_loop(self) -> None:
        """Poll the latest bar for external charts. Stops entirely once the feed has given up."""
        while True:
            await asyncio.sleep(settings.external_poll_s)
            if self.feed.gave_up:
                continue
            groups: dict[tuple, list[tuple[Client, Sub]]] = {}
            for c in self.clients:
                for s in c.subs.values():
                    if s.source == "external":
                        groups.setdefault((s.symbol, s.tf.label, s.provider), []).append((c, s))
            for (symbol, _, provider), members in groups.items():
                tf = members[0][1].tf
                try:
                    used, _, bars = await self.feed.history(symbol, tf, 2, prefer=provider)
                except Exception as exc:
                    log.debug("external poll %s: %s", symbol, exc)
                    continue
                for c, s in members:
                    if used != s.provider:
                        s.provider = used
                    try:
                        await c.send({"type": "bar", "chartId": s.chart_id, "bars": bars, "provider": used})
                    except Exception:
                        pass
            if self.feed.gave_up:
                await self.notify("error", "All external data sources stopped responding. "
                                           "No further attempts will be made until you press Retry.", "external")
                await self.push_status()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.health_interval_s)
            if self.mt5_health["status"] in ("starting", "disconnected"):
                continue
            ok = await self.engine.ping()
            new = "ok" if ok else "down"
            if new != self.mt5_health["status"]:
                self.mt5_health = {"status": new, "error": "" if ok else "terminal not responding / lost connection"}
                await self.notify("info" if ok else "error",
                                  "MetaTrader 5 connection restored" if ok
                                  else "MetaTrader 5 is down - trading disabled until it reconnects", "mt5")
                await self.push_status()

    # ------------------------------------------------------------------ routing
    async def resolve(self, symbol: str) -> dict[str, Any]:
        """MT5 first (tradable); otherwise external charting-only."""
        raw = symbol.strip()
        explicit_exchange = ":" in raw and not raw.upper().startswith("MT5:")
        name = raw.split(":", 1)[1] if raw.upper().startswith("MT5:") else raw
        if self.engine.connected and not explicit_exchange:
            info = await self.engine.find_symbol(name)
            if info:
                return {**info, "source": "mt5"}
        return {"symbol": raw.upper(), "source": "external", "tradable": False, "description": "",
                "digits": None}
