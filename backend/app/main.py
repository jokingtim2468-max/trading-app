"""FastAPI entrypoint: one WebSocket (/ws) carrying requests, responses and pushes."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .engines.base import TradeError
from . import explorer
from .agents.orchestrator import AgentSystem
from .external import ExternalFeed, ProviderError
from .hub import Client, Hub, Sub
from .timeframes import parse_timeframe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("bridge")

# Common broker names for the two instruments this terminal is built around.
ALIASES = {
    "NAS100": ["NAS100", "USTEC", "US100", "NDX100", "NQ100", "USTECH", "NASDAQ100"],
    "US500": ["US500", "SPX500", "SP500", "US500Cash", "SPX", "USA500"],
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.", "XAU/USD"],
}
ALIAS_LOOKUP = {a.upper(): k for k, names in ALIASES.items() for a in names + [k]}
EXTERNAL_ALIAS = {"NAS100": "NDX", "US500": "SPX", "XAUUSD": "XAUUSD"}  # TV / Yahoo names
PINNED = ["NAS100", "US500", "XAUUSD"]


def make_engine():
    if settings.force_sim:
        from .engines.sim_engine import SimEngine
        return SimEngine(settings.max_deviation)
    from .engines import mt5_engine
    if settings.mt5_rpyc_host:
        from .engines.mt5_remote import RemoteMT5  # MT5 under Wine in the mt5 container
        mt5_engine.bind_module(RemoteMT5(settings.mt5_rpyc_host, settings.mt5_rpyc_port))
        return mt5_engine.MT5Engine(settings.max_deviation, settings.mt5_path, settings.mt5_portable)
    try:
        import MetaTrader5
    except ImportError:
        log.warning("MetaTrader5 package not available on this OS - running the SIMULATED broker")
        from .engines.sim_engine import SimEngine
        return SimEngine(settings.max_deviation)
    mt5_engine.bind_module(MetaTrader5)
    return mt5_engine.MT5Engine(settings.max_deviation, settings.mt5_path)


async def startup_checks(hub: Hub) -> None:
    """Bring both feeds up at launch and record their health for the UI."""
    engine = hub.engine
    if engine.sim:
        await engine.login("SIM-Demo", 1000001, "")
        hub.mt5_health = {"status": "ok", "error": ""}
    elif settings.mt5_login and settings.mt5_password and settings.mt5_server:
        for attempt in range(1, 6):  # the Wine terminal can take a while to boot
            try:
                await engine.login(settings.mt5_server, int(settings.mt5_login), settings.mt5_password)
                hub.mt5_health = {"status": "ok", "error": ""}
                break
            except Exception as exc:
                hub.mt5_health = {"status": "down", "error": str(exc)}
                log.warning("MT5 auto-login attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(5 * attempt)
    else:
        hub.mt5_health = {"status": "disconnected", "error": "not logged in - open MT5 settings"}

    if settings.tv_sessionid and hub.feed.tv:
        try:
            hub.tv_account = await asyncio.to_thread(hub.feed.tv.auth_session, settings.tv_sessionid,
                                                     settings.tv_sessionid_sign)
        except Exception as exc:
            log.warning("TradingView auto-auth failed: %s", exc)
    snap = await hub.feed.probe_all()
    log.info("external providers: %s", {p["name"]: p["status"] for p in snap["providers"]})
    await hub.push_status()
    for alert in hub.startup_alerts():
        await hub.broadcast(alert)


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub = Hub(make_engine(), ExternalFeed.create())
    app.state.hub = hub
    hub.agents = AgentSystem(hub)
    hub.start()
    hub.agents.start_guardian()
    app.state.startup = asyncio.create_task(startup_checks(hub))
    yield
    await hub.stop()


app = FastAPI(title="MT5 x TradingView Bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    """Always 200 while the bridge is alive; feed health is reported in the body so the
    launcher and the UI can show *which* side is down instead of just failing."""
    hub: Hub = app.state.hub
    return JSONResponse(hub.status())


# ---------------------------------------------------------------------- handlers
Handler = Callable[[Hub, Client, dict], Awaitable[Any]]
HANDLERS: dict[str, Handler] = {}


def handler(name: str):
    def deco(fn: Handler) -> Handler:
        HANDLERS[name] = fn
        return fn
    return deco


@handler("status")
async def h_status(hub: Hub, client: Client, msg: dict) -> dict:
    return hub.status()


@handler("mt5_login")
async def h_mt5_login(hub: Hub, client: Client, msg: dict) -> dict:
    try:
        acct = await hub.engine.login(str(msg["server"]), int(msg["login"]), str(msg["password"]),
                                      msg.get("path") or None)
    except TradeError as exc:
        hub.mt5_health = {"status": "down", "error": str(exc)}
        await hub.push_status()
        raise
    hub.mt5_health = {"status": "ok", "error": ""}
    hub.last_msc.clear()
    await hub.push_status()
    return acct


@handler("mt5_logout")
async def h_mt5_logout(hub: Hub, client: Client, msg: dict) -> dict:
    await hub.engine.logout()
    hub.mt5_health = {"status": "disconnected", "error": "logged out"}
    await hub.push_status()
    return {}


@handler("tv_auth")
async def h_tv_auth(hub: Hub, client: Client, msg: dict) -> dict:
    tv = hub.feed.tv
    if tv is None:
        raise ProviderError("tvdatafeed is not installed in the bridge (pip install -r requirements.txt)")
    if msg.get("sessionid"):
        info = await asyncio.to_thread(tv.auth_session, msg["sessionid"].strip(), msg.get("sessionid_sign", ""))
    else:
        info = await asyncio.to_thread(tv.auth_password, msg["username"], msg["password"])
    hub.tv_account = info
    st = hub.feed.state["tradingview"]
    st.status, st.failures, st.last_error = "unknown", 0, ""
    hub.feed.gave_up = False
    await hub.push_status()
    return info


@handler("tv_watchlists")
async def h_tv_watchlists(hub: Hub, client: Client, msg: dict) -> list:
    if hub.feed.tv is None:
        raise ProviderError("TradingView provider unavailable")
    return await asyncio.to_thread(hub.feed.tv.watchlists)


@handler("external_retry")
async def h_external_retry(hub: Hub, client: Client, msg: dict) -> dict:
    snap = await hub.feed.retry()
    await hub.push_status()
    for alert in hub.startup_alerts():
        await client.send(alert)
    return snap


@handler("resolve")
async def h_resolve(hub: Hub, client: Client, msg: dict) -> dict:
    return await hub.resolve(msg["symbol"])


async def _resolve_with_alias(hub: Hub, symbol: str) -> dict:
    info = await hub.resolve(symbol)
    canon = ALIAS_LOOKUP.get(symbol.strip().upper())
    if info["source"] == "external" and canon:
        for alt in ALIASES[canon]:  # the broker may call NAS100 "USTEC" etc.
            alt_info = await hub.resolve(alt)
            if alt_info["source"] == "mt5":
                return alt_info
        info["symbol"] = EXTERNAL_ALIAS[canon]
    return info


@handler("subscribe")
async def h_subscribe(hub: Hub, client: Client, msg: dict) -> dict:
    chart_id = str(msg["chartId"])
    tf = parse_timeframe(msg.get("timeframe", "M1"))
    count = max(50, min(int(msg.get("count", 1500)), 20_000))
    info = await _resolve_with_alias(hub, msg["symbol"])
    client.subs.pop(chart_id, None)
    if info["source"] == "mt5":
        bars = await hub.engine.history(info["symbol"], tf, count)
        sub = Sub(chart_id, info["symbol"], "mt5", tf)
    else:
        provider, native, bars = await hub.feed.history(info["symbol"], tf, count)
        sub = Sub(chart_id, info["symbol"], "external", tf, provider, native)
        info.update(provider=provider, native=native, digits=_guess_digits(bars))
    client.subs[chart_id] = sub
    return {**info, "timeframe": tf.label, "tfKind": tf.kind, "tfSeconds": tf.seconds, "tfTicks": tf.ticks,
            "bars": bars}


def _guess_digits(bars: list[dict]) -> int:
    if not bars:
        return 2
    p = bars[-1]["close"]
    return 2 if p >= 100 else 4 if p >= 1 else 6


@handler("files_list")
async def h_files_list(hub: Hub, client: Client, msg: dict) -> dict:
    return await asyncio.to_thread(explorer.list_tree, await _data_dir(hub))


@handler("file_read")
async def h_file_read(hub: Hub, client: Client, msg: dict) -> dict:
    return await asyncio.to_thread(explorer.read_file, await _data_dir(hub), str(msg["path"]))


async def _data_dir(hub: Hub):
    return await explorer.resolve_data_dir(hub.engine)


@handler("unsubscribe")
async def h_unsubscribe(hub: Hub, client: Client, msg: dict) -> dict:
    client.subs.pop(str(msg["chartId"]), None)
    return {}


def _require_mt5(hub: Hub) -> None:
    if not hub.engine.connected:
        raise TradeError("MT5 is not connected")


@handler("order")
async def h_order(hub: Hub, client: Client, msg: dict) -> dict:
    _require_mt5(hub)
    info = await hub.resolve(msg["symbol"])
    if info["source"] != "mt5" or not info.get("tradable"):
        raise TradeError(f"{msg['symbol']} is an external (chart-only) symbol - trading disabled")
    if msg.get("side") not in ("buy", "sell") or msg.get("kind") not in ("market", "limit", "stop"):
        raise TradeError("invalid side/kind")
    return await hub.engine.place_order({**msg, "symbol": info["symbol"]})


@handler("modify_sltp")
async def h_modify_sltp(hub: Hub, client: Client, msg: dict) -> dict:
    _require_mt5(hub)
    return await hub.engine.modify_position(int(msg["ticket"]), float(msg.get("sl") or 0), float(msg.get("tp") or 0))


@handler("modify_order")
async def h_modify_order(hub: Hub, client: Client, msg: dict) -> dict:
    _require_mt5(hub)
    return await hub.engine.modify_order(int(msg["ticket"]), float(msg.get("price") or 0),
                                         float(msg.get("sl") or 0), float(msg.get("tp") or 0))


@handler("close")
async def h_close(hub: Hub, client: Client, msg: dict) -> dict:
    _require_mt5(hub)
    return await hub.engine.close_position(int(msg["ticket"]), msg.get("volume"))


@handler("cancel_order")
async def h_cancel(hub: Hub, client: Client, msg: dict) -> dict:
    _require_mt5(hub)
    return await hub.engine.cancel_order(int(msg["ticket"]))


# ---------------------------------------------------------------------- AI trading agents
# Agent account profiles live in memory only (never written to disk or logged).
_AGENT_PROFILES: dict[str, dict] = {}


@handler("agents_status")
async def h_agents_status(hub: Hub, client: Client, msg: dict) -> dict:
    return hub.agents.status()


@handler("agents_start")
async def h_agents_start(hub: Hub, client: Client, msg: dict) -> dict:
    return await hub.agents.start()


@handler("agents_stop")
async def h_agents_stop(hub: Hub, client: Client, msg: dict) -> dict:
    return await hub.agents.stop()


@handler("agents_config")
async def h_agents_config(hub: Hub, client: Client, msg: dict) -> dict:
    patch = dict(msg.get("config") or {})
    if "demo_mode" in patch and hub.agents.running:
        raise ValueError("stop the agents before switching between Demo and Live")
    st = hub.agents.update_config(patch)
    profile = _AGENT_PROFILES.get("demo" if hub.agents.config.demo_mode else "live")
    if "demo_mode" in patch and profile:  # route the agents to the matching saved account
        await hub.agents.executor.connect_mt5(profile["server"], profile["login"], profile["password"])
        hub.mt5_health = {"status": "ok", "error": ""}
        await hub.push_status()
        st = hub.agents.status()
    await hub.broadcast(st)
    return st


@handler("agents_account")
async def h_agents_account(hub: Hub, client: Client, msg: dict) -> dict:
    kind = msg.get("profile")
    if kind not in ("demo", "live"):
        raise ValueError("profile must be demo or live")
    if hub.agents.running:
        raise ValueError("stop the agents before changing accounts")
    prof = {"server": str(msg["server"]), "login": int(msg["login"]), "password": str(msg["password"])}
    acct = await hub.agents.executor.connect_mt5(prof["server"], prof["login"], prof["password"])
    if kind == "demo" and acct.get("account_type") != "demo":
        raise ValueError(f"that account is '{acct.get('account_type')}', not a demo account")
    _AGENT_PROFILES[kind] = prof
    hub.mt5_health = {"status": "ok", "error": ""}
    await hub.push_status()
    hub.agents.update_config({"demo_mode": kind == "demo", "live_confirmed": bool(msg.get("live_confirmed"))})
    return {"account": acct, "profiles": {k: {"server": v["server"], "login": v["login"]}
                                          for k, v in _AGENT_PROFILES.items()}, **hub.agents.status()}


@handler("agents_run_now")
async def h_agents_run_now(hub: Hub, client: Client, msg: dict) -> list:
    ok, why = await hub.agents.executor.account_gate()
    if not ok:
        raise ValueError(why)
    return await hub.agents.run_cycle()


@handler("agents_new_challenge")
async def h_agents_new_challenge(hub: Hub, client: Client, msg: dict) -> dict:
    if hub.agents.running:
        raise ValueError("stop the agents first")
    if msg.get("confirm") != "RESET":
        raise ValueError("type RESET to confirm starting a new challenge phase")
    return hub.agents.new_challenge(int(msg.get("phase", 1)))


# ---------------------------------------------------------------------- websocket
def _origin_allowed(origin: str | None) -> bool:
    """Block cross-site WebSocket hijacking: a random web page must not be able to
    connect to ws://127.0.0.1 and send orders from your browser."""
    if not origin:
        return True  # non-browser clients (scripts, tests)
    if origin in settings.allowed_origins:
        return True
    host = urlparse(origin).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if not _origin_allowed(ws.headers.get("origin")):
        await ws.close(code=4403)
        return
    if settings.api_token and not secrets.compare_digest(ws.query_params.get("token", ""), settings.api_token):
        await ws.close(code=4401)
        return
    await ws.accept()
    hub: Hub = app.state.hub
    client = Client(ws)
    hub.clients.add(client)
    await client.send(hub.status())
    for alert in hub.startup_alerts():
        await client.send(alert)
    hub._positions_sig = ""  # force a fresh positions snapshot for the new client

    async def run(msg: dict) -> None:
        rid, kind = msg.get("id"), msg.get("type")
        fn = HANDLERS.get(kind)
        try:
            if fn is None:
                raise ValueError(f"unknown request type {kind!r}")
            data = await fn(hub, client, msg)
            reply = {"type": "response", "id": rid, "ok": True, "data": data}
        except (TradeError, ProviderError, ValueError, KeyError) as exc:
            reply = {"type": "response", "id": rid, "ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("handler %s failed", kind)
            reply = {"type": "response", "id": rid, "ok": False, "error": f"internal error: {exc}"}
        try:
            await client.send(reply)
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            asyncio.create_task(run(msg))
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.discard(client)


if settings.frontend_dist.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="ui")


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
