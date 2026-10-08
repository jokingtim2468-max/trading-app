"""Agent 1 - Market Analyst.

Produces structured trade proposals {symbol, direction, entry, stop_loss, take_profit, ...}.
It never sizes or sends orders - that is Agent 2's job, and Agent 2 can veto anything.

Two implementations share one interface (`analyze(symbol) -> list[Proposal]`):
  * ClaudeAnalyst   - Claude with tool calling over our chart data, news and calendar.
  * RuleAnalyst     - deterministic EMA-pullback / ADR logic; used when no API key is set
                      (and in tests), so the whole pipeline also works offline.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from .tools import AnalystTools

log = logging.getLogger("bridge.agents.analyst")

MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 8


@dataclass
class Proposal:
    symbol: str
    direction: str            # buy | sell
    entry: float
    stop_loss: float
    take_profit: float
    order_type: str = "market"  # market | limit
    confidence: float = 0.5
    rationale: str = ""
    analyst: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- Claude
SYSTEM_PROMPT = """You are the Market Analyst in a two-agent trading system for an FTMO 10k Standard \
evaluation. You analyse and propose; a separate Risk Manager sizes, validates and executes, and \
will reject anything that breaks the prop-firm rules.

Instruments: NAS100 (Nasdaq 100 CFD), US500 (S&P 500 CFD) and XAUUSD (gold). Style: intraday only; \
positions are force-closed before the weekend, so do not plan multi-day holds.

How to work:
- Call fetch_custom_tv_data for the symbol (an intraday timeframe such as M5 or M15) before forming a view; \
  use the levels it returns (previous-day high/low, ADR projected high/low, EMAs, ATR).
- Check get_market_conditions for spread, session and the remaining risk headroom.
- Check get_economic_calendar and scrape_external_news when a high-impact event or headline could \
  move the instrument; avoid entries in the minutes around high-impact US releases.
- Place the stop beyond a real level (structure, PDH/PDL, ATR multiple) and the target at a realistic \
  level inside the ADR projection. Aim for reward:risk of at least 1.5.
- Finish with exactly one call: submit_trade_proposal when there is a clear setup, otherwise no_trade \
  with the reason. Staying flat is a good outcome when the edge is unclear."""

TOOLS: list[dict[str, Any]] = [
    {"name": "fetch_custom_tv_data",
     "description": "Read our terminal's chart data for a symbol: recent OHLC bars plus indicator levels "
                    "(EMA20/50, ATR14, RSI14, previous-day high/low/close, today's range, ADR14 and ADR "
                    "projected high/low). Same data the user sees on the charts.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["symbol", "timeframe"],
                      "properties": {"symbol": {"type": "string"},
                                     "timeframe": {"type": "string", "description": "e.g. M5, M15, H1"},
                                     "bars": {"type": "integer", "description": "history length, 60-2000"}}}},
    {"name": "get_market_conditions",
     "description": "Live spread, trading session, open positions on the symbol and the FTMO risk headroom "
                    "(daily P/L, distance to limits, weekend lockout) the Risk Manager will enforce.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["symbol"],
                      "properties": {"symbol": {"type": "string"}}}},
    {"name": "scrape_external_news",
     "description": "Latest headlines from public financial RSS feeds for a symbol (NAS100, US500, XAUUSD) "
                    "or a free-text query.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["query"],
                      "properties": {"query": {"type": "string"},
                                     "max_items": {"type": "integer"}}}},
    {"name": "get_economic_calendar",
     "description": "This week's high-impact economic events for a currency (default USD) with times.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": [],
                      "properties": {"currency": {"type": "string"}}}},
    {"name": "submit_trade_proposal",
     "description": "Submit the final trade proposal for the Risk Manager. Prices must be absolute levels. "
                    "Do not choose a position size.",
     "strict": True,
     "input_schema": {"type": "object", "additionalProperties": False,
                      "required": ["symbol", "direction", "order_type", "entry", "stop_loss", "take_profit",
                                   "confidence", "rationale"],
                      "properties": {"symbol": {"type": "string"},
                                     "direction": {"type": "string", "enum": ["buy", "sell"]},
                                     "order_type": {"type": "string", "enum": ["market", "limit"]},
                                     "entry": {"type": "number"},
                                     "stop_loss": {"type": "number"},
                                     "take_profit": {"type": "number"},
                                     "confidence": {"type": "number"},
                                     "rationale": {"type": "string"}}}},
    {"name": "no_trade",
     "description": "Finish without a proposal and explain why.",
     "strict": True,
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["reason"],
                      "properties": {"reason": {"type": "string"}}}},
]


class ClaudeAnalyst:
    name = "claude"

    def __init__(self, tools: AnalystTools, api_key: str = "", effort: str = "medium",
                 on_event: Callable[[str, str], Awaitable[None]] | None = None) -> None:
        import anthropic  # imported lazily so the bridge runs without the SDK installed
        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.tools = tools
        self.effort = effort
        self.on_event = on_event

    async def _emit(self, kind: str, msg: str) -> None:
        if self.on_event:
            await self.on_event(kind, msg)

    async def _run_tool(self, name: str, args: dict) -> Any:
        fn = {"fetch_custom_tv_data": self.tools.fetch_custom_tv_data,
              "get_market_conditions": self.tools.get_market_conditions,
              "scrape_external_news": self.tools.scrape_external_news,
              "get_economic_calendar": self.tools.get_economic_calendar}.get(name)
        if fn is None:
            raise ValueError(f"unknown tool {name}")
        return await fn(**args)

    async def analyze(self, symbol: str) -> list[Proposal]:
        messages: list[dict[str, Any]] = [{"role": "user", "content":
                                           f"Analyse {symbol} now and decide whether there is an intraday trade. "
                                           f"Current UTC time: {time.strftime('%Y-%m-%d %H:%M', time.gmtime())}."}]
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                resp = await self.client.beta.messages.create(
                    model=MODEL,
                    max_tokens=16000,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    tool_choice={"type": "auto"},
                    output_config={"effort": self.effort},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",  # a policy decline is re-run server-side on the recommended model
                    messages=messages,
                )
            except self._anthropic.RateLimitError as exc:
                await self._emit("error", f"Claude rate limited: {exc.message}")
                return []
            except self._anthropic.APIStatusError as exc:
                await self._emit("error", f"Claude API error {exc.status_code}: {exc.message}")
                return []
            except self._anthropic.APIConnectionError:
                await self._emit("error", "Claude API unreachable")
                return []

            if resp.stop_reason == "refusal":
                await self._emit("analyst", f"{symbol}: model declined to analyse this request")
                return []
            if resp.stop_reason == "max_tokens":
                await self._emit("error", f"{symbol}: analysis hit max_tokens")
                return []

            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                await self._emit("analyst", f"{symbol}: no proposal ({text[:200] or 'no tool call'})")
                return []

            results, final = [], None
            for use in uses:
                if use.name == "submit_trade_proposal":
                    final = use.input
                    results.append({"type": "tool_result", "tool_use_id": use.id, "content": "received"})
                elif use.name == "no_trade":
                    await self._emit("analyst", f"{symbol}: no trade - {use.input.get('reason', '')}")
                    return []
                else:
                    try:
                        out = await self._run_tool(use.name, dict(use.input))
                        results.append({"type": "tool_result", "tool_use_id": use.id,
                                        "content": json.dumps(out, default=str)[:60_000]})
                    except Exception as exc:  # tool failure goes back to the model, not up the stack
                        results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                        "content": f"Error: {exc}"})
            if final is not None:
                try:
                    p = Proposal(symbol=str(final["symbol"]).upper(), direction=final["direction"],
                                 entry=float(final["entry"]), stop_loss=float(final["stop_loss"]),
                                 take_profit=float(final["take_profit"]), order_type=final["order_type"],
                                 confidence=float(final["confidence"]), rationale=str(final["rationale"])[:1500],
                                 analyst=f"claude:{resp.model}")
                except (KeyError, TypeError, ValueError) as exc:
                    await self._emit("error", f"{symbol}: malformed proposal ({exc})")
                    return []
                return [p]
            messages.append({"role": "user", "content": results})
        await self._emit("error", f"{symbol}: analysis exceeded {MAX_TOOL_ROUNDS} tool rounds")
        return []


# --------------------------------------------------------------------------- rule-based
class RuleAnalyst:
    """Trend pullback: EMA20 > EMA50 (or <), price tags EMA20 and the last bar closes back in the
    trend direction. SL 1.5 ATR, TP 2.5 ATR capped at the ADR projection. Deliberately simple and
    transparent - a baseline, not an edge."""
    name = "rules"

    def __init__(self, tools: AnalystTools, timeframe: str = "M5") -> None:
        self.tools = tools
        self.timeframe = timeframe

    async def analyze(self, symbol: str) -> list[Proposal]:
        d = await self.tools.fetch_custom_tv_data(symbol, self.timeframe, 300)
        if "error" in d or d["atr14"] is None or d["ema50"] is None:
            return []
        bars = d["recent_bars"]
        a, e20, last = d["atr14"], d["ema20"], bars[-1]
        prev = bars[-2]
        lv = d.get("levels") or {}
        if d["trend"] == "up" and prev[3] <= e20 and last[4] > e20 and last[4] > last[1]:
            entry = last[4]
            sl, tp = entry - 1.5 * a, entry + 2.5 * a
            if lv.get("adr_projected_high"):
                tp = min(tp, lv["adr_projected_high"])
            direction = "buy"
        elif d["trend"] == "down" and prev[2] >= e20 and last[4] < e20 and last[4] < last[1]:
            entry = last[4]
            sl, tp = entry + 1.5 * a, entry - 2.5 * a
            if lv.get("adr_projected_low"):
                tp = max(tp, lv["adr_projected_low"])
            direction = "sell"
        else:
            return []
        if abs(tp - entry) < abs(entry - sl):  # ADR cap left no room
            return []
        rr = abs(tp - entry) / abs(entry - sl)
        return [Proposal(symbol=d["symbol"], direction=direction, entry=entry, stop_loss=round(sl, 5),
                         take_profit=round(tp, 5), confidence=min(0.9, 0.4 + 0.1 * rr),
                         rationale=f"{d['trend']} trend (EMA20 {e20} vs EMA50 {d['ema50']}), pullback to EMA20, "
                                   f"ATR {a}, R:R {rr:.2f}", analyst="rules")]


def is_finite(*xs: float) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)
