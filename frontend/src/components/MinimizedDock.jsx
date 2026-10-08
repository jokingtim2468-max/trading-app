import { MAX_CHARTS, useStore } from '../lib/store.js';

export default function MinimizedDock() {
  const charts = useStore((s) => s.charts);
  const { restore, removeChart, addChartAfter } = useStore.getState();
  const minimized = charts.filter((c) => c.minimized);
  return (
    <div className="dock">
      <span className="muted">Minimised:</span>
      {minimized.length === 0 && <span className="muted">none</span>}
      {minimized.map((c) => (
        <span key={c.id} className="chip">
          <button type="button" onClick={() => restore(c.id)} title="Restore">⤢ {c.symbol} · {c.timeframe}</button>
          <button type="button" onClick={() => removeChart(c.id)} title="Close">✕</button>
        </span>
      ))}
      <button type="button" className="chip add" disabled={charts.length >= MAX_CHARTS}
        onClick={() => addChartAfter(charts.at(-1).id)}>＋ New chart</button>
    </div>
  );
}
