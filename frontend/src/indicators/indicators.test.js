import test from 'node:test';
import assert from 'node:assert/strict';
import { BUILTINS, BUILTIN_MAP, defaultParams } from './builtins.js';
import { evaluate, compileCustom, CUSTOM_TEMPLATE } from './engine.js';
import { makeClock, sma, ema, rsi } from './ta.js';

// Three NY sessions of 5-minute bars, true-UTC timestamps (EDT, UTC-4).
const bars = [];
const start = Date.UTC(2026, 5, 1, 22, 0) / 1000; // Mon 18:00 NY
for (let i = 0; i < 3 * 288; i++) {
  const t = start + i * 300;
  const p = 20000 + 50 * Math.sin(i / 20) + i * 0.1;
  bars.push({ time: t, open: p, high: p + 5, low: p - 5, close: p + 1, volume: 100 });
}
const ctx = { clock: makeClock('utc'), tfSeconds: 300, tfKind: 'time' };

test('ta basics', () => {
  assert.deepEqual(sma([1, 2, 3, 4], 2).slice(1), [1.5, 2.5, 3.5]);
  assert.equal(ema([1, 1, 1, 1], 2)[3], 1);
  assert.ok(rsi([1, 2, 3, 4, 5, 6], 3)[5] === 100);
});

test('every builtin evaluates without throwing and returns aligned arrays', () => {
  for (const def of BUILTINS) {
    const res = evaluate(def, bars, defaultParams(def), ctx);
    for (const p of res.plots) assert.equal(p.values.length, bars.length, `${def.id}.${p.key}`);
  }
});

test('previous-day levels equal prior session extremes', () => {
  const res = evaluate(BUILTIN_MAP.pdhl, bars, {}, ctx);
  const day1 = bars.slice(0, 288);
  const pdh = res.plots.find((p) => p.key === 'pdh').values;
  assert.ok(Number.isNaN(pdh[0]));
  assert.equal(pdh[300], Math.max(...day1.map((b) => b.high)));
});

test('opening range is set after 09:45 NY and spans 09:30-09:45', () => {
  const res = evaluate(BUILTIN_MAP.orb, bars, { minutes: 15, start: '09:30' }, ctx);
  const orh = res.plots.find((p) => p.key === 'high').values;
  const i930 = bars.findIndex((b) => ctx.clock(b.time).minutes === 570);
  assert.ok(Number.isNaN(orh[i930 + 1]));
  assert.equal(orh[i930 + 3], Math.max(...bars.slice(i930, i930 + 3).map((b) => b.high)));
});

test('ADR projection brackets the session', () => {
  const res = evaluate(BUILTIN_MAP.adr, bars, { length: 1, mult: 1 }, ctx);
  const ph = res.plots[0].values; const pl = res.plots[1].values;
  const last = bars.length - 1;
  assert.ok(ph[last] >= bars[last].high && pl[last] <= bars[last].low);
});

test('custom template compiles and runs', () => {
  const fn = compileCustom(CUSTOM_TEMPLATE);
  const res = evaluate({ custom: true, code: CUSTOM_TEMPLATE }, bars, {}, ctx);
  assert.equal(typeof fn, 'function');
  assert.equal(res.plots.length, 3);
  assert.ok(res.overlay);
});

test('ny+7 broker clock maps server midnight to 17:00 NY', () => {
  const c = makeClock('ny+7');
  assert.equal(c(Date.UTC(2026, 0, 5, 0, 0) / 1000).minutes, 17 * 60);
});
