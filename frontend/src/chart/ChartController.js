// Imperative wrapper around one Lightweight Charts v5 instance: main series, live bar
// building, unlimited indicators, MT5 position/order price lines and drag-to-modify.
import {
  AreaSeries, BarSeries, CandlestickSeries, CrosshairMode, HistogramSeries, LineSeries, LineStyle, LineType,
  createChart,
} from 'lightweight-charts';
import { BarBuilder } from '../lib/timeframes.js';
import { THEME } from '../lib/theme.js';
import { roundTo } from '../lib/format.js';
import { definitionOf, evaluate } from '../indicators/engine.js';
import { makeClock } from '../indicators/ta.js';

const HIT_PX = 6;
const INDICATOR_THROTTLE_MS = 250;

export class ChartController {
  constructor(container, { onContextMenu, onTradeCommit, onCrosshair }) {
    this.el = container;
    this.cb = { onContextMenu, onTradeCommit, onCrosshair };
    this.bars = [];
    this.meta = { digits: 2, tfKind: 'time', tfSeconds: 60, source: 'mt5', tradable: false };
    this.chartType = 'candles';
    this.indicators = new Map(); // uid -> { key, series: [], def }
    this.indicatorInputs = { list: [], customs: [], clockMode: 'utc' };
    this.levels = new Map();     // level id -> { ...level, line }
    this.lastTrade = { positions: [], orders: [] };
    this.drag = null;
    this.indTimer = null;

    this.chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { color: THEME.bg }, textColor: THEME.text, fontFamily: THEME.font, fontSize: 11,
        panes: { separatorColor: THEME.border, enableResize: true },
        attributionLogo: true, // Lightweight Charts is Apache-2.0; keep the TradingView attribution
      },
      grid: { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false, rightOffset: 8 },
    });
    this.#createMainSeries();

    this.chart.subscribeCrosshairMove((param) => {
      if (!this.drag) {
        const y = param.point?.y;
        this.el.style.cursor = y != null && this.#hit(y) ? 'ns-resize' : '';
      }
      const bar = param.time ? param.seriesData.get(this.series) : null;
      this.cb.onCrosshair?.(bar ? this.bars.find((b) => b.time === param.time) || bar : this.bars.at(-1));
    });

    this.onDown = this.#onDown.bind(this);
    this.onMove = this.#onMove.bind(this);
    this.onUp = this.#onUp.bind(this);
    this.onKey = (e) => { if (e.key === 'Escape' && this.drag) this.#cancelDrag(); };
    this.onCtx = this.#onContextMenu.bind(this);
    // Capture phase: we see the mousedown before the chart starts panning.
    container.addEventListener('mousedown', this.onDown, true);
    container.addEventListener('contextmenu', this.onCtx);
    window.addEventListener('mousemove', this.onMove);
    window.addEventListener('mouseup', this.onUp);
    window.addEventListener('keydown', this.onKey);
  }

  destroy() {
    clearTimeout(this.indTimer);
    this.el.removeEventListener('mousedown', this.onDown, true);
    this.el.removeEventListener('contextmenu', this.onCtx);
    window.removeEventListener('mousemove', this.onMove);
    window.removeEventListener('mouseup', this.onUp);
    window.removeEventListener('keydown', this.onKey);
    this.chart.remove();
  }

  // ------------------------------------------------------------------ main series
  #createMainSeries() {
    const common = { priceFormat: this.#priceFormat(), lastValueVisible: true, priceLineVisible: true };
    const map = {
      candles: [CandlestickSeries, { upColor: THEME.up, downColor: THEME.down, borderVisible: false,
        wickUpColor: THEME.up, wickDownColor: THEME.down }],
      bars: [BarSeries, { upColor: THEME.up, downColor: THEME.down, thinBars: false }],
      line: [LineSeries, { color: THEME.accent, lineWidth: 2 }],
      area: [AreaSeries, { lineColor: THEME.accent, topColor: 'rgba(41,98,255,0.35)', bottomColor: 'rgba(41,98,255,0)' }],
    };
    const [Def, opts] = map[this.chartType] || map.candles;
    this.series = this.chart.addSeries(Def, { ...common, ...opts }, 0);
  }

  #priceFormat() {
    const d = this.meta.digits ?? 2;
    return { type: 'price', precision: d, minMove: 10 ** -d };
  }

  #toPoint(b) {
    return this.chartType === 'line' || this.chartType === 'area' ? { time: b.time, value: b.close } : b;
  }

  setChartType(type) {
    if (type === this.chartType) return;
    this.chart.removeSeries(this.series);
    this.levels.clear(); // price lines belonged to the old series
    this.chartType = type;
    this.#createMainSeries();
    this.series.setData(this.bars.map((b) => this.#toPoint(b)));
    this.setTradeLevels(this.lastTrade.positions, this.lastTrade.orders);
  }

  setData(bars, meta) {
    this.meta = { ...this.meta, ...meta };
    this.bars = bars.map((b) => ({ ...b }));
    this.builder = new BarBuilder(meta.tf, this.bars.at(-1));
    this.series.applyOptions({ priceFormat: this.#priceFormat() });
    this.series.setData(this.bars.map((b) => this.#toPoint(b)));
    this.chart.applyOptions({ timeScale: { secondsVisible: meta.tf.kind === 'tick' || meta.tf.seconds < 60 } });
    this.chart.timeScale().scrollToRealTime();
    this.#recomputeIndicators(true);
    this.levels.forEach((l) => this.series.removePriceLine(l.line));
    this.levels.clear();
    this.setTradeLevels(this.lastTrade.positions, this.lastTrade.orders);
    this.cb.onCrosshair?.(this.bars.at(-1));
  }

  /** MT5 ticks: [[time_msc, bid, ask, last], ...] */
  applyTicks(ticks) {
    if (!this.builder) return;
    let bar = null;
    for (const [tMsc, bid, , last] of ticks) {
      bar = this.builder.push(tMsc, last || bid);
      this.#upsert(bar);
    }
    if (bar) {
      this.series.update(this.#toPoint(bar));
      this.#scheduleIndicators();
      if (!this.drag) this.cb.onCrosshair?.(bar);
    }
  }

  /** External feed: latest closed/forming bars. */
  applyBars(bars) {
    for (const b of bars) {
      if (this.bars.length && b.time < this.bars.at(-1).time) continue;
      this.#upsert({ ...b });
      this.series.update(this.#toPoint(b));
    }
    if (this.builder && this.bars.length) this.builder.replace(this.bars.at(-1));
    this.#scheduleIndicators();
  }

  #upsert(bar) {
    const last = this.bars.at(-1);
    if (last && last.time === bar.time) this.bars[this.bars.length - 1] = { ...bar };
    else this.bars.push({ ...bar });
  }

  // ------------------------------------------------------------------ indicators
  setIndicators(list, customs, clockMode) {
    this.indicatorInputs = { list, customs, clockMode };
    const wanted = new Map(list.filter((i) => i.visible !== false).map((i) => [i.uid, i]));
    for (const [uid, entry] of this.indicators) {
      const inst = wanted.get(uid);
      const def = inst && definitionOf(inst, customs);
      if (!inst || !def || entry.key !== this.#instKey(inst, def)) this.#removeIndicator(uid);
    }
    this.#recomputeIndicators(true);
  }

  #instKey(inst, def) {
    return JSON.stringify([inst.params, def.code || def.id, this.indicatorInputs.clockMode]);
  }

  #removeIndicator(uid) {
    const entry = this.indicators.get(uid);
    if (!entry) return;
    entry.series.forEach((s) => this.chart.removeSeries(s));
    this.indicators.delete(uid);
    const panes = this.chart.panes();
    for (let i = panes.length - 1; i > 0; i--) {
      if (panes[i].getSeries().length === 0) this.chart.removePane(i);
    }
  }

  #scheduleIndicators() {
    if (this.indTimer) return;
    this.indTimer = setTimeout(() => { this.indTimer = null; this.#recomputeIndicators(false); }, INDICATOR_THROTTLE_MS);
  }

  #recomputeIndicators(full) {
    const { list, customs, clockMode } = this.indicatorInputs;
    if (!this.bars.length) return;
    const ctx = { clock: this.clock(clockMode), tfSeconds: this.meta.tf?.seconds || 0, tfKind: this.meta.tf?.kind };
    for (const inst of list) {
      if (inst.visible === false) continue;
      const def = definitionOf(inst, customs);
      if (!def) continue;
      let res;
      try {
        res = evaluate(def, this.bars, inst.params, ctx);
      } catch (err) {
        console.warn(`indicator ${def.name} failed`, err);
        continue;
      }
      let entry = this.indicators.get(inst.uid);
      const isNew = !entry;
      if (isNew) entry = this.#createIndicatorSeries(inst, def, res);
      const doFull = full || isNew || (entry.len !== this.bars.length - 1 && entry.len !== this.bars.length);
      res.plots.forEach((plot, i) => {
        const s = entry.series[i];
        if (!s) return;
        if (doFull) s.setData(this.bars.map((b, j) => this.#plotPoint(plot, b, j)));
        else {
          // Re-touch the previous bar only when a new bar just opened (LWC rejects older updates).
          const from = Math.max(0, this.bars.length - (entry.len === this.bars.length - 1 ? 2 : 1));
          for (let j = from; j < this.bars.length; j++) s.update(this.#plotPoint(plot, this.bars[j], j));
        }
      });
      entry.len = this.bars.length;
    }
  }

  clock(mode) {
    // External feeds are true UTC; MT5 bars use the broker clock chosen in settings.
    const m = this.meta.source === 'mt5' ? mode : 'utc';
    if (!this._clocks) this._clocks = new Map();
    if (!this._clocks.has(m)) this._clocks.set(m, makeClock(m));
    return this._clocks.get(m);
  }

  #plotPoint(plot, bar, j) {
    const v = plot.values[j];
    if (v == null || Number.isNaN(v) || !Number.isFinite(v)) return { time: bar.time };
    const p = { time: bar.time, value: v };
    if (plot.colorByBar) p.color = bar.close >= bar.open ? 'rgba(38,166,154,0.5)' : 'rgba(239,83,80,0.5)';
    if (plot.colorBySign) p.color = v >= 0 ? THEME.up : THEME.down;
    return p;
  }

  #createIndicatorSeries(inst, def, res) {
    const pane = res.overlay ? 0 : this.chart.panes().length;
    const series = [];
    res.plots.forEach((plot, i) => {
      const style = plot.dotted ? LineStyle.Dotted : plot.dashed ? LineStyle.Dashed : LineStyle.Solid;
      const base = { title: i === 0 || res.plots.length < 4 ? plot.title : '', priceLineVisible: false,
        lastValueVisible: !res.overlay || i === 0, priceFormat: res.overlay ? this.#priceFormat() : undefined,
        // Far-away daily levels (PDL, ADR, pivots) must not squash the candles - like TradingView's
        // "scale price chart only". They are still drawn whenever they are inside the visible range.
        ...(def.group === 'High/Low Projection' ? { autoscaleInfoProvider: () => null } : {}) };
      const paneIndex = i === 0 ? pane : series[0].getPane().paneIndex();
      const s = plot.style === 'histogram'
        ? this.chart.addSeries(HistogramSeries, { ...base, color: plot.color }, paneIndex)
        : this.chart.addSeries(LineSeries, {
          ...base, color: plot.color, lineWidth: plot.lineWidth || 1, lineStyle: style,
          lineType: plot.style === 'step' ? LineType.WithSteps : LineType.Simple,
          crosshairMarkerVisible: false,
        }, paneIndex);
      series.push(s);
    });
    if (!res.overlay && series[0]) {
      for (const lvl of res.levels) {
        series[0].createPriceLine({ price: lvl, color: THEME.muted, lineStyle: LineStyle.Dashed, lineWidth: 1,
          axisLabelVisible: false });
      }
      this.chart.panes()[0].setStretchFactor(3);
    }
    const entry = { key: this.#instKey(inst, def), series, len: 0 };
    this.indicators.set(inst.uid, entry);
    return entry;
  }

  // ------------------------------------------------------------------ trade levels
  /** Draw Entry / SL / TP for every MT5 position and pending order on this symbol. */
  setTradeLevels(positions, orders) {
    this.lastTrade = { positions, orders };
    if (this.drag) return; // re-sync after the drop
    const sym = this.meta.symbol;
    const wanted = new Map();
    const d = this.meta.digits ?? 2;
    if (this.meta.source === 'mt5') {
      for (const p of positions.filter((x) => x.symbol === sym)) {
        const owner = p.source === 'ea' ? THEME.ea : THEME.manual;
        const pl = `${p.profit >= 0 ? '+' : ''}${p.profit.toFixed(2)}`;
        wanted.set(`p${p.ticket}`, { kind: 'entry', ref: p, isOrder: false, price: p.price_open, color: owner,
          title: `${p.label} · ${p.side.toUpperCase()} ${p.volume} · ${pl}`, style: LineStyle.Solid, width: 2,
          draggable: !p.sl || !p.tp });
        if (p.sl) wanted.set(`p${p.ticket}sl`, { kind: 'sl', ref: p, isOrder: false, price: p.sl, color: THEME.sl,
          title: `SL · ${p.label}`, style: LineStyle.Dashed, width: 1, draggable: true });
        if (p.tp) wanted.set(`p${p.ticket}tp`, { kind: 'tp', ref: p, isOrder: false, price: p.tp, color: THEME.tp,
          title: `TP · ${p.label}`, style: LineStyle.Dashed, width: 1, draggable: true });
      }
      for (const o of orders.filter((x) => x.symbol === sym)) {
        const owner = o.source === 'ea' ? THEME.ea : THEME.pending;
        wanted.set(`o${o.ticket}`, { kind: 'entry', ref: o, isOrder: true, price: o.price_open, color: owner,
          title: `${o.label} · ${o.type_name} ${o.volume}`, style: LineStyle.LargeDashed, width: 1, draggable: true });
        if (o.sl) wanted.set(`o${o.ticket}sl`, { kind: 'sl', ref: o, isOrder: true, price: o.sl, color: THEME.sl,
          title: `SL · ${o.type_name}`, style: LineStyle.Dotted, width: 1, draggable: true });
        if (o.tp) wanted.set(`o${o.ticket}tp`, { kind: 'tp', ref: o, isOrder: true, price: o.tp, color: THEME.tp,
          title: `TP · ${o.type_name}`, style: LineStyle.Dotted, width: 1, draggable: true });
      }
    }
    for (const [id, lvl] of this.levels) {
      if (!wanted.has(id)) { this.series.removePriceLine(lvl.line); this.levels.delete(id); }
    }
    for (const [id, lvl] of wanted) {
      const opts = { price: roundTo(lvl.price, d), color: lvl.color, lineWidth: lvl.width, lineStyle: lvl.style,
        axisLabelVisible: true, title: lvl.title, lineVisible: true };
      const cur = this.levels.get(id);
      if (cur) { cur.line.applyOptions(opts); Object.assign(cur, lvl); }
      else this.levels.set(id, { ...lvl, id, line: this.series.createPriceLine(opts) });
    }
  }

  #hit(y) {
    if (!this.meta.tradable) return null;
    let best = null;
    for (const lvl of this.levels.values()) {
      if (!lvl.draggable) continue;
      const ly = this.series.priceToCoordinate(lvl.price);
      if (ly == null) continue;
      const dist = Math.abs(ly - y);
      if (dist <= HIT_PX && (!best || dist < best.dist)) best = { lvl, dist };
    }
    return best?.lvl || null;
  }

  #localY(e) { return e.clientY - this.el.getBoundingClientRect().top; }

  #onDown(e) {
    if (e.button !== 0) return;
    const lvl = this.#hit(this.#localY(e));
    if (!lvl) return;
    e.preventDefault();
    e.stopPropagation(); // keep the chart from panning while we drag the line
    const ghost = lvl.kind === 'entry' && !lvl.isOrder
      ? this.series.createPriceLine({ price: lvl.price, color: THEME.muted, lineStyle: LineStyle.Dashed,
        lineWidth: 1, axisLabelVisible: true, title: 'drag → SL / TP' })
      : null;
    this.drag = { lvl, price: lvl.price, ghost };
    this.chart.applyOptions({ handleScroll: false, handleScale: false });
    this.el.style.cursor = 'ns-resize';
  }

  #onMove(e) {
    if (!this.drag) return;
    const raw = this.series.coordinateToPrice(this.#localY(e));
    if (raw == null) return;
    const price = roundTo(raw, this.meta.digits ?? 2);
    this.drag.price = price;
    const { lvl, ghost } = this.drag;
    if (ghost) {
      const p = lvl.ref;
      const isTp = (p.side === 'buy') === (price > p.price_open);
      ghost.applyOptions({ price, color: isTp ? THEME.tp : THEME.sl, title: `${isTp ? 'TP' : 'SL'} ${price}` });
    } else {
      lvl.line.applyOptions({ price, title: `${lvl.title} → ${price}` });
    }
  }

  #endDrag() {
    const { ghost } = this.drag;
    if (ghost) this.series.removePriceLine(ghost);
    this.drag = null;
    this.chart.applyOptions({ handleScroll: true, handleScale: true });
    this.el.style.cursor = '';
  }

  #cancelDrag() {
    this.#endDrag();
    this.setTradeLevels(this.lastTrade.positions, this.lastTrade.orders);
  }

  #onUp() {
    if (!this.drag) return;
    const { lvl, price } = this.drag;
    this.#endDrag();
    if (price === lvl.price) { this.setTradeLevels(this.lastTrade.positions, this.lastTrade.orders); return; }
    const r = lvl.ref;
    let type; let payload;
    if (lvl.isOrder) {
      type = 'modify_order';
      payload = { ticket: r.ticket, price: r.price_open, sl: r.sl, tp: r.tp };
      if (lvl.kind === 'entry') payload.price = price; else payload[lvl.kind] = price;
    } else {
      type = 'modify_sltp';
      payload = { ticket: r.ticket, sl: r.sl, tp: r.tp };
      if (lvl.kind === 'entry') payload[(r.side === 'buy') === (price > r.price_open) ? 'tp' : 'sl'] = price;
      else payload[lvl.kind] = price;
    }
    // Leave the line at the dropped price; the next MT5 snapshot confirms or reverts it.
    lvl.price = price;
    lvl.line?.applyOptions({ price, title: `${lvl.title} …` });
    this.cb.onTradeCommit?.(type, payload, () => this.setTradeLevels(this.lastTrade.positions, this.lastTrade.orders));
  }

  #onContextMenu(e) {
    e.preventDefault();
    const rect = this.el.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const price = this.series.coordinateToPrice(y);
    this.cb.onContextMenu?.({ x: e.clientX, y: e.clientY, price: price == null ? null : roundTo(price, this.meta.digits ?? 2),
      level: this.#hit(y), last: this.bars.at(-1)?.close });
  }

  // ------------------------------------------------------------------ helpers for the drawing layer
  get timeScale() { return this.chart.timeScale(); }

  mainPaneSize() {
    const pane = this.chart.panes()[0];
    return { width: this.timeScale.width(), height: pane ? pane.getHeight() : this.el.clientHeight };
  }

  timeToX(time) {
    const bars = this.bars;
    if (!bars.length) return null;
    let lo = 0; let hi = bars.length - 1;
    if (time >= bars[hi].time) {
      const step = bars.length > 1 ? bars[hi].time - bars[hi - 1].time : 60;
      return this.timeScale.logicalToCoordinate(hi + (time - bars[hi].time) / (step || 1));
    }
    if (time <= bars[0].time) {
      const step = bars.length > 1 ? bars[1].time - bars[0].time : 60;
      return this.timeScale.logicalToCoordinate((time - bars[0].time) / (step || 1));
    }
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (bars[mid].time <= time) lo = mid; else hi = mid; }
    const frac = (time - bars[lo].time) / (bars[hi].time - bars[lo].time || 1);
    return this.timeScale.logicalToCoordinate(lo + frac);
  }

  xToTime(x) {
    const logical = this.timeScale.coordinateToLogical(x);
    const bars = this.bars;
    if (logical == null || !bars.length) return null;
    const i = Math.floor(logical);
    const last = bars.length - 1;
    const step = bars.length > 1 ? bars[last].time - bars[last - 1].time : 60;
    if (i >= last) return Math.round(bars[last].time + (logical - last) * step);
    if (i < 0) return Math.round(bars[0].time + logical * step);
    return Math.round(bars[i].time + (logical - i) * (bars[i + 1].time - bars[i].time));
  }

  priceToY(p) { return this.series.priceToCoordinate(p); }
  yToPrice(y) { return this.series.coordinateToPrice(y); }
}
