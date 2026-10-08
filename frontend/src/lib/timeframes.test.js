import test from 'node:test';
import assert from 'node:assert/strict';
import { BarBuilder, parseTimeframe } from './timeframes.js';

test('parse', () => {
  assert.equal(parseTimeframe('H1').seconds, 3600);
  assert.equal(parseTimeframe('5s').seconds, 5);
  assert.equal(parseTimeframe('250t').ticks, 250);
  assert.equal(parseTimeframe('nope'), null);
});

test('time bars', () => {
  const b = new BarBuilder(parseTimeframe('5s'), null);
  b.push(10_000, 1); b.push(12_000, 3);
  const bar = b.push(14_999, 2);
  assert.deepEqual([bar.time, bar.open, bar.high, bar.close], [10, 1, 3, 2]);
  assert.equal(b.push(15_000, 5).time, 15);
});

test('tick bars stay strictly increasing', () => {
  const b = new BarBuilder(parseTimeframe('2T'), null);
  const t = [];
  for (let i = 0; i < 6; i++) t.push(b.push(1000, i).time);
  assert.deepEqual([...new Set(t)], [1, 2, 3]);
});
