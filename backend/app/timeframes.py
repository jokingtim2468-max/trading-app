"""Timeframe parsing and bar aggregation shared by every data engine.

Accepted inputs (case-insensitive where unambiguous):
  * MT5 style:      M1 M5 M15 H1 H4 D1 W1 MN1
  * Compact style:  1s 5s 30s 1m 3m 7m 1h 2h 1d 1w 1mn
  * Tick charts:    100T 250t 1000T
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mn": 2592000}


@dataclass(frozen=True)
class Timeframe:
    label: str
    kind: str  # "time" | "tick"
    seconds: int = 0
    ticks: int = 0

    @property
    def sub_minute(self) -> bool:
        return self.kind == "time" and self.seconds < 60


def parse_timeframe(raw: str) -> Timeframe:
    s = raw.strip()
    if m := re.fullmatch(r"(\d+)\s*[tT]", s):
        n = int(m.group(1))
        if n < 1:
            raise ValueError("tick count must be >= 1")
        return Timeframe(f"{n}T", "tick", ticks=n)
    if m := re.fullmatch(r"(MN|[MHDW])(\d+)", s.upper()):
        unit = {"M": "m", "H": "h", "D": "d", "W": "w", "MN": "mn"}[m.group(1)]
        n = int(m.group(2))
    elif m := re.fullmatch(r"(\d+)\s*(mn|s|m|h|d|w)", s.lower()):
        n, unit = int(m.group(1)), m.group(2)
    else:
        raise ValueError(f"unrecognised timeframe '{raw}'")
    if n < 1:
        raise ValueError("timeframe multiplier must be >= 1")
    seconds = n * SECONDS[unit]
    return Timeframe(f"{n}{unit}", "time", seconds=seconds)


def resample(bars: list[dict], seconds: int) -> list[dict]:
    """Aggregate ascending OHLCV bars into a coarser time bucket."""
    out: list[dict] = []
    for b in bars:
        bucket = b["time"] - b["time"] % seconds
        if out and out[-1]["time"] == bucket:
            o = out[-1]
            o["high"] = max(o["high"], b["high"])
            o["low"] = min(o["low"], b["low"])
            o["close"] = b["close"]
            o["volume"] += b.get("volume", 0)
        else:
            out.append({"time": bucket, "open": b["open"], "high": b["high"], "low": b["low"],
                        "close": b["close"], "volume": b.get("volume", 0)})
    return out


def tick_price(t: dict) -> float:
    """Charts use `last` when the symbol reports trades (stocks/futures), else bid (FX/CFD)."""
    return t["last"] if t.get("last") else t["bid"]


def ticks_to_time_bars(ticks: Iterable[dict], seconds: int) -> list[dict]:
    out: list[dict] = []
    for t in ticks:
        p = tick_price(t)
        if not p:
            continue
        bucket = int(t["time_msc"] // 1000)
        bucket -= bucket % seconds
        if out and out[-1]["time"] == bucket:
            o = out[-1]
            o["high"] = max(o["high"], p)
            o["low"] = min(o["low"], p)
            o["close"] = p
            o["volume"] += 1
        else:
            out.append({"time": bucket, "open": p, "high": p, "low": p, "close": p, "volume": 1})
    return out


def ticks_to_tick_bars(ticks: Iterable[dict], per_bar: int) -> list[dict]:
    """Tick-count bars. Lightweight Charts needs strictly increasing times, so a bar
    that would share a second with its predecessor is nudged forward by one second
    (the frontend live aggregator applies the identical rule)."""
    out: list[dict] = []
    count = 0
    for t in ticks:
        p = tick_price(t)
        if not p:
            continue
        sec = int(t["time_msc"] // 1000)
        if out and count < per_bar:
            o = out[-1]
            o["high"] = max(o["high"], p)
            o["low"] = min(o["low"], p)
            o["close"] = p
            o["volume"] += 1
            count += 1
        else:
            if out and sec <= out[-1]["time"]:
                sec = out[-1]["time"] + 1
            out.append({"time": sec, "open": p, "high": p, "low": p, "close": p, "volume": 1})
            count = 1
    return out
