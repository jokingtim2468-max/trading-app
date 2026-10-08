// Mirrors backend/app/timeframes.py so live ticks build bars identical to history.
const UNIT = { s: 1, m: 60, h: 3600, d: 86400, w: 604800, mn: 2592000 };

export const PRESET_TIMEFRAMES = ['1s', '5s', '15s', 'M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1', '100T'];

export function parseTimeframe(raw) {
  const s = String(raw).trim();
  let m = s.match(/^(\d+)\s*t$/i);
  if (m) return { label: `${+m[1]}T`, kind: 'tick', ticks: +m[1], seconds: 0 };
  m = s.toUpperCase().match(/^(MN|[MHDW])(\d+)$/);
  let n, unit;
  if (m) { unit = { M: 'm', H: 'h', D: 'd', W: 'w', MN: 'mn' }[m[1]]; n = +m[2]; }
  else {
    m = s.toLowerCase().match(/^(\d+)\s*(mn|s|m|h|d|w)$/);
    if (!m) return null;
    n = +m[1]; unit = m[2];
  }
  if (n < 1) return null;
  return { label: `${n}${unit}`, kind: 'time', seconds: n * UNIT[unit], ticks: 0 };
}

// Live aggregator: feed it ticks, it returns the bar to update (same object while it grows).
export class BarBuilder {
  constructor(tf, lastBar) {
    this.tf = tf;
    this.bar = lastBar ? { ...lastBar } : null;
    this.count = tf.kind === 'tick' ? tf.ticks : 0; // assume last history bar is complete
  }

  push(tMsc, price) {
    const sec = Math.floor(tMsc / 1000);
    const { tf } = this;
    if (tf.kind === 'tick') {
      if (this.bar && this.count < tf.ticks) return this.#grow(price);
      const time = this.bar && sec <= this.bar.time ? this.bar.time + 1 : sec;
      this.count = 1;
      return (this.bar = { time, open: price, high: price, low: price, close: price, volume: 1 });
    }
    // W1/MN1 buckets come from the broker; never open a new one ahead of history.
    const bucket = tf.seconds >= 604800 && this.bar ? this.bar.time : sec - (sec % tf.seconds);
    if (this.bar && bucket <= this.bar.time) return this.#grow(price);
    return (this.bar = { time: bucket, open: price, high: price, low: price, close: price, volume: 1 });
  }

  replace(bar) { this.bar = { ...bar }; }

  #grow(price) {
    const b = this.bar;
    b.high = Math.max(b.high, price);
    b.low = Math.min(b.low, price);
    b.close = price;
    b.volume += 1;
    this.count += 1;
    return b;
  }
}
