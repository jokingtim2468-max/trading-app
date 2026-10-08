"""Multi-agent orchestration: Agent 1 (analyst) -> proposals -> Agent 2 (risk/execution).

Two independent loops so the firewall never waits on analysis:
  * guardian  - every second: FTMO checks, flatten/lock/halt, metrics push to the UI.
  * analysis  - every `interval` seconds: Agent 1 analyses each symbol, Agent 2 decides.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import _secret
from .analyst import ClaudeAnalyst, Proposal, RuleAnalyst
from .executor import ExecutionRiskManager
from .ftmo import FTMO_10K, RiskGuard, StateStore
from .tools import AnalystTools

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("bridge.agents")
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", Path(__file__).resolve().parents[2] / "state"))


@dataclass
class AgentConfig:
    symbols: list[str] = field(default_factory=lambda: ["NAS100", "US500", "XAUUSD"])
    interval_s: int = 300
    analyst: str = "auto"            # auto | claude | rules
    effort: str = "medium"           # Claude effort for Agent 1
    risk_per_trade: float = 50.0     # USD, hard-capped at $100 by Agent 2
    demo_mode: bool = True
    guardian_always_on: bool = False  # enforce FTMO limits even while agents are stopped


class AgentSystem:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.config = AgentConfig()
        self.guard = RiskGuard(StateStore(STATE_DIR / "ftmo_state.json"))
        self.executor = ExecutionRiskManager(hub.engine, self.guard, on_event=self._event)
        self.tools = AnalystTools(hub, lambda: self.executor.last_snapshot)
        self.running = False
        self.activity: list[dict] = []
        self.proposals: list[dict] = []
        self.next_analysis_at = 0.0
        self._analysis_task: asyncio.Task | None = None
        self._guardian_task: asyncio.Task | None = None
        self._busy = asyncio.Lock()

    # ------------------------------------------------------------------ events -> UI
    async def _event(self, kind: str, message: str) -> None:
        ev = {"time": time.time(), "kind": kind, "message": message}
        self.activity = (self.activity + [ev])[-300:]
        await self.hub.broadcast({"type": "agent_event", **ev})
        if kind in ("flatten", "halt", "lock"):
            await self.hub.notify("error", f"AI Risk Manager: {message}", "agents")

    def api_key(self) -> str:
        return _secret("ANTHROPIC_API_KEY")

    def analyst(self):
        mode = self.config.analyst
        if mode == "claude" or (mode == "auto" and self.api_key()):
            try:
                return ClaudeAnalyst(self.tools, self.api_key(), self.config.effort, on_event=self._event)
            except ImportError:
                log.warning("anthropic SDK missing - falling back to rule-based analyst")
        return RuleAnalyst(self.tools)

    def status(self) -> dict:
        return {"type": "agents", "running": self.running, "config": asdict(self.config),
                "analyst": "claude" if (self.config.analyst == "claude" or
                                        (self.config.analyst == "auto" and self.api_key())) else "rules",
                "risk": self.executor.last_snapshot, "proposals": self.proposals[-30:],
                "activity": self.activity[-60:], "next_analysis_at": self.next_analysis_at,
                "magic": 880001}

    # ------------------------------------------------------------------ lifecycle
    def start_guardian(self) -> None:
        if self._guardian_task is None:
            self._guardian_task = asyncio.create_task(self._guardian_loop())

    async def start(self) -> dict:
        if self.running:
            return self.status()
        ok, why = await self.executor.account_gate()
        if not ok:
            raise ValueError(why)
        allowed, why = self.guard.can_open(datetime.now(timezone.utc))
        self.running = True
        self.next_analysis_at = time.time()
        self._analysis_task = asyncio.create_task(self._analysis_loop())
        await self._event("system", f"Agents started ({'DEMO' if self.config.demo_mode else 'LIVE'}) - "
                                    f"analyst={self.status()['analyst']}, symbols={', '.join(self.config.symbols)}"
                          + ("" if allowed else f" - entries currently blocked: {why}"))
        return self.status()

    async def stop(self, reason: str = "stopped by user") -> dict:
        self.running = False
        if self._analysis_task:
            self._analysis_task.cancel()
            self._analysis_task = None
        await self._event("system", f"Agents stopped: {reason}")
        return self.status()

    def update_config(self, patch: dict) -> dict:
        allowed = AgentConfig.__dataclass_fields__
        for k, v in patch.items():
            if k not in allowed:
                continue
            if k == "risk_per_trade":
                v = max(5.0, min(float(v), FTMO_10K.max_risk_per_trade))
            if k == "interval_s":
                v = max(60, int(v))
            if k == "symbols":
                v = [str(s).upper() for s in v][:6]
            setattr(self.config, k, v)
        self.executor.demo_mode = self.config.demo_mode
        self.executor.risk_per_trade = self.config.risk_per_trade
        if "demo_mode" in patch:
            self.executor.live_confirmed = bool(patch.get("live_confirmed")) and not self.config.demo_mode
        return self.status()

    # ------------------------------------------------------------------ loops
    async def _guardian_loop(self) -> None:
        last_push = 0.0
        while True:
            await asyncio.sleep(1.0)
            if not self.hub.engine.connected:
                continue
            try:
                if self.running or self.config.guardian_always_on:
                    await self.executor.enforce()
                    st = self.guard.state
                    if self.running and (st.halted or (st.target_reached and not self.guard.needs_daycount_trade(
                            datetime.now(timezone.utc)))):
                        await self.stop(st.halt_reason or "profit target reached")
                elif time.time() - last_push > 5:
                    await self._refresh_snapshot()
            except Exception as exc:  # the guardian must survive anything
                log.exception("guardian error")
                await self._event("error", f"guardian: {exc}")
                await asyncio.sleep(2)
            if time.time() - last_push > 2:
                last_push = time.time()
                await self.hub.broadcast(self.status())

    async def _refresh_snapshot(self) -> None:
        acct = await self.hub.engine.account()
        if acct:
            positions = await self.hub.engine.positions()
            now = datetime.now(timezone.utc)
            self.guard.roll_day(now, acct["balance"])
            self.executor.last_snapshot = {
                **self.guard.snapshot(now, acct["balance"], acct["equity"], await self.executor.open_risk(positions)),
                "account_type": acct.get("account_type"), "login": acct.get("login"), "server": acct.get("server"),
                "demo_mode": self.config.demo_mode, "agent_positions": [p for p in positions if p["magic"] == 880001]}

    async def _analysis_loop(self) -> None:
        while self.running:
            if time.time() >= self.next_analysis_at:
                await self.run_cycle()
                self.next_analysis_at = time.time() + self.config.interval_s
            await asyncio.sleep(1)

    async def run_cycle(self) -> list[dict]:
        """One pass: Agent 1 analyses every symbol, Agent 2 rules on each proposal."""
        async with self._busy:
            results = []
            now = datetime.now(timezone.utc)
            if self.guard.needs_daycount_trade(now):
                await self.executor.complete_trading_day(self.config.symbols[0], now)
                return results
            allowed, why = self.guard.can_open(now)
            if not allowed:
                await self._event("system", f"analysis skipped: {why}")
                return results
            analyst = self.analyst()
            for sym in self.config.symbols:
                try:
                    proposals: list[Proposal] = await analyst.analyze(sym)
                except Exception as exc:
                    await self._event("error", f"analyst failed on {sym}: {exc}")
                    continue
                if not proposals:
                    await self._event("analyst", f"{sym}: no setup")
                for p in proposals:
                    await self._event("proposal", f"{p.symbol} {p.direction.upper()} {p.order_type} @ {p.entry} "
                                                  f"SL {p.stop_loss} TP {p.take_profit} ({p.analyst}, "
                                                  f"conf {p.confidence:.2f}) - {p.rationale[:240]}")
                    decision = await self.executor.execute_mt5_order(p)
                    rec = {**p.to_dict(), "decision": decision["status"], "reason": decision.get("reason", ""),
                           "order": decision.get("order")}
                    self.proposals = (self.proposals + [rec])[-100:]
                    results.append(rec)
            return results

    def new_challenge(self, phase: int) -> dict:
        acct_balance = self.executor.last_snapshot.get("balance") or FTMO_10K.initial_balance
        self.guard.new_challenge(phase, acct_balance, datetime.now(timezone.utc))
        return self.status()
