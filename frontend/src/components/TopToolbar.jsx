import { useState } from 'react';
import { PINNED, useStore } from '../lib/store.js';
import { PRESET_TIMEFRAMES, parseTimeframe } from '../lib/timeframes.js';
import { DRAWING_TOOLS } from '../chart/DrawingLayer.js';
import { fmtMoney } from '../lib/format.js';
import { placeOrder } from '../lib/trading.js';

const CHART_TYPES = [
  { id: 'candles', label: 'Candles', icon: '🕯' },
  { id: 'bars', label: 'Bars', icon: '┤' },
  { id: 'line', label: 'Line', icon: '⌇' },
  { id: 'area', label: 'Area', icon: '◭' },
];
const LAYOUTS = [1, 2, 3, 4, 6, 8];

function Dot({ state }) {
  return <span className={`dot dot-${state}`} />;
}

export default function TopToolbar() {
  const s = useStore();
  const chart = s.activeChart();
  const meta = (chart && s.chartMeta[chart.id]) || {};
  const [symbolInput, setSymbolInput] = useState('');
  const [tfInput, setTfInput] = useState('');
  const visible = s.charts.filter((c) => !c.minimized).length;

  const mt5 = s.status.mt5 || {};
  const mt5State = !s.wsConnected ? 'down' : mt5.status === 'ok' ? 'ok' : mt5.status === 'down' ? 'down' : 'warn';
  const ext = s.status.external || { providers: [] };
  const tvProv = ext.providers?.find((p) => p.name === 'tradingview');
  const tvState = ext.gave_up ? 'down' : tvProv?.status === 'ok' ? 'ok' : tvProv?.status === 'down' ? 'warn' : 'warn';
  const tradable = !!meta.tradable && mt5.connected;

  const applySymbol = (e) => {
    e.preventDefault();
    if (symbolInput.trim() && chart) s.updateChart(chart.id, { symbol: symbolInput.trim().toUpperCase() });
    setSymbolInput('');
  };
  const applyTf = (e) => {
    e.preventDefault();
    const tf = parseTimeframe(tfInput);
    if (!tf) { s.toast('warn', `Unknown timeframe "${tfInput}". Try 1s, 7s, 3m, H2, 250T`); return; }
    s.updateChart(chart.id, { timeframe: tf.label });
    setTfInput('');
  };
  const quick = (side) => {
    if (!chart) return;
    const order = { symbol: meta.symbol || chart.symbol, side, kind: 'market', volume: s.settings.defaultVolume };
    if (s.settings.oneClick) placeOrder(order);
    else s.openModal('order', { order });
  };

  return (
    <header className="toolbar">
      <div className="tb-fixed">
      <div className="tb-group brand" title="MT5 × TradingView terminal">◆ MT5·TV</div>

      <div className="tb-group pinned" role="tablist" aria-label="Main instruments">
        {PINNED.map((p) => (
          <button key={p} type="button" className={`tab ${chart?.symbol === p ? 'on' : ''}`}
            onClick={() => chart && s.updateChart(chart.id, { symbol: p })}>{p}</button>
        ))}
      </div>

      <form className="tb-group" onSubmit={applySymbol}>
        <input className="sym-input" placeholder={chart?.symbol || 'Symbol'} value={symbolInput}
          onChange={(e) => setSymbolInput(e.target.value)} aria-label="Symbol search (e.g. AAPL, NASDAQ:NDX, BTCUSDT)" />
      </form>
      </div>

      <div className="tb-scroll">
      <div className="tb-group">
        {PRESET_TIMEFRAMES.map((tf) => (
          <button key={tf} type="button" className={`btn-sm ${parseTimeframe(chart?.timeframe || '')?.label === parseTimeframe(tf).label ? 'on' : ''}`}
            onClick={() => s.updateChart(chart.id, { timeframe: tf })}>{tf}</button>
        ))}
        <form onSubmit={applyTf}>
          <input className="tf-input" placeholder="custom" value={tfInput} onChange={(e) => setTfInput(e.target.value)}
            aria-label="Custom timeframe" />
        </form>
      </div>

      <div className="tb-group">
        {CHART_TYPES.map((t) => (
          <button key={t.id} type="button" title={t.label} className={`btn-icon ${chart?.chartType === t.id ? 'on' : ''}`}
            onClick={() => s.updateChart(chart.id, { chartType: t.id })}>{t.icon}</button>
        ))}
      </div>

      <div className="tb-group" title="Layout (visible charts)">
        {LAYOUTS.map((n) => (
          <button key={n} type="button" className={`btn-sm ${visible === n ? 'on' : ''}`} onClick={() => s.setLayoutCount(n)}>
            {n === 4 ? '⊞' : n}
          </button>
        ))}
      </div>

      <div className="tb-group">
        {DRAWING_TOOLS.map((t) => (
          <button key={t.id} type="button" title={t.label} className={`btn-icon ${s.drawingTool === t.id ? 'on' : ''}`}
            onClick={() => s.setDrawingTool(t.id)}>{t.icon}</button>
        ))}
        <button type="button" className="btn" onClick={() => s.openModal('indicators')}>ƒx Indicators</button>
        <button type="button" className={`btn-icon ${s.settings.sidePanel === 'explorer' ? 'on' : ''}`} title="EA & indicator explorer"
          onClick={() => s.setSettings({ sidePanel: s.settings.sidePanel === 'explorer' ? null : 'explorer' })}>📁</button>
      </div>

      <div className="tb-group trade" title={tradable ? 'Market order on the active chart' : 'Trading disabled: external (chart-only) symbol or MT5 offline'}>
        <input type="number" className="lot" min="0.01" step="0.01" value={s.settings.defaultVolume}
          onChange={(e) => s.setSettings({ defaultVolume: Number(e.target.value) })} aria-label="Lots" />
        <button type="button" className="sell" disabled={!tradable} onClick={() => quick('sell')}>SELL</button>
        <button type="button" className="buy" disabled={!tradable} onClick={() => quick('buy')}>BUY</button>
        <label className="oneclick" title="Skip the confirmation dialog">
          <input type="checkbox" checked={s.settings.oneClick} onChange={(e) => s.setSettings({ oneClick: e.target.checked })} /> 1-click
        </label>
      </div>

      {s.account && (
        <div className="tb-group">
          <span className="acct" title={`${s.account.name} · ${s.account.server}`}>
            Bal {fmtMoney(s.account.balance)} · Eq <b className={s.account.profit >= 0 ? 'up' : 'down'}>{fmtMoney(s.account.equity)}</b>
          </span>
        </div>
      )}
      </div>

      <div className="tb-group right">
        <button type="button" className="pill" onClick={() => s.openModal('mt5')}>
          <Dot state={mt5State} /> MT5{mt5.sim ? ' (SIM)' : ''}
        </button>
        <button type="button" className="pill" onClick={() => s.openModal('tv')}>
          <Dot state={tvState} /> TV{s.status.tv?.username ? ` · ${s.status.tv.username}` : ''}
        </button>
        <button type="button" className="pill" title="Data sources & settings" onClick={() => s.openModal('status')}>⚙</button>
        <button type="button" className="pill" title="Toggle trade panel" onClick={() => s.setSettings({ showPanel: !s.settings.showPanel })}>▤</button>
      </div>
    </header>
  );
}
