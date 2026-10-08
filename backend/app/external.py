"""External (non-MT5) market data: TradingView first, then four public fallbacks.

Retry policy (deliberately conservative - we never hammer sites that block bots):
  * At startup every provider is probed once, in parallel.
  * A provider that fails MAX_FAILURES times in a row is marked "down" and is
    not contacted again automatically.
  * When all five are down the feed "gives up": no further attempts are made
    until the user presses Retry in the UI (or re-authenticates TradingView).
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .timeframes import Timeframe, resample

log = logging.getLogger("bridge.external")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
MAX_FAILURES = 3
TIMEOUT = 10
CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LTC", "AVAX", "DOT", "LINK", "TRX",
                "MATIC", "TON", "SHIB", "BCH", "XLM", "ATOM", "NEAR", "APT", "ARB", "OP", "PEPE"}


class ProviderError(Exception):
    pass


def _split(symbol: str) -> tuple[str, str]:
    s = symbol.strip().upper()
    if ":" in s:
        ex, _, sym = s.partition(":")
        return ex, sym
    return "", s


def _crypto_base(sym: str) -> tuple[str, str] | None:
    for quote in ("USDT", "USDC", "FDUSD", "BUSD", "USD"):
        if sym.endswith(quote) and sym[: -len(quote)] in CRYPTO_BASES:
            return sym[: -len(quote)], quote
    if "-" in sym:
        base, _, quote = sym.partition("-")
        if base in CRYPTO_BASES:
            return base, quote
    return None


def _pick_base(tf: Timeframe, native: dict[int, Any]) -> tuple[int, Any]:
    if tf.kind != "time":
        raise ProviderError("tick charts need MT5 tick data")
    options = [s for s in native if tf.seconds % s == 0]
    if not options:
        raise ProviderError(f"{tf.label} not available from this source")
    base = max(options)
    return base, native[base]


def _finish(bars: list[dict], tf: Timeframe, base: int, count: int) -> list[dict]:
    bars.sort(key=lambda b: b["time"])
    dedup: list[dict] = []
    for b in bars:
        if dedup and dedup[-1]["time"] == b["time"]:
            dedup[-1] = b
        else:
            dedup.append(b)
    return (dedup if base == tf.seconds else resample(dedup, tf.seconds))[-count:]


# --------------------------------------------------------------------------- providers
class Provider:
    name = "base"
    label = "Base"
    probe_symbol = ""

    def map(self, symbol: str) -> str | None:  # provider-native symbol, or None if unsupported
        raise NotImplementedError

    def history(self, native: str, tf: Timeframe, count: int) -> list[dict]:
        raise NotImplementedError

    def probe(self) -> None:
        from .timeframes import parse_timeframe
        if not self.history(self.map(self.probe_symbol), parse_timeframe("1d"), 5):
            raise ProviderError("probe returned no data")


class TradingViewProvider(Provider):
    name, label, probe_symbol = "tradingview", "TradingView", "NASDAQ:AAPL"
    KNOWN = {"NDX": "NASDAQ:NDX", "NAS100": "NASDAQ:NDX", "NQ": "CME_MINI:NQ1!", "SPX": "SP:SPX",
             "US500": "SP:SPX", "ES": "CME_MINI:ES1!", "SPY": "AMEX:SPY", "QQQ": "NASDAQ:QQQ",
             "XAUUSD": "OANDA:XAUUSD", "GOLD": "OANDA:XAUUSD", "GC": "COMEX:GC1!"}

    def __init__(self) -> None:
        self.tv: Any = None
        self.username: str | None = None
        self.mode = "anonymous"
        self.cookies: dict[str, str] = {}
        self._interval: dict[int, Any] = {}
        self._search_cache: dict[str, str] = {}
        self._init(None)

    def _init(self, token: str | None, username: str | None = None, password: str | None = None) -> None:
        try:
            from tvDatafeed import Interval, TvDatafeed
        except ImportError as exc:
            raise ProviderError("tvdatafeed package not installed") from exc
        self._interval = {60: Interval.in_1_minute, 180: Interval.in_3_minute, 300: Interval.in_5_minute,
                          900: Interval.in_15_minute, 1800: Interval.in_30_minute, 2700: Interval.in_45_minute,
                          3600: Interval.in_1_hour, 7200: Interval.in_2_hour, 10800: Interval.in_3_hour,
                          14400: Interval.in_4_hour, 86400: Interval.in_daily, 604800: Interval.in_weekly,
                          2592000: Interval.in_monthly}
        self.tv = TvDatafeed(username, password) if username else TvDatafeed()
        if token:
            self.tv.token = token

    def auth_session(self, sessionid: str, sessionid_sign: str = "") -> dict:
        """Turn the browser `sessionid` cookie into the websocket auth_token tvDatafeed uses."""
        self.cookies = {"sessionid": sessionid}
        if sessionid_sign:
            self.cookies["sessionid_sign"] = sessionid_sign
        r = requests.get("https://www.tradingview.com/", cookies=self.cookies, headers={"User-Agent": UA},
                         timeout=TIMEOUT)
        token = re.search(r'"auth_token":"(.*?)"', r.text)
        user = re.search(r'"username":"(.*?)"', r.text)
        if not token or not user:
            raise ProviderError("TradingView rejected the sessionid (expired or wrong cookie)")
        self._init(token.group(1))
        self.username, self.mode = user.group(1), "session"
        plan = re.search(r'"pro_plan":"(.*?)"', r.text)
        return {"username": self.username, "mode": self.mode, "plan": plan.group(1) if plan else ""}

    def auth_password(self, username: str, password: str) -> dict:
        self._init(None, username, password)
        if not getattr(self.tv, "token", None) or self.tv.token == "unauthorized_user_token":
            raise ProviderError("TradingView login failed (wrong credentials, 2FA or captcha). Use sessionid.")
        self.username, self.mode, self.cookies = username, "credentials", {}
        return {"username": username, "mode": self.mode, "plan": ""}

    def watchlists(self) -> list[dict]:
        if not self.cookies:
            raise ProviderError("watchlists need sessionid auth")
        r = requests.get("https://www.tradingview.com/api/v1/symbols_list/custom/", cookies=self.cookies,
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        return [{"id": w.get("id"), "name": w.get("name"),
                 "symbols": [s for s in w.get("symbols", []) if not s.startswith("###")]} for w in r.json()]

    def map(self, symbol: str) -> str | None:
        ex, sym = _split(symbol)
        if ex:
            return f"{ex}:{sym}"
        if sym in self.KNOWN:
            return self.KNOWN[sym]
        if sym in self._search_cache:
            return self._search_cache[sym]
        try:
            hits = self.tv.search_symbol(sym) or []
        except Exception:
            hits = []
        exact = [h for h in hits if h.get("symbol", "").upper() == sym] or hits
        if not exact:
            return None
        native = f"{exact[0]['exchange'].upper()}:{exact[0]['symbol'].upper()}"
        self._search_cache[sym] = native
        return native

    def history(self, native: str, tf: Timeframe, count: int) -> list[dict]:
        base, interval = _pick_base(tf, self._interval)
        ex, sym = native.split(":", 1)
        df = self.tv.get_hist(symbol=sym, exchange=ex, interval=interval,
                              n_bars=min(5000, count * (tf.seconds // base)))
        if df is None or df.empty:
            raise ProviderError(f"no TradingView data for {native}")
        bars = [{"time": int(ts.timestamp()), "open": float(r.open), "high": float(r.high), "low": float(r.low),
                 "close": float(r.close), "volume": float(r.volume)} for ts, r in df.iterrows()]
        return _finish(bars, tf, base, count)


class YahooProvider(Provider):
    name, label, probe_symbol = "yahoo", "Yahoo Finance", "AAPL"
    NATIVE = {60: ("1m", "7d"), 120: ("2m", "60d"), 300: ("5m", "60d"), 900: ("15m", "60d"),
              1800: ("30m", "60d"), 3600: ("60m", "730d"), 86400: ("1d", "max"), 604800: ("1wk", "max"),
              2592000: ("1mo", "max")}
    INDEX = {"SPX": "^GSPC", "SPX500": "^GSPC", "NDX": "^NDX", "DJI": "^DJI", "VIX": "^VIX", "DAX": "^GDAXI",
             "UKX": "^FTSE", "NI225": "^N225", "RUT": "^RUT", "DXY": "DX-Y.NYB",
             "XAUUSD": "GC=F", "GOLD": "GC=F", "NAS100": "^NDX", "US500": "^GSPC"}

    def map(self, symbol: str) -> str | None:
        _, sym = _split(symbol)
        if sym in self.INDEX:
            return self.INDEX[sym]
        if c := _crypto_base(sym):
            return f"{c[0]}-USD"
        if re.fullmatch(r"[A-Z]{6}", sym) and sym[:3] in {"EUR", "GBP", "USD", "AUD", "NZD", "CAD", "CHF", "JPY"}:
            return f"{sym}=X"
        return sym

    def history(self, native: str, tf: Timeframe, count: int) -> list[dict]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ProviderError("yfinance not installed") from exc
        base, (interval, period) = _pick_base(tf, self.NATIVE)
        df = yf.Ticker(native).history(period=period, interval=interval, auto_adjust=False)
        if df is None or df.empty:
            raise ProviderError(f"no Yahoo data for {native}")
        bars = [{"time": int(ts.timestamp()), "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
                 "close": float(r.Close), "volume": float(r.Volume)} for ts, r in df.iterrows()]
        return _finish(bars, tf, base, count)


class BinanceProvider(Provider):
    name, label, probe_symbol = "binance", "Binance", "BTCUSDT"
    NATIVE = {1: "1s", 60: "1m", 180: "3m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 7200: "2h",
              14400: "4h", 21600: "6h", 28800: "8h", 43200: "12h", 86400: "1d", 259200: "3d", 604800: "1w"}

    def map(self, symbol: str) -> str | None:
        ex, sym = _split(symbol)
        if ex and ex != "BINANCE":
            return None
        c = _crypto_base(sym.replace("-", ""))
        if not c:
            return None
        return c[0] + ("USDT" if c[1] == "USD" else c[1])

    def history(self, native: str, tf: Timeframe, count: int) -> list[dict]:
        base, interval = _pick_base(tf, self.NATIVE)
        r = requests.get("https://api.binance.com/api/v3/klines", timeout=TIMEOUT, headers={"User-Agent": UA},
                         params={"symbol": native, "interval": interval,
                                 "limit": min(1000, count * (tf.seconds // base))})
        if r.status_code != 200:
            raise ProviderError(f"Binance HTTP {r.status_code}")
        bars = [{"time": int(k[0]) // 1000, "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])} for k in r.json()]
        return _finish(bars, tf, base, count)


class CoinbaseProvider(Provider):
    name, label, probe_symbol = "coinbase", "Coinbase", "BTCUSD"
    NATIVE = {60: 60, 300: 300, 900: 900, 3600: 3600, 21600: 21600, 86400: 86400}

    def map(self, symbol: str) -> str | None:
        ex, sym = _split(symbol)
        if ex and ex != "COINBASE":
            return None
        c = _crypto_base(sym)
        if not c:
            return None
        return f"{c[0]}-{'USD' if c[1] in ('USD', 'USDT') else c[1]}"

    def history(self, native: str, tf: Timeframe, count: int) -> list[dict]:
        base, gran = _pick_base(tf, self.NATIVE)
        r = requests.get(f"https://api.exchange.coinbase.com/products/{native}/candles", timeout=TIMEOUT,
                         headers={"User-Agent": UA}, params={"granularity": gran})
        if r.status_code != 200:
            raise ProviderError(f"Coinbase HTTP {r.status_code}")
        bars = [{"time": int(k[0]), "low": float(k[1]), "high": float(k[2]), "open": float(k[3]),
                 "close": float(k[4]), "volume": float(k[5])} for k in r.json()]
        return _finish(bars, tf, base, count)


class StooqProvider(Provider):
    name, label, probe_symbol = "stooq", "Stooq", "AAPL"
    NATIVE = {86400: "d", 604800: "w", 2592000: "m"}

    def map(self, symbol: str) -> str | None:
        ex, sym = _split(symbol)
        if _crypto_base(sym):
            return None
        if ex in ("", "NASDAQ", "NYSE", "AMEX", "BATS") and re.fullmatch(r"[A-Z.]{1,6}", sym):
            return f"{sym.lower()}.us"
        return None

    def history(self, native: str, tf: Timeframe, count: int) -> list[dict]:
        import pandas as pd
        base, interval = _pick_base(tf, self.NATIVE)
        r = requests.get("https://stooq.com/q/d/l/", params={"s": native, "i": interval}, timeout=TIMEOUT,
                         headers={"User-Agent": UA})
        if r.status_code != 200 or not r.text.startswith("Date"):
            raise ProviderError("Stooq refused the request")
        df = pd.read_csv(io.StringIO(r.text))
        bars = [{"time": int(pd.Timestamp(row.Date).timestamp()), "open": float(row.Open), "high": float(row.High),
                 "low": float(row.Low), "close": float(row.Close),
                 "volume": float(getattr(row, "Volume", 0) or 0)} for row in df.itertuples()]
        return _finish(bars, tf, base, count)


# --------------------------------------------------------------------------- feed
@dataclass
class ProviderState:
    status: str = "unknown"  # unknown | ok | down | unavailable
    failures: int = 0
    last_error: str = ""
    checked_at: float = 0.0


@dataclass
class ExternalFeed:
    providers: list[Provider] = field(default_factory=list)
    state: dict[str, ProviderState] = field(default_factory=dict)
    gave_up: bool = False

    @classmethod
    def create(cls) -> "ExternalFeed":
        feed = cls()
        for ctor in (TradingViewProvider, YahooProvider, BinanceProvider, CoinbaseProvider, StooqProvider):
            st = ProviderState()
            try:
                p = ctor()
            except ProviderError as exc:  # e.g. tvdatafeed not installed
                p = ctor.__new__(ctor)
                st.status, st.last_error = "unavailable", str(exc)
            feed.providers.append(p)
            feed.state[p.name] = st
        return feed

    @property
    def tv(self) -> TradingViewProvider | None:
        p = self.providers[0]
        return p if self.state[p.name].status != "unavailable" else None

    def snapshot(self) -> dict:
        return {"gave_up": self.gave_up,
                "providers": [{"name": p.name, "label": p.label, **vars(self.state[p.name])} for p in self.providers]}

    def _usable(self) -> list[Provider]:
        return [p for p in self.providers if self.state[p.name].status in ("ok", "unknown")]

    def _ok(self, p: Provider) -> None:
        st = self.state[p.name]
        st.status, st.failures, st.last_error, st.checked_at = "ok", 0, "", time.time()

    def _fail(self, p: Provider, exc: Exception) -> None:
        st = self.state[p.name]
        st.failures += 1
        st.last_error, st.checked_at = str(exc)[:200], time.time()
        if st.failures >= MAX_FAILURES:
            st.status = "down"
            log.warning("external provider %s marked DOWN: %s", p.name, exc)
        if not self._usable():
            self.gave_up = True
            log.error("all external providers down - giving up until manual retry")

    async def probe_all(self) -> dict:
        """One parallel health check at startup. Probe failures count fully (=down), so a
        site that blocks bots is tried exactly once and then left alone."""
        async def one(p: Provider) -> None:
            st = self.state[p.name]
            if st.status == "unavailable":
                return
            try:
                await asyncio.wait_for(asyncio.to_thread(p.probe), timeout=TIMEOUT + 5)
                self._ok(p)
            except Exception as exc:
                st.failures = MAX_FAILURES - 1
                self._fail(p, exc)
        await asyncio.gather(*(one(p) for p in self.providers))
        self.gave_up = not self._usable()
        return self.snapshot()

    async def retry(self) -> dict:
        """User-initiated only."""
        for st in self.state.values():
            if st.status != "unavailable":
                st.status, st.failures = "unknown", 0
        self.gave_up = False
        return await self.probe_all()

    async def history(self, symbol: str, tf: Timeframe, count: int,
                      prefer: str | None = None) -> tuple[str, str, list[dict]]:
        """Returns (provider name, provider symbol, bars). Tries providers in priority order."""
        if self.gave_up:
            raise ProviderError("all external data sources are down - press Retry in the status menu")
        order = self._usable()
        if prefer:
            order.sort(key=lambda p: p.name != prefer)
        errors = []
        for p in order:
            try:
                native = await asyncio.to_thread(p.map, symbol)
            except Exception as exc:
                self._fail(p, exc)
                errors.append(f"{p.label}: {exc}")
                continue
            if not native:
                continue
            try:
                bars = await asyncio.wait_for(asyncio.to_thread(p.history, native, tf, count), timeout=TIMEOUT + 10)
            except ProviderError as exc:
                # "symbol/timeframe not supported here" is not an outage - don't count it.
                errors.append(f"{p.label}: {exc}")
                continue
            except Exception as exc:
                self._fail(p, exc)
                errors.append(f"{p.label}: {exc}")
                continue
            if bars:
                self._ok(p)
                return p.name, native, bars
            errors.append(f"{p.label}: empty")
        raise ProviderError("; ".join(errors) or f"no external source supports {symbol} {tf.label}")
