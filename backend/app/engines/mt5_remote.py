"""Drop-in stand-in for the `MetaTrader5` module when MT5 runs in the Wine container.

Every call executes inside the container's Windows Python, where results (namedtuples,
numpy record arrays) are flattened to plain Python before crossing the wire, so one
call == one network round-trip and no Windows-only types ever reach Linux.
Requires only `rpyc` (same major version on both sides).
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import rpyc

_REMOTE = r'''
import MetaTrader5 as _mt5
from datetime import datetime as _dt, timezone as _tz

def _dec(v):
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "__dict__":
        return {k: _dec(x) for k, x in v[1]}
    if isinstance(v, tuple) and len(v) == 2 and v[0] == "__ts__":
        return _dt.fromtimestamp(v[1], tz=_tz.utc)
    return v

def _plain(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if hasattr(v, "_asdict"):
        return {"__nt__": 1, **{k: _plain(x) for k, x in v._asdict().items()}}
    names = getattr(getattr(v, "dtype", None), "names", None)
    if names:
        return {"__rows__": [dict(zip(names, row)) for row in v.tolist()]}
    if isinstance(v, (tuple, list)):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if hasattr(v, "item"):
        return v.item()
    return str(v)

def _call(name, args, kwargs):
    fn = getattr(_mt5, name)
    return _plain(fn(*[_dec(a) for a in args], **{k: _dec(x) for k, x in kwargs}))
'''


class Record:
    """Attribute + `_asdict()` access, like the MetaTrader5 namedtuples."""

    def __init__(self, data: dict) -> None:
        self.__dict__.update(data)

    def _asdict(self) -> dict:
        return dict(self.__dict__)

    def __repr__(self) -> str:
        return f"Record({self.__dict__})"


def _enc(v: Any) -> Any:
    if isinstance(v, dict):
        return ("__dict__", tuple((k, _enc(x)) for k, x in v.items()))
    if isinstance(v, datetime):
        return ("__ts__", v.timestamp())
    return v


def _wrap(v: Any) -> Any:
    if isinstance(v, dict):
        if v.pop("__nt__", None):
            return Record({k: _wrap(x) for k, x in v.items()})
        if "__rows__" in v:
            return v["__rows__"]  # list of dicts; indexable like the numpy record array
        return {k: _wrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return tuple(_wrap(x) for x in v)
    return v


class RemoteMT5:
    def __init__(self, host: str, port: int = 18812) -> None:
        self.host, self.port = host, port
        self._lock = threading.Lock()
        self._conn = None
        self._consts: dict[str, Any] = {}

    def _connection(self):
        if self._conn is None or self._conn.closed:
            conn = rpyc.classic.connect(self.host, self.port, keepalive=True)
            conn._config["sync_request_timeout"] = 300
            conn.execute(_REMOTE)
            self._conn = conn
        return self._conn

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name.isupper():  # constants: TIMEFRAME_M1, ORDER_TYPE_BUY, ...
            if name not in self._consts:
                with self._lock:
                    self._consts[name] = rpyc.classic.obtain(self._connection().eval(f"_mt5.{name}"))
            return self._consts[name]

        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                try:
                    conn = self._connection()
                    res = conn.namespace["_call"](name, tuple(_enc(a) for a in args),
                                                  tuple((k, _enc(v)) for k, v in kwargs.items()))
                    return _wrap(rpyc.classic.obtain(res))
                except (EOFError, ConnectionError, OSError):
                    self._conn = None  # container restarted - reconnect on next call
                    raise
        return call
