"""FTMO 2-Step *Standard* evaluation, $10,000 account - hardcoded risk guardrails.

Everything here is pure logic (no I/O except the JSON state file) so it can be unit
tested with a fake clock. Agent 2 (executor.py) calls into this module before every
order and once per second to decide whether to flatten, lock or halt.

Rule references (FTMO Standard, 10k):
  * Max Daily Loss 5%  = $500, measured against the balance at midnight CE(S)T
    (Europe/Prague), floating P/L included. We flatten + lock at -$480.
  * Max Loss 10%       = $1,000 -> equity must never touch $9,000. We flatten + halt at $9,020.
  * Profit Target      = Phase 1: 10% ($1,000), Phase 2: 5% ($500). Halt when reached.
  * Min Trading Days   = 4 distinct days with at least one executed trade.
  * Standard account   = no weekend holding -> flatten before the Friday close.
  * Trading Period     = unlimited. Leverage 1:100.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")      # FTMO day boundary (midnight CE(S)T)
NEW_YORK = ZoneInfo("America/New_York")  # FX/CFD weekly close: Friday 17:00 New York


# ---------------------------------------------------------------------------- hard limits
@dataclass(frozen=True)
class FTMORules:
    initial_balance: float = 10_000.0
    leverage: int = 100
    max_daily_loss: float = 500.0          # 5 %
    daily_loss_trigger: float = 480.0      # flatten + lock the day before the hard limit
    max_overall_loss: float = 1_000.0      # 10 % -> equity floor $9,000
    overall_loss_buffer: float = 20.0      # flatten + halt at equity $9,020
    phase_targets: tuple[tuple[int, float], ...] = ((1, 1_000.0), (2, 500.0))
    min_trading_days: int = 4
    # Weekend: flatten Friday 16:30 NY (30 min before the 17:00 weekly close), no new
    # entries from 15:45 NY Friday until the Sunday 18:05 NY re-open.
    weekend_flatten_ny: tuple[int, int, int] = (4, 16, 30)     # weekday (Mon=0), hour, minute
    weekend_no_entry_ny: tuple[int, int, int] = (4, 15, 45)
    weekend_reopen_ny: tuple[int, int, int] = (6, 18, 5)
    # Agent-2 sizing limits (inside the FTMO limits, never configurable above these).
    max_risk_per_trade: float = 100.0      # 1 % of initial balance, hard cap
    max_open_risk: float = 200.0           # sum of distance-to-SL across all positions
    max_open_positions: int = 3
    min_reward_risk: float = 1.0
    margin_usage_cap: float = 0.5          # never use more than 50 % of free margin
    daycount_trade_risk: float = 10.0      # risk of a "trading-day" top-up trade

    @property
    def equity_floor(self) -> float:
        return self.initial_balance - self.max_overall_loss

    def target(self, phase: int) -> float:
        return dict(self.phase_targets).get(phase, 0.0)


FTMO_10K = FTMORules()


# ---------------------------------------------------------------------------- time helpers
def prague_day(now: datetime) -> str:
    return now.astimezone(PRAGUE).strftime("%Y-%m-%d")


def _ny_minutes(now: datetime) -> tuple[int, int]:
    ny = now.astimezone(NEW_YORK)
    return ny.weekday(), ny.hour * 60 + ny.minute


def _after(now: datetime, spec: tuple[int, int, int]) -> bool:
    wd, mins = _ny_minutes(now)
    return (wd, mins) >= (spec[0], spec[1] * 60 + spec[2])


def in_weekend_window(now: datetime, rules: FTMORules = FTMO_10K) -> bool:
    """True from Friday's flatten time until the Sunday re-open (New York clock)."""
    wd, mins = _ny_minutes(now)
    if wd == 5:
        return True
    if wd == 4:
        return _after(now, rules.weekend_flatten_ny)
    if wd == 6:
        return not _after(now, rules.weekend_reopen_ny)
    return False


def entries_blocked_for_weekend(now: datetime, rules: FTMORules = FTMO_10K) -> bool:
    wd, _ = _ny_minutes(now)
    return in_weekend_window(now, rules) or (wd == 4 and _after(now, rules.weekend_no_entry_ny))


def next_weekend_flatten(now: datetime, rules: FTMORules = FTMO_10K) -> datetime:
    ny = now.astimezone(NEW_YORK)
    days = (rules.weekend_flatten_ny[0] - ny.weekday()) % 7
    t = (ny + timedelta(days=days)).replace(hour=rules.weekend_flatten_ny[1], minute=rules.weekend_flatten_ny[2],
                                           second=0, microsecond=0)
    if t <= ny:
        t += timedelta(days=7)
    return t.astimezone(timezone.utc)


def next_prague_midnight(now: datetime) -> datetime:
    p = now.astimezone(PRAGUE)
    return (p + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


# ---------------------------------------------------------------------------- state
@dataclass
class RiskState:
    phase: int = 1
    day_key: str = ""
    day_start_balance: float = FTMO_10K.initial_balance
    trading_days: list[str] = field(default_factory=list)
    seen_tickets: list[int] = field(default_factory=list)
    daily_locked: bool = False
    halted: bool = False
    halt_reason: str = ""
    target_reached: bool = False
    peak_equity: float = FTMO_10K.initial_balance
    events: list[dict] = field(default_factory=list)

    def log(self, kind: str, message: str) -> dict:
        ev = {"time": time.time(), "kind": kind, "message": message}
        self.events = (self.events + [ev])[-300:]
        return ev


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RiskState:
        try:
            data = json.loads(self.path.read_text())
            return RiskState(**{k: v for k, v in data.items() if k in RiskState.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return RiskState()

    def save(self, state: RiskState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=1))
        os.replace(tmp, self.path)  # atomic: a crash never leaves a half-written state


# ---------------------------------------------------------------------------- decisions
@dataclass
class GuardDecision:
    action: str          # ok | flatten_lock_day | flatten_halt | flatten_weekend | target_halt
    reason: str = ""


@dataclass
class SizedOrder:
    symbol: str
    side: str
    kind: str            # market | limit | stop
    volume: float
    price: float
    sl: float
    tp: float
    risk_usd: float
    reward_usd: float


class RiskRejected(Exception):
    pass


class RiskGuard:
    """Agent 2's rulebook. Owns the persisted RiskState."""

    def __init__(self, store: StateStore, rules: FTMORules = FTMO_10K) -> None:
        self.rules = rules
        self.store = store
        self.state = store.load()

    def save(self) -> None:
        self.store.save(self.state)

    # -- day roll & bookkeeping ------------------------------------------------------------
    def roll_day(self, now: datetime, balance: float) -> bool:
        key = prague_day(now)
        if key == self.state.day_key:
            return False
        first = not self.state.day_key
        self.state.day_key = key
        self.state.day_start_balance = balance
        if self.state.daily_locked:
            self.state.log("unlock", f"Midnight reset ({key}) - daily lock lifted")
        self.state.daily_locked = False
        if not first:
            self.state.log("day", f"New FTMO day {key}: start balance {balance:.2f}")
        self.save()
        return True

    def record_executions(self, now: datetime, tickets: list[int]) -> None:
        """A trading day counts when at least one trade is opened that day."""
        new = [t for t in tickets if t not in self.state.seen_tickets]
        if not new:
            return
        self.state.seen_tickets = (self.state.seen_tickets + new)[-2000:]
        day = prague_day(now)
        if day not in self.state.trading_days:
            self.state.trading_days.append(day)
            self.state.log("trading_day", f"Trading day #{len(self.state.trading_days)} recorded ({day})")
        self.save()

    # -- individual verifications ---------------------------------------------------------
    def daily_pnl(self, equity: float) -> float:
        return equity - self.state.day_start_balance

    def verify_daily_loss_limit(self, equity: float) -> tuple[bool, float, float]:
        """-> (ok, daily_pnl, headroom_to_trigger). Floating equity is what counts."""
        pnl = self.daily_pnl(equity)
        headroom = self.rules.daily_loss_trigger + pnl
        return headroom > 0, pnl, headroom

    def verify_overall_loss_limit(self, equity: float) -> tuple[bool, float]:
        headroom = equity - (self.rules.equity_floor + self.rules.overall_loss_buffer)
        return headroom > 0, headroom

    def verify_profit_target(self, balance: float, equity: float) -> tuple[bool, float]:
        target_equity = self.rules.initial_balance + self.rules.target(self.state.phase)
        return min(balance, equity) >= target_equity, target_equity - min(balance, equity)

    def trading_days_remaining(self) -> int:
        return max(0, self.rules.min_trading_days - len(self.state.trading_days))

    # -- the 1-second check ---------------------------------------------------------------
    def evaluate(self, now: datetime, balance: float, equity: float, has_positions: bool) -> GuardDecision:
        self.roll_day(now, balance)
        self.state.peak_equity = max(self.state.peak_equity, equity)
        if self.state.halted:
            return GuardDecision("flatten_halt" if has_positions else "ok", self.state.halt_reason)

        ok_overall, _ = self.verify_overall_loss_limit(equity)
        if not ok_overall:
            self.state.halted = True
            self.state.halt_reason = (f"Max overall loss guard: equity {equity:.2f} <= "
                                      f"{self.rules.equity_floor + self.rules.overall_loss_buffer:.2f}")
            self.state.log("halt", self.state.halt_reason)
            self.save()
            return GuardDecision("flatten_halt", self.state.halt_reason)

        ok_daily, pnl, _ = self.verify_daily_loss_limit(equity)
        if not ok_daily and not self.state.daily_locked:
            self.state.daily_locked = True
            msg = f"Daily loss guard: P/L {pnl:.2f} <= -{self.rules.daily_loss_trigger:.0f} - locked until midnight CE(S)T"
            self.state.log("lock", msg)
            self.save()
            return GuardDecision("flatten_lock_day", msg)
        if self.state.daily_locked and has_positions:
            return GuardDecision("flatten_lock_day", "daily lock active")

        hit, _ = self.verify_profit_target(balance, equity)
        if hit and not self.state.target_reached:
            self.state.target_reached = True
            msg = (f"Phase {self.state.phase} profit target reached "
                   f"(+{self.rules.target(self.state.phase):.0f}). Trading days {len(self.state.trading_days)}/"
                   f"{self.rules.min_trading_days}.")
            self.state.log("target", msg)
            self.save()
            return GuardDecision("target_halt", msg)

        if in_weekend_window(now, self.rules) and has_positions:
            return GuardDecision("flatten_weekend", "Standard account: no weekend holding - flattening before Friday close")
        return GuardDecision("ok")

    def can_open(self, now: datetime) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"halted: {self.state.halt_reason}"
        if self.state.daily_locked:
            return False, "daily loss lock active until midnight CE(S)T"
        if self.state.target_reached:
            return False, "profit target reached - system halted"
        if entries_blocked_for_weekend(now, self.rules):
            return False, "weekend window - no new entries (Standard account)"
        return True, ""

    def needs_daycount_trade(self, now: datetime) -> bool:
        return (self.state.target_reached and not self.state.halted and not self.state.daily_locked
                and self.trading_days_remaining() > 0 and prague_day(now) not in self.state.trading_days
                and not entries_blocked_for_weekend(now, self.rules))

    # -- proposal -> sized order -----------------------------------------------------------
    def size_order(self, *, symbol: str, side: str, entry: float, sl: float, tp: float, market_price: float,
                   loss_per_lot: float, profit_per_lot: float, margin_per_lot: float,
                   volume_min: float, volume_max: float, volume_step: float,
                   equity: float, margin_free: float, open_risk: float, open_positions: int,
                   risk_per_trade: float, kind: str = "market") -> SizedOrder:
        """Validate a proposal against every FTMO limit and return the order to send.

        `loss_per_lot` / `profit_per_lot` are the account-currency P/L of 1.0 lot from
        entry to SL / TP (positive numbers), computed by the broker (order_calc_profit)."""
        r = self.rules
        if side not in ("buy", "sell"):
            raise RiskRejected("direction must be buy or sell")
        if side == "buy" and not (sl < entry < tp):
            raise RiskRejected("BUY needs SL < entry < TP")
        if side == "sell" and not (tp < entry < sl):
            raise RiskRejected("SELL needs TP < entry < SL")
        if loss_per_lot <= 0:
            raise RiskRejected("could not price the stop distance")
        rr = profit_per_lot / loss_per_lot
        if rr < r.min_reward_risk:
            raise RiskRejected(f"reward:risk {rr:.2f} below {r.min_reward_risk}")
        if open_positions >= r.max_open_positions:
            raise RiskRejected(f"max {r.max_open_positions} open positions")

        ok_daily, pnl, daily_headroom = self.verify_daily_loss_limit(equity)
        ok_overall, overall_headroom = self.verify_overall_loss_limit(equity)
        if not ok_daily or not ok_overall:
            raise RiskRejected("loss limits already reached")
        # Worst case = every open position AND this one stop out today.
        budget = min(risk_per_trade, r.max_risk_per_trade, r.max_open_risk - open_risk,
                     daily_headroom - open_risk, overall_headroom - open_risk)
        if budget <= 0:
            raise RiskRejected(f"no risk budget left (daily headroom {daily_headroom:.2f}, open risk {open_risk:.2f})")

        volume = math.floor(budget / loss_per_lot / volume_step + 1e-9) * volume_step
        volume = round(min(volume, volume_max), 8)
        if volume < volume_min:
            raise RiskRejected(f"stop too wide: min lot {volume_min} risks {volume_min * loss_per_lot:.2f} > budget {budget:.2f}")
        if margin_per_lot * volume > margin_free * r.margin_usage_cap:
            volume = math.floor(margin_free * r.margin_usage_cap / margin_per_lot / volume_step) * volume_step
            if volume < volume_min:
                raise RiskRejected("insufficient free margin at 1:100")
        return SizedOrder(symbol, side, kind, round(volume, 8), entry, sl, tp,
                          round(volume * loss_per_lot, 2), round(volume * profit_per_lot, 2))

    # -- admin -----------------------------------------------------------------------------
    def snapshot(self, now: datetime, balance: float, equity: float, open_risk: float) -> dict:
        r, s = self.rules, self.state
        _, pnl, daily_headroom = self.verify_daily_loss_limit(equity)
        target_equity = r.initial_balance + r.target(s.phase)
        return {
            "rules": {"initial_balance": r.initial_balance, "leverage": r.leverage, "max_daily_loss": r.max_daily_loss,
                      "daily_loss_trigger": r.daily_loss_trigger, "max_overall_loss": r.max_overall_loss,
                      "equity_floor": r.equity_floor, "phase_target": r.target(s.phase),
                      "min_trading_days": r.min_trading_days},
            "phase": s.phase, "balance": balance, "equity": equity,
            "daily_pnl": round(pnl, 2), "daily_headroom": round(daily_headroom, 2),
            "day_start_balance": s.day_start_balance, "day_key": s.day_key,
            "overall_pnl": round(equity - r.initial_balance, 2),
            "equity_floor_distance": round(equity - r.equity_floor, 2),
            "target_equity": target_equity, "target_progress": round((equity - r.initial_balance) / r.target(s.phase), 4)
            if r.target(s.phase) else 0,
            "trading_days": s.trading_days, "trading_days_remaining": self.trading_days_remaining(),
            "daily_locked": s.daily_locked, "halted": s.halted, "halt_reason": s.halt_reason,
            "target_reached": s.target_reached, "open_risk": round(open_risk, 2),
            "weekend_window": in_weekend_window(now, r), "entries_blocked_weekend": entries_blocked_for_weekend(now, r),
            "next_weekend_flatten": next_weekend_flatten(now, r).isoformat(),
            "next_daily_reset": next_prague_midnight(now).isoformat(),
            "events": s.events[-40:],
        }

    def new_challenge(self, phase: int, balance: float, now: datetime) -> None:
        """Start a fresh Phase 1/2 (or re-arm after a passed phase). Overall-loss halts
        are only cleared here, deliberately behind an explicit user action."""
        self.state = RiskState(phase=phase, day_key=prague_day(now), day_start_balance=balance)
        self.state.log("challenge", f"New FTMO 10k Phase {phase} started at balance {balance:.2f}")
        self.save()
