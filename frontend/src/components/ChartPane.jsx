import { useEffect, useRef } from 'react';
import { ChartController } from '../chart/ChartController.js';
import { DrawingLayer } from '../chart/DrawingLayer.js';
import { on, request } from '../lib/ws.js';
import { useStore } from '../lib/store.js';
import { parseTimeframe } from '../lib/timeframes.js';
import { fmtPrice } from '../lib/format.js';
import { tradeRequest } from '../lib/trading.js';

const PROVIDER_LABEL = { tradingview: 'TradingView', yahoo: 'Yahoo', binance: 'Binance', coinbase: 'Coinbase', stooq: 'Stooq' };

export default function ChartPane({ chart, active, canMinimize }) {
  const elRef = useRef(null);
  const ctrlRef = useRef(null);
  const layerRef = useRef(null);
  const legendRef = useRef(null);
  const metaRef = useRef({});
  const meta = useStore((s) => s.chartMeta[chart.id]) || {};
  const wsEpoch = useStore((s) => s.wsEpoch);
  const positions = useStore((s) => s.positions);
  const orders = useStore((s) => s.orders);
  const customs = useStore((s) => s.customIndicators);
  const brokerClock = useStore((s) => s.settings.brokerClock);
  const drawingTool = useStore((s) => s.drawingTool);
  const { setActive, setChartMeta, updateChart, minimize, addChartAfter, removeChart, openModal, setDrawingTool } = useStore.getState();

  // ---- mount chart + drawing overlay
  useEffect(() => {
    const ctrl = new ChartController(elRef.current, {
      onCrosshair: (bar) => {
        const el = legendRef.current;
        if (!el || !bar) return;
        const d = metaRef.current.digits ?? 2;
        const chg = bar.close - bar.open;
        el.innerHTML = `O <b>${fmtPrice(bar.open, d)}</b> H <b>${fmtPrice(bar.high, d)}</b> L <b>${fmtPrice(bar.low, d)}</b> `
          + `C <b class="${chg >= 0 ? 'up' : 'down'}">${fmtPrice(bar.close, d)}</b>`;
      },
      onContextMenu: (info) => {
        setActive(chart.id);
        openModal('context', { ...info, chartId: chart.id, meta: metaRef.current });
      },
      onTradeCommit: (type, payload, revert) => {
        const what = type === 'modify_sltp' ? `#${payload.ticket} SL ${payload.sl || '—'} / TP ${payload.tp || '—'}`
          : `order #${payload.ticket} @ ${payload.price} SL ${payload.sl || '—'} TP ${payload.tp || '—'}`;
        tradeRequest(type, payload, `Modified ${what}`).catch(revert);
      },
    });
    ctrlRef.current = ctrl;
    (window.__charts ||= {})[chart.id] = ctrl; // debug/e2e handle
    layerRef.current = new DrawingLayer(elRef.current, ctrl, { onToolDone: () => setDrawingTool('cursor') });
    return () => { delete window.__charts[chart.id]; layerRef.current.destroy(); ctrl.destroy(); };
  }, []);

  // ---- data subscription (dual feed routing happens server-side)
  useEffect(() => {
    const tf = parseTimeframe(chart.timeframe);
    const ctrl = ctrlRef.current;
    let live = true;
    if (!tf) { setChartMeta(chart.id, { error: `bad timeframe ${chart.timeframe}` }); return undefined; }
    setChartMeta(chart.id, { loading: true, error: null });
    request('subscribe', { chartId: chart.id, symbol: chart.symbol, timeframe: tf.label,
      count: tf.kind === 'tick' || tf.seconds < 60 ? 1500 : 3000 }, 60000)
      .then((data) => {
        if (!live) return;
        const m = { symbol: data.symbol, source: data.source, tradable: data.source === 'mt5' && !!data.tradable,
          digits: data.digits ?? 2, provider: data.provider, native: data.native, description: data.description,
          loading: false, error: null };
        metaRef.current = m;
        ctrl.setData(data.bars, { ...m, tf });
        layerRef.current.setSymbol(data.symbol);
        setChartMeta(chart.id, m);
      })
      .catch((err) => { if (live) setChartMeta(chart.id, { loading: false, error: err.message }); });

    const offTicks = on('ticks', (msg) => {
      if (metaRef.current.source === 'mt5' && msg.symbol === metaRef.current.symbol) ctrl.applyTicks(msg.ticks);
    });
    const offBar = on('bar', (msg) => { if (msg.chartId === chart.id) ctrl.applyBars(msg.bars); });
    return () => {
      live = false;
      offTicks(); offBar();
      request('unsubscribe', { chartId: chart.id }).catch(() => {});
    };
  }, [chart.symbol, chart.timeframe, wsEpoch]);

  useEffect(() => { ctrlRef.current.setChartType(chart.chartType); }, [chart.chartType]);
  useEffect(() => { ctrlRef.current.setIndicators(chart.indicators, customs, brokerClock); },
    [chart.indicators, customs, brokerClock, meta.source]);
  useEffect(() => { ctrlRef.current.setTradeLevels(positions, orders); }, [positions, orders, meta.symbol]);
  useEffect(() => { layerRef.current.setTool(active ? drawingTool : 'cursor'); }, [drawingTool, active]);

  const badge = meta.source === 'mt5'
    ? <span className="badge mt5" title="Live MT5 broker feed - trading enabled">MT5 · tradable</span>
    : meta.source === 'external'
      ? <span className="badge ext" title="External feed - charting only, MT5 trading disabled">{PROVIDER_LABEL[meta.provider] || 'External'} · chart only</span>
      : null;

  return (
    <div className={`pane ${active ? 'active' : ''}`} onMouseDown={() => setActive(chart.id)}>
      <div className="pane-head">
        <b className="pane-sym">{meta.symbol || chart.symbol}</b>
        <span className="muted">{chart.timeframe}</span>
        {badge}
        <span className="legend" ref={legendRef} />
        <span className="pane-actions">
          <button type="button" title="Indicators" onClick={() => { setActive(chart.id); openModal('indicators'); }}>ƒx</button>
          <button type="button" title="Add a chart beside this one" onClick={() => addChartAfter(chart.id)}>＋</button>
          <button type="button" title="Minimise" disabled={!canMinimize} onClick={() => minimize(chart.id)}>—</button>
          <button type="button" title="Close chart" onClick={() => removeChart(chart.id)}>✕</button>
        </span>
      </div>
      <div className="pane-body">
        <div className="chart-host" ref={elRef} />
        {meta.loading && <div className="pane-overlay">Loading {chart.symbol}…</div>}
        {meta.error && (
          <div className="pane-overlay error">
            <div>{meta.error}</div>
            <button type="button" className="btn" onClick={() => updateChart(chart.id, { timeframe: chart.timeframe === 'M5' ? 'M15' : 'M5' })}>
              Try another timeframe
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
