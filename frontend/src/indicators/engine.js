// Indicator evaluation: built-ins and user-written JS indicators share one output shape.
import { BUILTIN_MAP, defaultParams } from './builtins.js';
import { ta } from './ta.js';

export const CUSTOM_TEMPLATE = `// Custom indicator - plain JavaScript, runs locally in your browser.
// Available: bars [{time, open, high, low, close, volume}], ta (sma, ema, rma, wma, stdev,
// highest, lowest, atr, rsi, trueRange, sessions, makeClock), params, ctx {clock, tfSeconds}.
// Return { plots: [{ title, values, color, pane: 'overlay' | 'new', style: 'line'|'step'|'histogram' }] }
const close = ta.src(bars, 'close');
const len = params.length ?? 20;
const basis = ta.ema(close, len);
const a = ta.atr(bars, 14);
return {
  plots: [
    { title: 'Basis', values: basis, color: '#f7a600', pane: 'overlay' },
    { title: 'Upper', values: basis.map((v, i) => v + 2 * a[i]), color: '#26a69a', pane: 'overlay', dashed: true },
    { title: 'Lower', values: basis.map((v, i) => v - 2 * a[i]), color: '#ef5350', pane: 'overlay', dashed: true },
  ],
};`;

const compiled = new Map();

export function compileCustom(code) {
  if (!compiled.has(code)) {
    // eslint-disable-next-line no-new-func
    compiled.set(code, new Function('bars', 'ta', 'params', 'ctx', `"use strict";\n${code}`));
  }
  return compiled.get(code);
}

/** Resolve an indicator instance to its definition (built-in or custom). */
export function definitionOf(inst, customs) {
  if (inst.defId.startsWith('custom:')) {
    const c = customs.find((x) => x.id === inst.defId);
    return c ? { id: c.id, name: c.name, group: 'Custom', custom: true, code: c.code, params: c.params || [] } : null;
  }
  return BUILTIN_MAP[inst.defId] || null;
}

export function newInstance(def) {
  return { uid: Math.random().toString(36).slice(2, 10), defId: def.id, params: defaultParams(def), visible: true };
}

/** -> { overlay: bool, levels: number[], plots: [{key,title,color,style,lineWidth,dashed,dotted,values,...}] } */
export function evaluate(def, bars, params, ctx) {
  if (def.custom) {
    const res = compileCustom(def.code)(bars, ta, params, ctx) || {};
    const plots = (res.plots || []).map((p, i) => ({
      key: `p${i}`, title: p.title || `Plot ${i + 1}`, color: p.color || '#2962ff', style: p.style || 'line',
      lineWidth: p.lineWidth || 1, dashed: p.dashed, dotted: p.dotted, values: p.values || [], pane: p.pane,
    }));
    return { overlay: plots.every((p) => p.pane !== 'new'), levels: res.levels || [], plots };
  }
  const out = def.calc(bars, params, ctx);
  return {
    overlay: def.overlay,
    levels: def.levels || [],
    plots: def.plots.map((p) => ({ ...p, values: out[p.key] || [] })),
  };
}
