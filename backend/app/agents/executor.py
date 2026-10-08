"""Agent 2 - Execution & Risk Manager.

The only component allowed to send orders on behalf of the agents, and an absolute
firewall: every proposal is re-validated and re-sized against the FTMO 10k rules, and a
1-second guardian closes *all* positions on the account (agent, EA and manual alike -
FTMO limits are account-level) when a limit is approached.

Demo Mode: orders go through exactly the same code path, latency and MT5 calls; the only
difference is the account gate - in Demo Mode the connected MT5 account must report
trade_mode == demo, otherwise nothing is sent.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..engines.base import TradeError
from .analyst import Proposal, is_finite
from .ftmo import FTMO_10K, RiskGuard, RiskRejected, SizedOrder, prague_day

log = logging.getLogger("bridge.agents.executor")

AGENT_MAGIC = 880001
AGENT_COMMENT = "AI-Agent"


class ExecutionRiskManager:
    def __init__(self, engine: Any, guard: RiskGuard,
                 on_event: Callable[[str, str], Awaitable[None]] | None = None) -> None:
        self.engine = engine
        self.guard = guard
        self.on_event = on_event
        self.demo_mode = True
        self.live_confirmed = False
        self.risk_per_trade = 50.0          # USD; clamped by FTMO_10K.max_risk_per_trade
        self._flattening = asyncio.Lock()
        self._seeded = False
        self.last_snapshot: dict = {}

    async def _emit(self, kind: str, msg: str) -> None:
        log.info("[agent2] %s: %s", kind, msg)
        if self.on_event:
            await self.on_event(kind, msg)

    # ------------------------------------------------------------------ connection
    async def connect_mt5(self, server: str, login: int, password: str, path: str | None = None) -> dict:
        """Secure connection to the terminal via the MetaTrader5 library (mt5.initialize + mt5.login).
        Credentials are passed straight through and never stored or logged."""
        return await self.engine.login(server, int(login), password, path)

    async def account_gate(self) -> tuple[bool, str]:
        acct = await self.engine.account()
        if not acct:
            return False, "MT5 not connected"
        kind = acct.get("account_type")
        if self.demo_mode and kind != "demo":
            return False, f"Demo Mode is on but the connected account is '{kind}' - refusing to trade"
        if not self.demo_mode:
            if kind == "demo":
                return False, "Live mode selected but the connected account is a demo account"
            if not self.live_confirmed:
                return False, "Live mode requires explicit confirmation in the agent account panel"
        if acct.get("trade_allowed") is False:
            return False, "trading is disabled for this account (AutoTrading / investor password?)"
        return True, ""

    # ------------------------------------------------------------------ state
    async def _state(self) -> tuple[dict, list[dict], list[dict]]:
        acct, positions, orders = await asyncio.gather(self.engine.account(), self.engine.positions(),
                                                       self.engine.orders())
        return acct or {}, positions, orders

    async def open_risk(self, positions: list[dict]) -> float:
        """Sum of what every open position loses if its SL is hit (current loss for no-SL positions)."""
        total = 0.0
        for p in positions:
            if p["sl"]:
                loss = -await self.engine.calc_profit(p["symbol"], p["side"], p["volume"], p["price_current"], p["sl"])
                total += max(loss, 0.0)
            else:
                total += max(-p["profit"], 0.0)  # no SL: count the loss already on the books
        return total

    async def verify_daily_loss_limit(self) -> tuple[bool, float, float]:
        acct, _, _ = await self._state()
        return self.guard.verify_daily_loss_limit(acct.get("equity", 0.0))

    async def verify_overall_loss_limit(self) -> tuple[bool, float]:
        acct, _, _ = await self._state()
        return self.guard.verify_overall_loss_limit(acct.get("equity", 0.0))

    # ------------------------------------------------------------------ guardian (every second)
    async def enforce(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        acct, positions, orders = await self._state()
        if not acct:
            return {}
        tickets = [p["ticket"] for p in positions]
        if not self._seeded:  # positions that pre-date this process are not "trades executed today"
            self.guard.state.seen_tickets = list(dict.fromkeys(self.guard.state.seen_tickets + tickets))
            self._seeded = True
        self.guard.record_executions(now, tickets)
        decision = self.guard.evaluate(now, acct["balance"], acct["equity"], bool(positions or orders))
        if decision.action in ("flatten_lock_day", "flatten_halt", "flatten_weekend", "target_halt"):
            await self.close_all(decision.reason, positions, orders)
            acct, positions, orders = await self._state()
        open_risk = await self.open_risk(positions)
        self.last_snapshot = self.guard.snapshot(now, acct.get("balance", 0.0), acct.get("equity", 0.0), open_risk)
        self.last_snapshot.update(account_type=acct.get("account_type"), login=acct.get("login"),
                                  server=acct.get("server"), demo_mode=self.demo_mode,
                                  agent_positions=[p for p in positions if p["magic"] == AGENT_MAGIC])
        return self.last_snapshot

    async def close_all(self, reason: str, positions: list[dict] | None = None,
                        orders: list[dict] | None = None) -> None:
        async with self._flattening:
            if positions is None or orders is None:
                _, positions, orders = await self._state()
            if not positions and not orders:
                return
            await self._emit("flatten", f"Closing {len(positions)} position(s), cancelling {len(orders)} order(s): {reason}")
            for o in orders:
                try:
                    await self.engine.cancel_order(o["ticket"])
                except TradeError as exc:
                    await self._emit("error", f"cancel #{o['ticket']} failed: {exc}")
            for attempt in range(3):  # a requote / market-closed retcode must not leave risk open
                failures = 0
                for p in positions:
                    try:
                        await self.engine.close_position(p["ticket"])
                    except TradeError as exc:
                        failures += 1
                        await self._emit("error", f"close #{p['ticket']} failed (try {attempt + 1}): {exc}")
                if not failures:
                    break
                await asyncio.sleep(0.5)
                _, positions, _ = await self._state()

    # ------------------------------------------------------------------ proposals -> orders
    async def size(self, proposal: Proposal) -> SizedOrder:
        if not is_finite(proposal.entry, proposal.stop_loss, proposal.take_profit):
            raise RiskRejected("non-numeric prices")
        info = await self.engine.find_symbol(proposal.symbol)
        if info is None or not info.get("tradable"):
            raise RiskRejected(f"{proposal.symbol} is not tradable on the MT5 account")
        acct, positions, _ = await self._state()
        tick = await self.engine.tick(info["symbol"])
        side = proposal.direction
        market = tick["ask"] if side == "buy" else tick["bid"]
        kind = proposal.order_type
        entry = proposal.entry
        if kind == "market":
            # Re-anchor to the live price; the analyst's entry may be a few seconds stale.
            shift = market - entry
            entry = market
            stop_dist = abs(proposal.entry - proposal.stop_loss)
            if abs(shift) > 0.25 * stop_dist:
                raise RiskRejected(f"price moved {shift:+.2f} since the proposal (> 25% of stop distance)")
        else:
            kind = "limit" if (side == "buy" and entry < market) or (side == "sell" and entry > market) else "stop"
        sl, tp = proposal.stop_loss, proposal.take_profit
        min_dist = info.get("stops_level", 0) * info.get("point", 0)
        if abs(entry - sl) < min_dist or abs(tp - entry) < min_dist:
            raise RiskRejected("SL/TP inside the broker's stops level")
        loss = -await self.engine.calc_profit(info["symbol"], side, 1.0, entry, sl)
        gain = await self.engine.calc_profit(info["symbol"], side, 1.0, entry, tp)
        margin = await self.engine.calc_margin(info["symbol"], side, 1.0, entry)
        if any(p["symbol"] == info["symbol"] and p["magic"] == AGENT_MAGIC for p in positions):
            raise RiskRejected(f"agent already has a position on {info['symbol']}")
        return self.guard.size_order(
            symbol=info["symbol"], side=side, entry=entry, sl=sl, tp=tp, market_price=market,
            loss_per_lot=loss, profit_per_lot=gain, margin_per_lot=margin,
            volume_min=info["volume_min"], volume_max=info["volume_max"], volume_step=info["volume_step"],
            equity=acct.get("equity", 0.0), margin_free=acct.get("margin_free", 0.0),
            open_risk=await self.open_risk(positions), open_positions=len(positions),
            risk_per_trade=min(self.risk_per_trade, FTMO_10K.max_risk_per_trade), kind=kind)

    async def execute_mt5_order(self, proposal: Proposal, now: datetime | None = None) -> dict:
        """Validate -> size -> send. Returns {status: executed|rejected, ...}; never raises."""
        now = now or datetime.now(timezone.utc)
        ok, why = await self.account_gate()
        if not ok:
            await self._emit("reject", f"{proposal.symbol} {proposal.direction}: {why}")
            return {"status": "rejected", "reason": why}
        allowed, why = self.guard.can_open(now)
        if not allowed:
            await self._emit("reject", f"{proposal.symbol} {proposal.direction}: {why}")
            return {"status": "rejected", "reason": why}
        try:
            order = await self.size(proposal)
        except RiskRejected as exc:
            await self._emit("reject", f"{proposal.symbol} {proposal.direction}: {exc}")
            return {"status": "rejected", "reason": str(exc)}
        try:
            res = await self.engine.place_order({
                "symbol": order.symbol, "side": order.side, "kind": order.kind, "volume": order.volume,
                "price": order.price, "sl": order.sl, "tp": order.tp, "magic": AGENT_MAGIC,
                "comment": f"{AGENT_COMMENT} {'demo' if self.demo_mode else 'live'}"})
        except TradeError as exc:
            await self._emit("reject", f"broker rejected {order.symbol}: {exc}")
            return {"status": "rejected", "reason": str(exc)}
        self.guard.record_executions(now, [res.get("order") or 0])
        await self._emit("execute", f"{order.side.upper()} {order.volume} {order.symbol} {order.kind} @ {order.price} "
                                    f"SL {order.sl} TP {order.tp} - risk ${order.risk_usd} / reward ${order.reward_usd} "
                                    f"(#{res.get('order')})")
        return {"status": "executed", "order": order.__dict__, "result": res}

    async def complete_trading_day(self, symbol: str, now: datetime | None = None) -> None:
        """After the target is hit, FTMO still needs 4 trading days: open and close one
        minimum-lot trade (risk <= $10) on each missing day."""
        now = now or datetime.now(timezone.utc)
        if not self.guard.needs_daycount_trade(now):
            return
        ok, why = await self.account_gate()
        if not ok:
            return
        info = await self.engine.find_symbol(symbol)
        tick = await self.engine.tick(info["symbol"])
        loss_per_point = -await self.engine.calc_profit(info["symbol"], "buy", info["volume_min"], tick["ask"],
                                                        tick["ask"] - 1)
        dist = FTMO_10K.daycount_trade_risk / max(loss_per_point, 1e-9)
        res = await self.engine.place_order({"symbol": info["symbol"], "side": "buy", "kind": "market",
                                             "volume": info["volume_min"], "sl": round(tick["bid"] - dist, info["digits"]),
                                             "tp": 0, "magic": AGENT_MAGIC, "comment": "AI-Agent day-count"})
        self.guard.record_executions(now, [res.get("order") or 0])
        await self._emit("execute", f"Trading-day top-up on {prague_day(now)}: min lot {info['symbol']} (#{res.get('order')})")
        await asyncio.sleep(5)
        try:
            await self.engine.close_position(res["order"])
        except TradeError:
            pass
