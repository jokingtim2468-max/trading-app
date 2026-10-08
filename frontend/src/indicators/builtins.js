// Built-in indicator registry. No instance limit: every applied indicator is just more
// Lightweight Charts series, so you can stack as many as your machine can draw.
import { atr, ema, highest, lowest, rma, rsi, sessions, sma, src, stdev, trueRange, wma } from './ta.js';

const n = (key, label, def, extra = {}) => ({ key, label, type: 'number', default: def, ...extra });
const source = { key: 'source', label: 'Source', type: 'select', default: 'close',
  options: ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4'] };
const NaNs = (len) => new Array(len).fill(NaN);

// ---------------------------------------------------------------- session-level helpers
const intradayOnly = (ctx) => (ctx.tfSeconds > 0 && ctx.tfSeconds < 86400) || ctx.tfKind === 'tick';

/** Values that hold for a whole session, keyed by session index, broadcast to bars. */
function perSession(bars, ctx, keys, fn) {
  const out = Object.fromEntries(keys.map((k) => [k, NaNs(bars.length)]));
  if (!intradayOnly(ctx)) return out;
  const { idx, days } = sessions(bars, ctx.clock);
  const vals = days.map((d, i) => fn(d, i, days));
  for (let i = 0; i < bars.length; i++) {
    const v = vals[idx[i]];
    if (v) for (const k of keys) out[k][i] = v[k] ?? NaN;
  }
  return out;
}

/** Range built from bars inside a NY-time window [fromMin, toMin) of each session,
 *  shown from the end of the window to the end of the session. */
function windowRange(bars, ctx, fromMin, toMin, mults) {
  const keys = ['high', 'low', ...mults.flatMap((m) => [`up${m}`, `dn${m}`])];
  const out = Object.fromEntries(keys.map((k) => [k, NaNs(bars.length)]));
  if (!intradayOnly(ctx)) return out;
  const { idx } = sessions(bars, ctx.clock);
  let cur = -1; let hi = -Infinity; let lo = Infinity; let done = false;
  for (let i = 0; i < bars.length; i++) {
    if (idx[i] !== cur) { cur = idx[i]; hi = -Infinity; lo = Infinity; done = false; }
    const { minutes } = ctx.clock(bars[i].time);
    const inWin = fromMin <= toMin ? minutes >= fromMin && minutes < toMin : minutes >= fromMin || minutes < toMin;
    if (inWin && !done) { hi = Math.max(hi, bars[i].high); lo = Math.min(lo, bars[i].low); continue; }
    if (hi > -Infinity && !inWin) done = true;
    if (!done) continue;
    const w = hi - lo;
    out.high[i] = hi; out.low[i] = lo;
    for (const m of mults) { out[`up${m}`][i] = hi + w * m; out[`dn${m}`][i] = lo - w * m; }
  }
  return out;
}

const hm = (s) => { const [h, m] = String(s).split(':').map(Number); return h * 60 + (m || 0); };

// ---------------------------------------------------------------- registry
export const BUILTINS = [
  // ---- High / Low projection (NAS100 · US500 · XAUUSD) -----------------------------
  {
    id: 'pdhl', name: 'Previous Day High / Low / Close', group: 'High/Low Projection', overlay: true,
    description: 'Yesterday\'s session extremes - the most-watched intraday magnets on NQ/ES/Gold.',
    params: [],
    plots: [
      { key: 'pdh', title: 'PDH', color: '#26a69a', style: 'step', lineWidth: 2 },
      { key: 'pdl', title: 'PDL', color: '#ef5350', style: 'step', lineWidth: 2 },
      { key: 'pdc', title: 'PDC', color: '#9598a1', style: 'step', dashed: true },
    ],
    calc: (bars, p, ctx) => perSession(bars, ctx, ['pdh', 'pdl', 'pdc'], (d, i, days) => (
      i > 0 ? { pdh: days[i - 1].high, pdl: days[i - 1].low, pdc: days[i - 1].close } : null)),
  },
  {
    id: 'adr', name: 'ADR Projected High / Low', group: 'High/Low Projection', overlay: true,
    description: 'Average Daily Range projection: Projected High = session low + ADR, Projected Low = '
      + 'session high - ADR. Once one side is set the other end of the day is forecast.',
    params: [n('length', 'ADR days', 14, { min: 1 }), n('mult', 'Multiplier', 1, { step: 0.1 })],
    plots: [
      { key: 'ph', title: 'Proj High', color: '#00bcd4', style: 'line', lineWidth: 2, dashed: true },
      { key: 'pl', title: 'Proj Low', color: '#ff9800', style: 'line', lineWidth: 2, dashed: true },
      { key: 'mid', title: 'Day Open', color: '#787b86', style: 'step', dotted: true },
    ],
    calc: (bars, p, ctx) => {
      const out = { ph: NaNs(bars.length), pl: NaNs(bars.length), mid: NaNs(bars.length) };
      if (!intradayOnly(ctx)) return out;
      const { idx, days } = sessions(bars, ctx.clock);
      const adr = days.map((_, i) => {
        if (i < p.length) return NaN;
        let s = 0;
        for (let j = i - p.length; j < i; j++) s += days[j].high - days[j].low;
        return (s / p.length) * p.mult;
      });
      let cur = -1; let hi = -Infinity; let lo = Infinity;
      for (let i = 0; i < bars.length; i++) {
        if (idx[i] !== cur) { cur = idx[i]; hi = -Infinity; lo = Infinity; }
        hi = Math.max(hi, bars[i].high); lo = Math.min(lo, bars[i].low);
        const a = adr[cur];
        if (Number.isNaN(a)) continue;
        out.ph[i] = Math.max(lo + a, hi);
        out.pl[i] = Math.min(hi - a, lo);
        out.mid[i] = days[cur].open;
      }
      return out;
    },
  },
  {
    id: 'orb', name: 'NY Opening Range + Targets', group: 'High/Low Projection', overlay: true,
    description: 'High/low of the first N minutes after the 09:30 New York cash open, with 0.5x/1x/2x range extensions as projected high/low targets.',
    params: [n('minutes', 'Range minutes', 15, { min: 1 }), { key: 'start', label: 'Start (NY)', type: 'text', default: '09:30' }],
    plots: [
      { key: 'high', title: 'ORH', color: '#2962ff', style: 'step', lineWidth: 2 },
      { key: 'low', title: 'ORL', color: '#2962ff', style: 'step', lineWidth: 2 },
      { key: 'up0.5', title: 'OR +0.5', color: '#5b9cf6', style: 'step', dotted: true },
      { key: 'dn0.5', title: 'OR -0.5', color: '#5b9cf6', style: 'step', dotted: true },
      { key: 'up1', title: 'OR +1', color: '#26a69a', style: 'step', dashed: true },
      { key: 'dn1', title: 'OR -1', color: '#ef5350', style: 'step', dashed: true },
      { key: 'up2', title: 'OR +2', color: '#26a69a', style: 'step', dotted: true },
      { key: 'dn2', title: 'OR -2', color: '#ef5350', style: 'step', dotted: true },
    ],
    calc: (bars, p, ctx) => windowRange(bars, ctx, hm(p.start), hm(p.start) + p.minutes, [0.5, 1, 2]),
  },
  {
    id: 'ib', name: 'Initial Balance (1st hour) + Extensions', group: 'High/Low Projection', overlay: true,
    description: 'Market-profile Initial Balance 09:30-10:30 NY. 1.5x and 2x IB extensions are classic day-range targets.',
    params: [{ key: 'start', label: 'Start (NY)', type: 'text', default: '09:30' }, n('minutes', 'Minutes', 60)],
    plots: [
      { key: 'high', title: 'IBH', color: '#ab47bc', style: 'step', lineWidth: 2 },
      { key: 'low', title: 'IBL', color: '#ab47bc', style: 'step', lineWidth: 2 },
      { key: 'up0.5', title: 'IB 1.5x up', color: '#ce93d8', style: 'step', dashed: true },
      { key: 'dn0.5', title: 'IB 1.5x dn', color: '#ce93d8', style: 'step', dashed: true },
      { key: 'up1', title: 'IB 2x up', color: '#ce93d8', style: 'step', dotted: true },
      { key: 'dn1', title: 'IB 2x dn', color: '#ce93d8', style: 'step', dotted: true },
    ],
    calc: (bars, p, ctx) => windowRange(bars, ctx, hm(p.start), hm(p.start) + p.minutes, [0.5, 1]),
  },
  {
    id: 'overnight', name: 'Overnight (Globex) High / Low', group: 'High/Low Projection', overlay: true,
    description: 'Range from the 18:00 NY reopen to the 09:30 cash open; ONH/ONL are frequently tested in the first hour.',
    params: [{ key: 'start', label: 'From (NY)', type: 'text', default: '18:00' },
      { key: 'end', label: 'To (NY)', type: 'text', default: '09:30' }],
    plots: [
      { key: 'high', title: 'ONH', color: '#ffb74d', style: 'step', lineWidth: 2 },
      { key: 'low', title: 'ONL', color: '#ffb74d', style: 'step', lineWidth: 2 },
    ],
    calc: (bars, p, ctx) => windowRange(bars, ctx, hm(p.start), hm(p.end), []),
  },
  {
    id: 'pivots', name: 'Pivot Points (Classic / Camarilla / Fib)', group: 'High/Low Projection', overlay: true,
    description: 'Daily pivots from the previous session\'s H/L/C. Camarilla H3/L3 & H4/L4 are popular reversal / breakout levels for NQ & Gold.',
    params: [{ key: 'kind', label: 'Type', type: 'select', default: 'camarilla', options: ['classic', 'camarilla', 'fibonacci'] }],
    plots: [
      { key: 'p', title: 'P', color: '#f7a600', style: 'step', lineWidth: 2 },
      { key: 'r1', title: 'R1/H3', color: '#ef5350', style: 'step', dashed: true },
      { key: 'r2', title: 'R2/H4', color: '#ef5350', style: 'step' },
      { key: 'r3', title: 'R3/H5', color: '#ef5350', style: 'step', dotted: true },
      { key: 's1', title: 'S1/L3', color: '#26a69a', style: 'step', dashed: true },
      { key: 's2', title: 'S2/L4', color: '#26a69a', style: 'step' },
      { key: 's3', title: 'S3/L5', color: '#26a69a', style: 'step', dotted: true },
    ],
    calc: (bars, p, ctx) => perSession(bars, ctx, ['p', 'r1', 'r2', 'r3', 's1', 's2', 's3'], (d, i, days) => {
      if (i === 0) return null;
      const { high: H, low: L, close: C } = days[i - 1];
      const R = H - L; const P = (H + L + C) / 3;
      if (p.kind === 'camarilla') {
        return { p: P, r1: C + R * 1.1 / 4, r2: C + R * 1.1 / 2, r3: (H / L) * C,
          s1: C - R * 1.1 / 4, s2: C - R * 1.1 / 2, s3: C - ((H / L) * C - C) };
      }
      if (p.kind === 'fibonacci') {
        return { p: P, r1: P + 0.382 * R, r2: P + 0.618 * R, r3: P + R, s1: P - 0.382 * R, s2: P - 0.618 * R, s3: P - R };
      }
      return { p: P, r1: 2 * P - L, r2: P + R, r3: H + 2 * (P - L), s1: 2 * P - H, s2: P - R, s3: L - 2 * (H - P) };
    }),
  },
  {
    id: 'vwapbands', name: 'Session VWAP + σ Bands', group: 'High/Low Projection', overlay: true,
    description: 'Daily-anchored VWAP with standard-deviation bands; ±2σ marks statistically stretched highs/lows.',
    params: [n('m1', 'Band 1 σ', 1, { step: 0.1 }), n('m2', 'Band 2 σ', 2, { step: 0.1 })],
    plots: [
      { key: 'vwap', title: 'VWAP', color: '#f7a600', style: 'line', lineWidth: 2 },
      { key: 'u1', title: '+1σ', color: '#9598a1', style: 'line', dashed: true },
      { key: 'l1', title: '-1σ', color: '#9598a1', style: 'line', dashed: true },
      { key: 'u2', title: '+2σ', color: '#ef5350', style: 'line', dotted: true },
      { key: 'l2', title: '-2σ', color: '#26a69a', style: 'line', dotted: true },
    ],
    calc: (bars, p, ctx) => {
      const keys = ['vwap', 'u1', 'l1', 'u2', 'l2'];
      const out = Object.fromEntries(keys.map((k) => [k, NaNs(bars.length)]));
      const { idx } = sessions(bars, ctx.clock);
      let cur = -1; let pv = 0; let v = 0; let p2v = 0;
      for (let i = 0; i < bars.length; i++) {
        if (idx[i] !== cur) { cur = idx[i]; pv = 0; v = 0; p2v = 0; }
        const tp = (bars[i].high + bars[i].low + bars[i].close) / 3;
        const vol = bars[i].volume || 1;
        pv += tp * vol; v += vol; p2v += tp * tp * vol;
        const vw = pv / v;
        const sd = Math.sqrt(Math.max(p2v / v - vw * vw, 0));
        out.vwap[i] = vw;
        out.u1[i] = vw + sd * p.m1; out.l1[i] = vw - sd * p.m1;
        out.u2[i] = vw + sd * p.m2; out.l2[i] = vw - sd * p.m2;
      }
      return out;
    },
  },

  // ---- Trend --------------------------------------------------------------------------
  ...[['sma', 'Simple Moving Average', sma, 20, '#2962ff'], ['ema', 'Exponential Moving Average', ema, 21, '#f7a600'],
    ['wma', 'Weighted Moving Average', wma, 20, '#ab47bc']].map(([id, name, fn, len, color]) => ({
    id, name, group: 'Trend', overlay: true, params: [n('length', 'Length', len, { min: 1 }), source],
    plots: [{ key: 'v', title: id.toUpperCase(), color, style: 'line', lineWidth: 2 }],
    calc: (bars, p) => ({ v: fn(src(bars, p.source), p.length) }),
  })),
  {
    id: 'hma', name: 'Hull Moving Average', group: 'Trend', overlay: true, params: [n('length', 'Length', 55), source],
    plots: [{ key: 'v', title: 'HMA', color: '#00bcd4', style: 'line', lineWidth: 2 }],
    calc: (bars, p) => {
      const x = src(bars, p.source);
      const a = wma(x, Math.round(p.length / 2)); const b = wma(x, p.length);
      return { v: wma(a.map((v, i) => 2 * v - b[i]), Math.round(Math.sqrt(p.length))) };
    },
  },
  {
    id: 'supertrend', name: 'Supertrend', group: 'Trend', overlay: true,
    params: [n('length', 'ATR length', 10), n('mult', 'Multiplier', 3, { step: 0.1 })],
    plots: [{ key: 'up', title: 'Up', color: '#26a69a', style: 'line', lineWidth: 2 },
      { key: 'dn', title: 'Down', color: '#ef5350', style: 'line', lineWidth: 2 }],
    calc: (bars, p) => {
      const a = atr(bars, p.length);
      const up = NaNs(bars.length); const dn = NaNs(bars.length);
      let fu = NaN; let fl = NaN; let dir = 1;
      for (let i = 0; i < bars.length; i++) {
        if (Number.isNaN(a[i])) continue;
        const mid = (bars[i].high + bars[i].low) / 2;
        const bu = mid + p.mult * a[i]; const bl = mid - p.mult * a[i];
        const pc = i ? bars[i - 1].close : bars[i].close;
        fu = Number.isNaN(fu) || bu < fu || pc > fu ? bu : fu;
        fl = Number.isNaN(fl) || bl > fl || pc < fl ? bl : fl;
        if (bars[i].close > fu) dir = 1; else if (bars[i].close < fl) dir = -1;
        if (dir === 1) up[i] = fl; else dn[i] = fu;
      }
      return { up, dn };
    },
  },
  {
    id: 'bb', name: 'Bollinger Bands', group: 'Volatility', overlay: true,
    params: [n('length', 'Length', 20), n('mult', 'StdDev', 2, { step: 0.1 }), source],
    plots: [{ key: 'mid', title: 'Basis', color: '#f7a600', style: 'line' },
      { key: 'up', title: 'Upper', color: '#2962ff', style: 'line' }, { key: 'lo', title: 'Lower', color: '#2962ff', style: 'line' }],
    calc: (bars, p) => {
      const x = src(bars, p.source); const m = sma(x, p.length); const sd = stdev(x, p.length);
      return { mid: m, up: m.map((v, i) => v + p.mult * sd[i]), lo: m.map((v, i) => v - p.mult * sd[i]) };
    },
  },
  {
    id: 'keltner', name: 'Keltner Channels', group: 'Volatility', overlay: true,
    params: [n('length', 'EMA length', 20), n('mult', 'ATR mult', 2, { step: 0.1 })],
    plots: [{ key: 'mid', title: 'Mid', color: '#00bcd4', style: 'line' },
      { key: 'up', title: 'Upper', color: '#00bcd4', style: 'line', dashed: true },
      { key: 'lo', title: 'Lower', color: '#00bcd4', style: 'line', dashed: true }],
    calc: (bars, p) => {
      const m = ema(src(bars), p.length); const a = atr(bars, p.length);
      return { mid: m, up: m.map((v, i) => v + p.mult * a[i]), lo: m.map((v, i) => v - p.mult * a[i]) };
    },
  },
  {
    id: 'donchian', name: 'Donchian Channels', group: 'Volatility', overlay: true, params: [n('length', 'Length', 20)],
    plots: [{ key: 'up', title: 'Upper', color: '#26a69a', style: 'line' }, { key: 'lo', title: 'Lower', color: '#ef5350', style: 'line' },
      { key: 'mid', title: 'Mid', color: '#787b86', style: 'line', dashed: true }],
    calc: (bars, p) => {
      const up = highest(bars.map((b) => b.high), p.length); const lo = lowest(bars.map((b) => b.low), p.length);
      return { up, lo, mid: up.map((v, i) => (v + lo[i]) / 2) };
    },
  },

  // ---- Separate panes ---------------------------------------------------------------
  {
    id: 'volume', name: 'Volume', group: 'Volume', overlay: false, params: [],
    plots: [{ key: 'v', title: 'Vol', color: '#5d606b', style: 'histogram', colorByBar: true }],
    calc: (bars) => ({ v: bars.map((b) => b.volume || 0) }),
  },
  {
    id: 'rsi', name: 'Relative Strength Index', group: 'Oscillators', overlay: false, levels: [30, 50, 70],
    params: [n('length', 'Length', 14), source], plots: [{ key: 'v', title: 'RSI', color: '#7e57c2', style: 'line', lineWidth: 2 }],
    calc: (bars, p) => ({ v: rsi(src(bars, p.source), p.length) }),
  },
  {
    id: 'macd', name: 'MACD', group: 'Oscillators', overlay: false, levels: [0],
    params: [n('fast', 'Fast', 12), n('slow', 'Slow', 26), n('signal', 'Signal', 9), source],
    plots: [{ key: 'hist', title: 'Hist', color: '#26a69a', style: 'histogram', colorBySign: true },
      { key: 'macd', title: 'MACD', color: '#2962ff', style: 'line' }, { key: 'sig', title: 'Signal', color: '#ff6d00', style: 'line' }],
    calc: (bars, p) => {
      const x = src(bars, p.source); const f = ema(x, p.fast); const s = ema(x, p.slow);
      const macd = f.map((v, i) => v - s[i]); const sig = ema(macd, p.signal);
      return { macd, sig, hist: macd.map((v, i) => v - sig[i]) };
    },
  },
  {
    id: 'stoch', name: 'Stochastic', group: 'Oscillators', overlay: false, levels: [20, 80],
    params: [n('k', '%K', 14), n('d', '%D', 3), n('smooth', 'Smooth', 3)],
    plots: [{ key: 'k', title: '%K', color: '#2962ff', style: 'line' }, { key: 'd', title: '%D', color: '#ff6d00', style: 'line' }],
    calc: (bars, p) => {
      const hh = highest(bars.map((b) => b.high), p.k); const ll = lowest(bars.map((b) => b.low), p.k);
      const raw = bars.map((b, i) => (hh[i] === ll[i] ? 50 : (100 * (b.close - ll[i])) / (hh[i] - ll[i])));
      const k = sma(raw, p.smooth);
      return { k, d: sma(k, p.d) };
    },
  },
  {
    id: 'atr', name: 'Average True Range', group: 'Volatility', overlay: false, params: [n('length', 'Length', 14)],
    plots: [{ key: 'v', title: 'ATR', color: '#ef5350', style: 'line' }],
    calc: (bars, p) => ({ v: rma(trueRange(bars), p.length) }),
  },
  {
    id: 'cci', name: 'Commodity Channel Index', group: 'Oscillators', overlay: false, levels: [-100, 100],
    params: [n('length', 'Length', 20)], plots: [{ key: 'v', title: 'CCI', color: '#00bcd4', style: 'line' }],
    calc: (bars, p) => {
      const tp = src(bars, 'hlc3'); const m = sma(tp, p.length);
      return { v: tp.map((v, i) => {
        if (i < p.length - 1) return NaN;
        let md = 0;
        for (let j = i - p.length + 1; j <= i; j++) md += Math.abs(tp[j] - m[i]);
        md /= p.length;
        return md === 0 ? 0 : (v - m[i]) / (0.015 * md);
      }) };
    },
  },
  {
    id: 'willr', name: 'Williams %R', group: 'Oscillators', overlay: false, levels: [-80, -20],
    params: [n('length', 'Length', 14)], plots: [{ key: 'v', title: '%R', color: '#ab47bc', style: 'line' }],
    calc: (bars, p) => {
      const hh = highest(bars.map((b) => b.high), p.length); const ll = lowest(bars.map((b) => b.low), p.length);
      return { v: bars.map((b, i) => (hh[i] === ll[i] ? -50 : (-100 * (hh[i] - b.close)) / (hh[i] - ll[i]))) };
    },
  },
  {
    id: 'obv', name: 'On-Balance Volume', group: 'Volume', overlay: false, params: [],
    plots: [{ key: 'v', title: 'OBV', color: '#26a69a', style: 'line' }],
    calc: (bars) => {
      let acc = 0;
      return { v: bars.map((b, i) => {
        if (i) acc += b.close > bars[i - 1].close ? b.volume : b.close < bars[i - 1].close ? -b.volume : 0;
        return acc;
      }) };
    },
  },
];

export const BUILTIN_MAP = Object.fromEntries(BUILTINS.map((d) => [d.id, d]));

export function defaultParams(def) {
  return Object.fromEntries((def.params || []).map((p) => [p.key, p.default]));
}
