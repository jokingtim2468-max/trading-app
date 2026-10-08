// Vectorised TA helpers. Every function returns an array aligned 1:1 with its input,
// using NaN where the value is not yet defined. Exposed to custom indicators as `ta`.

export const src = (bars, key = 'close') => bars.map((b) => {
  switch (key) {
    case 'hl2': return (b.high + b.low) / 2;
    case 'hlc3': return (b.high + b.low + b.close) / 3;
    case 'ohlc4': return (b.open + b.high + b.low + b.close) / 4;
    default: return b[key];
  }
});

export function sma(x, n) {
  const out = new Array(x.length).fill(NaN);
  let sum = 0;
  for (let i = 0; i < x.length; i++) {
    sum += x[i];
    if (i >= n) sum -= x[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

export function ema(x, n) {
  const out = new Array(x.length).fill(NaN);
  const k = 2 / (n + 1);
  let prev = NaN;
  let seed = 0;
  let count = 0;
  for (let i = 0; i < x.length; i++) {
    if (Number.isNaN(x[i])) continue;
    if (count < n) {
      seed += x[i];
      count++;
      if (count === n) out[i] = prev = seed / n;
      continue;
    }
    out[i] = prev = x[i] * k + prev * (1 - k);
  }
  return out;
}

export const rma = (x, n) => {
  const out = new Array(x.length).fill(NaN);
  let prev = NaN;
  let seed = 0;
  let count = 0;
  for (let i = 0; i < x.length; i++) {
    if (Number.isNaN(x[i])) continue;
    if (count < n) { seed += x[i]; count++; if (count === n) out[i] = prev = seed / n; continue; }
    out[i] = prev = (prev * (n - 1) + x[i]) / n;
  }
  return out;
};

export function wma(x, n) {
  const out = new Array(x.length).fill(NaN);
  const denom = (n * (n + 1)) / 2;
  for (let i = n - 1; i < x.length; i++) {
    let s = 0;
    for (let j = 0; j < n; j++) s += x[i - j] * (n - j);
    out[i] = s / denom;
  }
  return out;
}

export function stdev(x, n) {
  const m = sma(x, n);
  return x.map((_, i) => {
    if (i < n - 1) return NaN;
    let s = 0;
    for (let j = i - n + 1; j <= i; j++) s += (x[j] - m[i]) ** 2;
    return Math.sqrt(s / n);
  });
}

function window(x, n, fn) {
  return x.map((_, i) => (i < n - 1 ? NaN : fn(x.slice(i - n + 1, i + 1))));
}
export const highest = (x, n) => window(x, n, (w) => Math.max(...w));
export const lowest = (x, n) => window(x, n, (w) => Math.min(...w));

export function trueRange(bars) {
  return bars.map((b, i) => (i === 0 ? b.high - b.low
    : Math.max(b.high - b.low, Math.abs(b.high - bars[i - 1].close), Math.abs(b.low - bars[i - 1].close))));
}
export const atr = (bars, n) => rma(trueRange(bars), n);

export function rsi(x, n) {
  const up = x.map((v, i) => (i === 0 ? NaN : Math.max(v - x[i - 1], 0)));
  const dn = x.map((v, i) => (i === 0 ? NaN : Math.max(x[i - 1] - v, 0)));
  const ru = rma(up, n);
  const rd = rma(dn, n);
  return ru.map((u, i) => (Number.isNaN(u) ? NaN : rd[i] === 0 ? 100 : 100 - 100 / (1 + u / rd[i])));
}

export const change = (x, n = 1) => x.map((v, i) => (i < n ? NaN : v - x[i - n]));

// ---- session clock ---------------------------------------------------------------
// Bar times from TradingView/Yahoo are true UTC epochs. MT5 bars are stamped in *broker
// server time*; most index/gold brokers run "New York + 7h" (server midnight = NY 17:00).
const nyFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York', hour12: false, year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit',
});
const pad = (n) => String(n).padStart(2, '0');

/** clockMode: 'utc' | 'ny+7' | 'utc+2' | 'utc+3' ... -> fn(time) => {day, minutes} in New York local time. */
export function makeClock(clockMode = 'utc') {
  const m = /^(ny|utc)([+-]\d+)?$/.exec(clockMode) || ['', 'utc', '0'];
  const shift = Number(m[2] || 0) * 3600;
  if (m[1] === 'ny') {
    return (time) => {
      const d = new Date((time - shift) * 1000);
      return { day: `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`,
        minutes: d.getUTCHours() * 60 + d.getUTCMinutes() };
    };
  }
  const cache = new Map();
  return (time) => {
    const key = time - (time % 60);
    let v = cache.get(key);
    if (!v) {
      const parts = Object.fromEntries(nyFmt.formatToParts(new Date((key - shift) * 1000)).map((p) => [p.type, p.value]));
      v = { day: `${parts.year}-${parts.month}-${parts.day}`, minutes: (Number(parts.hour) % 24) * 60 + Number(parts.minute) };
      cache.set(key, v);
    }
    return v;
  };
}

/** Split bars into trading sessions (CME/CFD day rolls at 18:00 New York). */
export function sessions(bars, clock) {
  const idx = new Array(bars.length);
  const days = [];
  let key = null;
  for (let i = 0; i < bars.length; i++) {
    const k = clock(bars[i].time + 6 * 3600).day;
    if (k !== key) {
      key = k;
      days.push({ key: k, start: i, open: bars[i].open, high: -Infinity, low: Infinity, close: NaN });
    }
    const d = days[days.length - 1];
    d.high = Math.max(d.high, bars[i].high);
    d.low = Math.min(d.low, bars[i].low);
    d.close = bars[i].close;
    idx[i] = days.length - 1;
  }
  return { idx, days };
}

export const ta = { src, sma, ema, rma, wma, stdev, highest, lowest, trueRange, atr, rsi, change, makeClock, sessions };
