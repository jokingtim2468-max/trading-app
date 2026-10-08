"""Agent 1 (Market Analyst) tool implementations.

Each public coroutine returns plain JSON-serialisable data; the same functions back
both the Claude tool-calling analyst and the offline rule-based analyst.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

import requests
from defusedxml import ElementTree as ET  # RSS is untrusted input: no XXE / entity bombs

from ..timeframes import parse_timeframe

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("bridge.agents.tools")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
NEWS_SYMBOL = {"NAS100": "^NDX", "US500": "^GSPC", "XAUUSD": "GC=F"}
NEWS_QUERY = {"NAS100": "Nasdaq 100 stocks", "US500": "S&P 500 stocks", "XAUUSD": "gold price"}
MAX_SOURCE_FAILURES = 3


# --------------------------------------------------------------------------- indicators
def ema(x: list[float], n: int) -> list[float]:
    out, k, prev = [], 2 / (n + 1), None
    for i, v in enumerate(x):
        if i < n - 1:
            out.append(math.nan)
            continue
        prev = sum(x[: n]) / n if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def atr(bars: list[dict], n: int = 14) -> list[float]:
    tr = [b["high"] - b["low"] if i == 0 else max(b["high"] - b["low"], abs(b["high"] - bars[i - 1]["close"]),
                                                   abs(b["low"] - bars[i - 1]["close"])) for i, b in enumerate(bars)]
    out, prev = [], None
    for i, v in enumerate(tr):
        if i < n - 1:
            out.append(math.nan)
            continue
        prev = sum(tr[:n]) / n if prev is None else (prev * (n - 1) + v) / n
        out.append(prev)
    return out


def rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) <= n:
        return math.nan
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains, losses = gains + max(d, 0), losses + max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _r(v: float, d: int) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(v, d)


# --------------------------------------------------------------------------- tools
class AnalystTools:
    def __init__(self, hub: "Hub", guard_snapshot) -> None:
        self.hub = hub
        self.guard_snapshot = guard_snapshot  # callable -> current FTMO snapshot dict
        self._source_failures: dict[str, int] = {}

    async def _bars(self, symbol: str, timeframe: str, count: int) -> tuple[dict, list[dict]]:
        tf = parse_timeframe(timeframe)
        info = await self.hub.resolve(symbol)
        if info["source"] == "mt5":
            return info, await self.hub.engine.history(info["symbol"], tf, count)
        _, _, bars = await self.hub.feed.history(info["symbol"], tf, count)
        return info, bars

    async def fetch_custom_tv_data(self, symbol: str, timeframe: str = "M5", bars: int = 300) -> dict[str, Any]:
        """Our chart data + the indicator levels the UI draws (EMA, ATR, RSI, PDH/PDL, ADR projections)."""
        bars = max(60, min(int(bars), 2000))
        info, intraday = await self._bars(symbol, timeframe, bars)
        _, daily = await self._bars(symbol, "D1", 20)
        if not intraday:
            return {"error": f"no data for {symbol}"}
        digits = info.get("digits") or 2
        closes = [b["close"] for b in intraday]
        e20, e50, a14 = ema(closes, 20), ema(closes, 50), atr(intraday, 14)
        last = intraday[-1]
        levels: dict[str, Any] = {}
        if len(daily) >= 3:
            prev, today = daily[-2], daily[-1]
            ranges = [d["high"] - d["low"] for d in daily[-15:-1]]
            adr = sum(ranges) / len(ranges)
            levels = {"prev_day_high": prev["high"], "prev_day_low": prev["low"], "prev_day_close": prev["close"],
                      "today_open": today["open"], "today_high": today["high"], "today_low": today["low"],
                      "adr14": _r(adr, digits), "adr_projected_high": _r(max(today["low"] + adr, today["high"]), digits),
                      "adr_projected_low": _r(min(today["high"] - adr, today["low"]), digits),
                      "pct_of_adr_used": _r((today["high"] - today["low"]) / adr * 100, 1) if adr else None}
        return {
            "symbol": info["symbol"], "source": info["source"], "tradable": bool(info.get("tradable")),
            "timeframe": timeframe, "last": {"time": last["time"], "close": last["close"]},
            "ema20": _r(e20[-1], digits), "ema50": _r(e50[-1], digits), "atr14": _r(a14[-1], digits),
            "rsi14": _r(rsi(closes[-200:]), 1),
            "trend": ("up" if e20[-1] > e50[-1] else "down") if not math.isnan(e50[-1]) else "unknown",
            "levels": levels,
            "recent_bars": [[b["time"], b["open"], b["high"], b["low"], b["close"]] for b in intraday[-40:]],
        }

    def _source_ok(self, name: str) -> bool:
        return self._source_failures.get(name, 0) < MAX_SOURCE_FAILURES

    def _source_result(self, name: str, ok: bool) -> None:
        self._source_failures[name] = 0 if ok else self._source_failures.get(name, 0) + 1

    async def scrape_external_news(self, query: str, max_items: int = 8) -> dict[str, Any]:
        """Headlines from public RSS feeds. A source that fails 3x in a row is skipped from then on."""
        sym = query.upper().strip()
        feeds = []
        if sym in NEWS_SYMBOL:
            feeds.append(("yahoo", f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(NEWS_SYMBOL[sym])}"
                                   "&region=US&lang=en-US"))
        q = NEWS_QUERY.get(sym, query)
        feeds.append(("google_news", f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"))

        def fetch(url: str) -> list[dict]:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            return [{"title": (i.findtext("title") or "").strip(), "published": i.findtext("pubDate") or "",
                     "source": (i.findtext("source") or "").strip()} for i in root.iter("item")]

        items: list[dict] = []
        errors: list[str] = []
        for name, url in feeds:
            if not self._source_ok(name):
                errors.append(f"{name}: disabled after repeated failures")
                continue
            try:
                got = await asyncio.wait_for(asyncio.to_thread(fetch, url), 15)
                self._source_result(name, True)
                items += [{**g, "feed": name} for g in got]
            except Exception as exc:
                self._source_result(name, False)
                errors.append(f"{name}: {exc}")
        seen, unique = set(), []
        for it in items:
            if it["title"] and it["title"] not in seen:
                seen.add(it["title"])
                unique.append(it)
        return {"query": query, "headlines": unique[: max(1, min(max_items, 20))], "errors": errors}

    async def get_economic_calendar(self, currency: str = "USD") -> dict[str, Any]:
        """This week's high-impact events (public ForexFactory JSON mirror)."""
        name = "ff_calendar"
        if not self._source_ok(name):
            return {"events": [], "error": "calendar source disabled after repeated failures"}

        def fetch() -> list[dict]:
            r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                             headers={"User-Agent": UA}, timeout=10)
            r.raise_for_status()
            return r.json()

        try:
            data = await asyncio.wait_for(asyncio.to_thread(fetch), 15)
            self._source_result(name, True)
        except Exception as exc:
            self._source_result(name, False)
            return {"events": [], "error": str(exc)}
        events = [{"title": e.get("title"), "country": e.get("country"), "date": e.get("date"),
                   "impact": e.get("impact"), "forecast": e.get("forecast"), "previous": e.get("previous")}
                  for e in data if e.get("country") == currency and e.get("impact") == "High"]
        return {"currency": currency, "events": events[:20]}

    async def get_market_conditions(self, symbol: str) -> dict[str, Any]:
        """Spread, session, open exposure and the FTMO headroom Agent 2 will enforce."""
        info = await self.hub.resolve(symbol)
        out: dict[str, Any] = {"symbol": info["symbol"], "tradable": bool(info.get("tradable")),
                               "utc_time": datetime.now(timezone.utc).isoformat()}
        if info["source"] == "mt5":
            t = await self.hub.engine.tick(info["symbol"])
            out.update(bid=t["bid"], ask=t["ask"], spread=round(t["ask"] - t["bid"], info.get("digits") or 2))
            positions = await self.hub.engine.positions()
            out["open_positions"] = [{k: p[k] for k in ("ticket", "side", "volume", "price_open", "sl", "tp", "profit",
                                                       "label")} for p in positions if p["symbol"] == info["symbol"]]
        snap = self.guard_snapshot()
        out["risk"] = {k: snap.get(k) for k in ("daily_pnl", "daily_headroom", "equity_floor_distance", "open_risk",
                                                 "daily_locked", "entries_blocked_weekend", "next_weekend_flatten",
                                                 "target_progress")}
        h = datetime.now(timezone.utc).hour
        out["session"] = "asia" if h < 7 else "london" if h < 13 else "new_york" if h < 21 else "late"
        return out

