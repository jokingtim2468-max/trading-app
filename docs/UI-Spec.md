---
tags: [mt5-tv-terminal, ui-spec, colors]
updated: 2026-10-08
---
# MT5 TV Terminal - UI spec

Source of truth in code: `frontend/src/lib/theme.js` (JS) and the `:root` block in `frontend/src/styles.css` (CSS).

## Colours (dark, TradingView-style)
| Token | Hex | Used for |
|---|---|---|
| `--bg` | `#131722` | app + chart background |
| `--panel` | `#1e222d` | toolbar, sidebar, panels, modals |
| `--panel-alt` / `--border` | `#2a2e39` | buttons, borders, splitters |
| `--hover` | `#2f3341` | hover state |
| `--text` | `#d1d4dc` | primary text |
| `--muted` | `#787b86` | labels, secondary text |
| `--accent` | `#2962ff` | active tab, primary buttons, manual positions |
| `--up` | `#26a69a` | bullish candles, BUY, TP lines, healthy status, DEMO badge |
| `--down` | `#ef5350` | bearish candles, SELL, SL lines, errors, LIVE badge |
| `--ea` | `#f7a600` | EA-owned positions/orders (magic > 0), VWAP |
| `--warn` | `#ff9800` | warnings, external (chart-only) badge |

## Layout rules
- Top bar 40 px: pinned **NAS100 · US500 · XAUUSD** tabs and status pills never scroll; tool groups in between scroll horizontally.
- Left sidebar 56 px: 📈 Charts · 🤖 AI Agents · 📁 EAs & indicators.
- Chart grid: 1 · 2 · **3 side by side** · **4 = 2×2** · 5-6 = 3+2/3+3 · 7-8 = 4+3/4+4. Splitters 4 px, drag to resize (min 12 %), double-click resets.
- Each chart header: symbol · timeframe · source badge (MT5 tradable / provider chart-only) · OHLC legend · ƒx · ＋ add beside · — minimise · ✕ close.
- Right side panels: Explorer 280 px, AI Agents 380 px. Bottom trade panel 200 px.

## Price-line conventions
- Entry: solid 2 px, amber for EA (`EA - 555123`), blue for manual; title shows side, volume, live P/L.
- SL: red dashed · TP: green dashed · pending orders: large-dash grey (EA pending amber).
- Draggable lines show a `ns-resize` cursor within 6 px.

## Typography
`-apple-system, BlinkMacSystemFont, 'Trebuchet MS', Roboto, Ubuntu, sans-serif` 12 px; code `JetBrains Mono, Consolas`.
