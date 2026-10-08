import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.agents.analyst import Proposal
from app.agents.executor import AGENT_MAGIC, ExecutionRiskManager
from app.agents.ftmo import FTMO_10K, RiskGuard, RiskRejected, StateStore, in_weekend_window, entries_blocked_for_weekend
from app.engines.sim_engine import SimEngine

NY = ZoneInfo("America/New_York")
WED_NOON = datetime(2026, 10, 7, 12, 0, tzinfo=NY)  # a Wednesday


def guard(tmp_path, balance=10_000.0, now=WED_NOON):
    g = RiskGuard(StateStore(tmp_path / "s.json"))
    g.roll_day(now, balance)
    return g


# ------------------------------------------------------------------ FTMO rules
def test_hardcoded_limits():
    r = FTMO_10K
    assert (r.initial_balance, r.leverage, r.max_daily_loss, r.max_overall_loss) == (10_000, 100, 500, 1_000)
    assert r.target(1) == 1_000 and r.target(2) == 500 and r.min_trading_days == 4
    assert r.equity_floor == 9_000


def test_daily_loss_flattens_at_480_and_unlocks_at_prague_midnight(tmp_path):
    g = guard(tmp_path)
    assert g.evaluate(WED_NOON, 10_000, 9_525, True).action == "ok"            # -475: still fine
    d = g.evaluate(WED_NOON, 10_000, 9_519, True)                                # -481: trigger
    assert d.action == "flatten_lock_day" and g.state.daily_locked
    assert g.can_open(WED_NOON)[0] is False
    # FTMO day boundary is midnight Prague (= 18:00 New York in October)
    after_reset = datetime(2026, 10, 7, 18, 1, tzinfo=NY)
    assert g.evaluate(after_reset, 9_519, 9_519, False).action == "ok"
    assert not g.state.daily_locked and g.state.day_start_balance == 9_519


def test_daily_loss_uses_floating_equity(tmp_path):
    g = guard(tmp_path)
    ok, pnl, headroom = g.verify_daily_loss_limit(9_600)   # balance untouched, open trade -400
    assert ok and pnl == -400 and headroom == 80


def test_overall_loss_halts_permanently(tmp_path):
    g = guard(tmp_path, balance=9_300)
    assert g.evaluate(WED_NOON, 9_300, 9_019, True).action == "flatten_halt"
    reloaded = RiskGuard(StateStore(tmp_path / "s.json"))
    assert reloaded.state.halted and reloaded.can_open(WED_NOON)[0] is False
    reloaded.new_challenge(1, 10_000, WED_NOON)
    assert not reloaded.state.halted


@pytest.mark.parametrize("phase,equity,hit", [(1, 10_999, False), (1, 11_000, True), (2, 10_500, True), (2, 10_499, False)])
def test_profit_targets(tmp_path, phase, equity, hit):
    g = guard(tmp_path)
    g.state.phase = phase
    g.state.day_start_balance = equity  # isolate the target check from the daily-loss check
    d = g.evaluate(WED_NOON, equity, equity, False)
    assert (d.action == "target_halt") is hit


def test_weekend_rules():
    fri = lambda h, m: datetime(2026, 10, 9, h, m, tzinfo=NY)
    assert not in_weekend_window(fri(16, 29)) and in_weekend_window(fri(16, 30))
    assert entries_blocked_for_weekend(fri(15, 45)) and not entries_blocked_for_weekend(fri(15, 44))
    assert in_weekend_window(datetime(2026, 10, 10, 12, 0, tzinfo=NY))                 # Saturday
    assert in_weekend_window(datetime(2026, 10, 11, 18, 0, tzinfo=NY))                 # Sunday pre-open
    assert not in_weekend_window(datetime(2026, 10, 11, 18, 10, tzinfo=NY))


def test_weekend_flatten_decision(tmp_path):
    g = guard(tmp_path)
    assert g.evaluate(datetime(2026, 10, 9, 16, 31, tzinfo=NY), 10_000, 10_000, True).action == "flatten_weekend"


def test_trading_days_and_daycount(tmp_path):
    g = guard(tmp_path)
    for i, day in enumerate([5, 6, 7]):
        g.record_executions(datetime(2026, 10, day, 12, tzinfo=NY), [100 + i])
    g.record_executions(datetime(2026, 10, 7, 13, tzinfo=NY), [200])  # same day: not a new day
    assert len(g.state.trading_days) == 3 and g.trading_days_remaining() == 1
    g.state.target_reached = True
    assert g.needs_daycount_trade(datetime(2026, 10, 8, 12, tzinfo=NY))


def sized(g, **kw):
    base = dict(symbol="NAS100", side="buy", entry=20_000, sl=19_900, tp=20_200, market_price=20_000,
                loss_per_lot=100.0, profit_per_lot=200.0, margin_per_lot=200.0, volume_min=0.1, volume_max=50,
                volume_step=0.1, equity=10_000, margin_free=10_000, open_risk=0.0, open_positions=0,
                risk_per_trade=50.0)
    base.update(kw)
    return g.size_order(**base)


def test_position_sizing(tmp_path):
    g = guard(tmp_path)
    o = sized(g)
    assert o.volume == 0.5 and o.risk_usd == 50 and o.reward_usd == 100
    # risk per trade can never exceed the $100 hard cap
    assert sized(g, risk_per_trade=10_000).risk_usd == 100
    # worst case (all open risk + this trade) must stay inside the daily trigger
    g.state.day_start_balance = 10_000
    assert sized(g, equity=9_600, open_risk=50).risk_usd == 30   # headroom 80 - open risk 50
    with pytest.raises(RiskRejected):
        sized(g, equity=9_560, open_risk=50)                       # headroom 40 < open risk: no budget
    with pytest.raises(RiskRejected):
        sized(g, tp=20_050, profit_per_lot=50.0)  # R:R 0.5
    with pytest.raises(RiskRejected):
        sized(g, sl=20_050)                       # SL on the wrong side
    with pytest.raises(RiskRejected):
        sized(g, loss_per_lot=2_000.0, profit_per_lot=4_000.0)  # min lot risks $200
    with pytest.raises(RiskRejected):
        sized(g, open_positions=3)


# ------------------------------------------------------------------ Agent 2 against the sim broker
@pytest.fixture
def sim_agent(tmp_path):
    async def make():
        eng = SimEngine()
        await eng.login("SIM", 1, "")
        for t in eng._tasks:  # freeze the market + toy EAs for deterministic tests
            t.cancel()
        g = RiskGuard(StateStore(tmp_path / "state.json"))
        return eng, g, ExecutionRiskManager(eng, g)
    return make


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_agent2_executes_sized_order_with_magic(sim_agent):
    async def go():
        eng, g, ex = await sim_agent()
        await ex.enforce()
        px = eng.books["NAS100"].ask
        res = await ex.execute_mt5_order(Proposal("NAS100", "buy", px, px - 50, px + 100, analyst="test"))
        assert res["status"] == "executed", res
        pos = await eng.positions()
        assert pos[0]["magic"] == AGENT_MAGIC and pos[0]["volume"] == 1.0  # $50 risk / $50 per lot
        again = await ex.execute_mt5_order(Proposal("NAS100", "buy", px, px - 50, px + 100))
        assert again["status"] == "rejected"   # one agent position per symbol
    run(go())


def test_agent2_daily_breach_closes_everything_and_locks(sim_agent):
    async def go():
        eng, g, ex = await sim_agent()
        await ex.enforce()
        eng._open("US500", "buy", 1.0, 0, 0, 0, "manual")         # a manual trade is closed too
        eng._open("XAUUSD", "sell", 0.1, 0, 0, 555123, "EA")      # ...and an EA's
        g.state.day_start_balance = 10_000 + 481                   # equity now 481 below day start
        snap = await ex.enforce()
        assert await eng.positions() == [] and snap["daily_locked"]
        px = eng.books["NAS100"].ask
        res = await ex.execute_mt5_order(Proposal("NAS100", "buy", px, px - 50, px + 100))
        assert res["status"] == "rejected" and "daily" in res["reason"]
    run(go())


def test_demo_mode_gate(sim_agent):
    async def go():
        eng, g, ex = await sim_agent()
        ex.demo_mode = False  # live mode on a demo account must refuse
        px = eng.books["NAS100"].ask
        res = await ex.execute_mt5_order(Proposal("NAS100", "buy", px, px - 50, px + 100))
        assert res["status"] == "rejected" and "demo account" in res["reason"]
        assert await eng.positions() == []
    run(go())
