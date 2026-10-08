import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import { BUILTIN_MAP } from '../indicators/builtins.js';
import { newInstance } from '../indicators/engine.js';

export const PINNED = ['NAS100', 'US500', 'XAUUSD'];
export const MAX_CHARTS = 8;

/** Charts per row for N visible charts: 3 = side by side, 4 = 2x2, ... up to 8 = 4x2. */
export const ROWS_FOR = { 1: [1], 2: [2], 3: [3], 4: [2, 2], 5: [3, 2], 6: [3, 3], 7: [4, 3], 8: [4, 4] };

let seq = 0;
const uid = () => `c${Date.now().toString(36)}${(seq++).toString(36)}`;

const starterIndicators = () => ['pdhl', 'vwapbands'].map((id) => newInstance(BUILTIN_MAP[id]));

export const makeChart = (symbol, timeframe = 'M5') => ({
  id: uid(), symbol, timeframe, chartType: 'candles', indicators: starterIndicators(), minimized: false,
});

const equal = (n) => Array(n).fill(1 / n);

export const useStore = create(persist((set, get) => ({
  // ---- persisted UI state
  charts: PINNED.map((s) => makeChart(s)),
  activeId: null,
  fracs: {},                   // `${visibleCount}` -> { rows: [..], cols: [[..], [..]] }
  customIndicators: [],
  settings: { brokerClock: 'ny+7', defaultVolume: 1, oneClick: false, showPanel: true, sidePanel: null },

  // ---- live (not persisted)
  status: { mt5: { connected: false, status: 'starting' }, tv: { mode: 'anonymous' }, external: { providers: [] } },
  wsConnected: false,
  wsEpoch: 0,                  // bumps on every (re)connect so charts resubscribe
  chartMeta: {},               // chartId -> { source, tradable, provider, digits, error, loading }
  positions: [],
  orders: [],
  account: null,
  drawingTool: 'cursor',
  modal: null,
  toasts: [],
  tradeLog: [],
  agents: null,                // latest AgentSystem status pushed by the bridge

  // ---- chart actions
  activeChart: () => {
    const { charts, activeId } = get();
    return charts.find((c) => c.id === activeId && !c.minimized) || charts.find((c) => !c.minimized) || charts[0];
  },
  setActive: (id) => set({ activeId: id }),
  updateChart: (id, patch) => set((s) => ({ charts: s.charts.map((c) => (c.id === id ? { ...c, ...patch } : c)) })),
  setSymbol: (symbol) => { const c = get().activeChart(); if (c) get().updateChart(c.id, { symbol }); },

  addChartAfter: (id, symbol) => set((s) => {
    if (s.charts.length >= MAX_CHARTS) return { toasts: pushToast(s.toasts, 'warn', `Maximum ${MAX_CHARTS} charts`) };
    const i = s.charts.findIndex((c) => c.id === id);
    const used = new Set(s.charts.map((c) => c.symbol));
    const src = s.charts[i];
    const chart = makeChart(symbol || PINNED.find((p) => !used.has(p)) || src?.symbol || PINNED[0], src?.timeframe);
    const charts = [...s.charts];
    charts.splice(i + 1, 0, chart);
    return { charts, activeId: chart.id };
  }),
  removeChart: (id) => set((s) => (s.charts.length <= 1 ? {} : { charts: s.charts.filter((c) => c.id !== id) })),
  minimize: (id) => set((s) => {
    if (s.charts.filter((c) => !c.minimized).length <= 1) return { toasts: pushToast(s.toasts, 'info', 'At least one chart must stay open') };
    return { charts: s.charts.map((c) => (c.id === id ? { ...c, minimized: true } : c)) };
  }),
  restore: (id) => set((s) => ({ charts: s.charts.map((c) => (c.id === id ? { ...c, minimized: false } : c)), activeId: id })),
  setLayoutCount: (n) => set((s) => {
    let charts = s.charts.map((c, i) => ({ ...c, minimized: i >= n }));
    while (charts.length < n) {
      const used = new Set(charts.map((c) => c.symbol));
      charts = [...charts, makeChart(PINNED.find((p) => !used.has(p)) || PINNED[charts.length % 3])];
    }
    return { charts };
  }),
  setFracs: (count, fr) => set((s) => ({ fracs: { ...s.fracs, [count]: fr } })),
  layoutFracs: (count) => {
    const rows = ROWS_FOR[count] || [count];
    const saved = get().fracs[count];
    if (saved && saved.rows.length === rows.length && saved.cols.every((c, i) => c.length === rows[i])) return saved;
    return { rows: equal(rows.length), cols: rows.map(equal) };
  },

  // ---- indicators (no cap)
  addIndicator: (chartId, def) => set((s) => ({
    charts: s.charts.map((c) => (c.id === chartId ? { ...c, indicators: [...c.indicators, newInstance(def)] } : c)),
  })),
  updateIndicator: (chartId, iuid, patch) => set((s) => ({
    charts: s.charts.map((c) => (c.id !== chartId ? c
      : { ...c, indicators: c.indicators.map((i) => (i.uid === iuid ? { ...i, ...patch } : i)) })),
  })),
  removeIndicator: (chartId, iuid) => set((s) => ({
    charts: s.charts.map((c) => (c.id !== chartId ? c : { ...c, indicators: c.indicators.filter((i) => i.uid !== iuid) })),
  })),
  saveCustomIndicator: (ci) => set((s) => ({
    customIndicators: s.customIndicators.some((x) => x.id === ci.id)
      ? s.customIndicators.map((x) => (x.id === ci.id ? ci : x)) : [...s.customIndicators, ci],
  })),
  deleteCustomIndicator: (id) => set((s) => ({ customIndicators: s.customIndicators.filter((x) => x.id !== id) })),

  setChartMeta: (id, meta) => set((s) => ({ chartMeta: { ...s.chartMeta, [id]: { ...(s.chartMeta[id] || {}), ...meta } } })),

  // ---- misc
  setSettings: (patch) => set((s) => ({ settings: { ...s.settings, ...patch } })),
  setDrawingTool: (drawingTool) => set({ drawingTool }),
  openModal: (type, props = {}) => set({ modal: { type, ...props } }),
  closeModal: () => set({ modal: null }),
  toast: (level, message, key) => set((s) => ({ toasts: pushToast(s.toasts, level, message, key) })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  logTrade: (message) => set((s) => ({ tradeLog: [{ id: uid(), time: Date.now(), message }, ...s.tradeLog].slice(0, 200) })),
}), {
  name: 'mt5-tv-terminal',
  version: 2,
  partialize: (s) => ({ charts: s.charts, activeId: s.activeId, fracs: s.fracs, customIndicators: s.customIndicators,
    settings: s.settings }),
  merge: (persisted, current) => {
    const merged = { ...current, ...persisted, settings: { ...current.settings, ...(persisted?.settings || {}) } };
    // The three main instruments are always loaded.
    const have = new Set((merged.charts || []).map((c) => c.symbol));
    const missing = PINNED.filter((p) => !have.has(p));
    if (missing.length && merged.charts.length + missing.length <= MAX_CHARTS) {
      merged.charts = [...merged.charts, ...missing.map((s) => ({ ...makeChart(s), minimized: true }))];
    }
    return merged;
  },
}));

function pushToast(list, level, message, key) {
  const filtered = key ? list.filter((t) => t.key !== key) : list;
  return [...filtered, { id: uid(), level, message, key, time: Date.now() }].slice(-6);
}
