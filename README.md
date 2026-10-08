# MT5 × TradingView Terminal

A self-hosted charting and trading terminal built around **NAS100, US500 and XAUUSD**:

- **Charts:** TradingView's open-source Lightweight Charts v5.
- **Execution:** live trading through **MetaTrader 5**, including EA synchronization by magic number.
- **External data:** **TradingView** with automatic public-data fallbacks.
- **AI agents:** a two-agent trading system with hardcoded **FTMO 10k** guardrails.

> **One-command Windows install** (PowerShell):
> ```powershell
> irm https://raw.githubusercontent.com/jokingtim2468-max/trading-app/main/install.ps1 | iex
> ```
> From **Command Prompt** (normal or *Run as administrator*):
> ```bat
> powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; irm https://raw.githubusercontent.com/jokingtim2468-max/trading-app/main/install.ps1 | iex"
> ```
> It downloads the latest signed-checksum installer from GitHub Releases, verifies the SHA-256 and opens the setup wizard.

---

## What's inside

```
trading-app/
├── backend/                  Python FastAPI WebSocket bridge
│   ├── app/main.py           /ws protocol, handlers, startup health checks
│   ├── app/hub.py            dual-feed routing, MT5 tick stream, EA position sync, health monitor
│   ├── app/external.py       TradingView + Yahoo + Binance + Coinbase + Stooq, give-up policy
│   ├── app/engines/          mt5_engine (MetaTrader5 lib) · mt5_remote (Docker/Wine RPC) · sim_engine
│   ├── app/agents/           AI Trading Agents (analyst, executor/risk manager, FTMO rules, orchestrator)
│   ├── app/explorer.py       read-only MQL5 file explorer (Experts / Indicators / Scripts / Presets)
│   └── tests/                pytest suite (bridge, FTMO rules, Agent 2 vs. simulated broker)
├── frontend/                 React + Vite + Lightweight Charts v5
│   └── src/chart/            ChartController (series, price lines, drag-to-modify) · DrawingLayer (canvas)
│   └── src/indicators/       unlimited indicator engine + High/Low projection set + custom JS indicators
├── docker/mt5/               MT5 terminal + Windows Python headless under Wine (Xvfb)
├── docker/bridge/            bridge + built UI, read-only non-root container
├── docker-compose.yml        private network: only the bridge can reach MT5; UI on 127.0.0.1 only
├── launcher/                 hidden Windows launcher with auto-updater
├── installer/                Inno Setup wizard script + icon
└── install.ps1               one-command installer
```

## Features

| Area | What you get |
|---|---|
| Top bar | Pinned **NAS100 · US500 · XAUUSD** tabs, symbol search, timeframes incl. **1s/5s/15s** and tick charts (`100T`), custom timeframes (`7s`, `3m`, `H2`, `250T`), chart type, layout 1–8, drawing tools, indicators, MT5 / TV account pills, quick BUY/SELL |
| Layout | 3 charts side by side, 4 = 2×2 grid, up to 8; **drag any border** to resize (double-click resets); per-chart **minimise** and **add-beside** buttons; minimised charts live in the dock |
| Dual feed | `EURUSD`, `NAS100`, … resolve in MT5 first (broker aliases like `USTEC`, `US100`, `SPX500`, `GOLD` are matched). Anything else (`AAPL`, `NASDAQ:NDX`, `BTCUSDT`) falls back to TradingView → Yahoo → Binance → Coinbase → Stooq and is **chart-only** (trade buttons disabled) |
| Feed health | Each external source is probed once at startup; a source that fails 3× is marked down and never retried automatically. If all 5 fail, the app stops trying and shows a notification (manual **Retry** in ⚙) |
| EA sync | `positions_get()`/`orders_get()` every 250 ms; MT5 is the source of truth. Entry/SL/TP drawn as price lines labelled **`EA - 555123`** or **`Manual`** |
| Drag-to-modify | Drag an SL/TP line → `TRADE_ACTION_SLTP`; drag a pending order → `TRADE_ACTION_MODIFY`; drag the entry line of a position to create a missing SL/TP. Esc cancels |
| Right-click | Buy/Sell Limit/Stop at the clicked price, close position, remove SL/TP, cancel order |
| Indicators | No limit. High/Low projection set: Previous-day H/L/C, ADR projected high/low, NY Opening Range + targets, Initial Balance + extensions, Overnight H/L, Camarilla/Classic/Fib pivots, session VWAP ±σ. Plus SMA/EMA/WMA/HMA, Supertrend, BB, Keltner, Donchian, Volume, RSI, MACD, Stoch, ATR, CCI, %R, OBV and your own JS indicators |
| Drawings | Trend line, ray, horizontal line, zone, Fibonacci, eraser - saved per symbol |
| Explorer | 📁 sidebar: browse your MT5 `MQL5/Experts`, `Indicators`, `Scripts`, `Presets` (read-only source viewer) + the app's indicator library |
| AI agents | 🤖 sidebar tab - see below |

## AI Trading Agents

```
 Agent 1  Market Analyst ──proposal {symbol, direction, entry, SL, TP}──▶ Agent 2  Execution & Risk Manager ──▶ MT5
   tools: fetch_custom_tv_data · get_market_conditions                     position sizing (order_calc_profit/margin)
          scrape_external_news · get_economic_calendar                     FTMO 10k firewall + 1-second guardian
```

- **Agent 1** uses Claude (`claude-opus-5-5`, tool calling, server-side refusal fallback enabled) when an Anthropic API key is set (`secrets/anthropic_api_key.txt` or `ANTHROPIC_API_KEY`). Without a key it uses a transparent rule-based analyst (EMA pullback + ADR targets), so the pipeline always works.
- **Agent 2** is the only component that sends agent orders (magic **880001**). It re-validates and re-sizes every proposal. Every second it runs the guardian, which closes **all** positions on the account (agent, EA and manual) when a limit is approached. FTMO limits are account-level, so the guardian cannot be selective.
- **Demo Mode** (account modal): agents trade only if the connected MT5 account reports `demo`. It uses the same code path and the same order latency as live trading. Live mode requires a real account and an explicit confirmation.

### FTMO 10k Standard guardrails (hardcoded in `backend/app/agents/ftmo.py`)

| Rule | Limit | What Agent 2 does |
|---|---|---|
| Balance / leverage | $10,000 · 1:100 | margin check caps usage at 50 % of free margin |
| Max daily loss 5 % | $500 vs. balance at midnight CE(S)T, floating P/L included | at **−$480**: market-close everything, cancel orders, lock until the Prague-midnight reset |
| Max loss 10 % | equity must stay above $9,000 | at **$9,020**: close everything and **halt** permanently (only *New challenge phase* re-arms) |
| Profit target | Phase 1 +$1,000 · Phase 2 +$500 | close and halt when reached |
| Min trading days | 4 | days with an executed trade are tracked; if the target is hit early, one min-lot "day-count" trade per missing day |
| Standard = intraday | no weekend holding | flatten Friday 16:30 New York, no new entries from 15:45 Friday until the Sunday re-open |
| Per-trade sizing | ≤ $100 risk (default $50), R:R ≥ 1 | worst case (every open SL + the new one) must stay inside the daily and overall headroom |

## Running it

### Option A - Windows, one command (recommended)

1. Open **PowerShell** (no admin needed) and paste the one-liner at the top of this file.
2. Choose a mode in the wizard:
   - **Native (recommended):** runs the bridge invisibly with a bundled Python and your installed MT5 terminal. There is nothing else to install.
   - **Docker (experimental):** MT5 runs headless in a container and needs [Docker Desktop](https://www.docker.com/products/docker-desktop/).
3. Launch from the desktop shortcut **MT5 TV Terminal**. On every start the launcher:
   - checks GitHub for a newer release and, if there is one, verifies the SHA-256 and silently upgrades
   - starts MT5 and the bridge in the background
   - waits until they're healthy, then opens the app in its own window. Problems appear as tray notifications.
4. In the app, click **MT5** → enter your broker server, login and password. Optionally click **TV** and paste your TradingView `sessionid`.

Windows SmartScreen may warn on first run because the installer is not code-signed. The checksum verification in `install.ps1` and in the updater protects against a tampered download.

### Option B - Docker on any OS

```bash
git clone https://github.com/jokingtim2468-max/trading-app && cd trading-app
for f in mt5_login mt5_password mt5_server tv_sessionid anthropic_api_key; do touch secrets/$f.txt; done
docker compose up -d --build        # first build ~5-15 min (WineHQ + MT5)
open http://127.0.0.1:8765
```
The containers are hardened like this:
- **Credentials:** optional auto-login via `secrets/*.txt` (one value per file); these files are never committed.
- **Network:** the MT5 RPC port sits on an internal Docker network that only the bridge joins. The UI binds to `127.0.0.1` only.
- **Privileges:** both containers drop all Linux capabilities and set `no-new-privileges`. The bridge container is read-only.

### Option C - Development

```bash
# backend (Linux/macOS run the simulated broker automatically; Windows uses the real MetaTrader5 package)
cd backend && python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.main                                           # ws://127.0.0.1:8765/ws

# frontend
cd frontend && npm install && npm run dev                    # http://127.0.0.1:5173 (proxies /ws)

# tests
cd backend && pytest -q          # 20 tests: bridge, FTMO rules, Agent 2 vs simulated broker
cd frontend && npm test          # timeframe aggregation + every indicator
```

`BRIDGE_SIM=1` forces the simulator. It runs two toy EAs (magic 555123 and 777001), so EA sync, drag-to-modify and the agents can be tested without a broker. See `backend/.env.example` for every setting.

## Security notes

- The bridge only listens on `127.0.0.1` and **rejects WebSocket connections from other websites' origins**, which blocks cross-site WebSocket hijacking. Optional `BRIDGE_API_TOKEN` adds a token on top.
- MT5 and agent credentials are passed straight to `mt5.login()` and kept in memory only. They are never logged, written to disk or stored in the browser.
- RSS feeds are parsed with `defusedxml`. The MQL5 explorer is read-only and confined to the `MQL5` folder.
- TradingView access uses **your own** login (`sessionid`), so data availability follows your TradingView plan.

## Publishing a release (auto-update source)

Bump `VERSION` and push. The workflow `.github/workflows/installer.yml`:
1. builds the UI on a Windows runner
2. compiles the Inno Setup installer
3. publishes `MT5TVTerminal-Setup-<version>.exe` + `.sha256` as GitHub Release `v<version>`

Installed copies pick it up on their next launch.
