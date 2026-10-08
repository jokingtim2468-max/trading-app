// HTML5 canvas overlay for line studies. Points are stored as {time, price} so drawings
// survive zooming, scrolling and timeframe changes, and persist per symbol in localStorage.
import { THEME } from '../lib/theme.js';

export const DRAWING_TOOLS = [
  { id: 'cursor', label: 'Cursor', icon: '↖' },
  { id: 'trend', label: 'Trend line', icon: '╱' },
  { id: 'ray', label: 'Ray', icon: '⟋' },
  { id: 'hline', label: 'Horizontal line', icon: '─' },
  { id: 'rect', label: 'Zone / rectangle', icon: '▭' },
  { id: 'fib', label: 'Fib retracement', icon: 'ƒ' },
  { id: 'erase', label: 'Eraser', icon: '⌫' },
];
const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618];
const COLORS = { trend: '#2962ff', ray: '#00bcd4', hline: '#f7a600', rect: 'rgba(41,98,255,0.15)', fib: '#9598a1' };

const load = (k) => { try { return JSON.parse(localStorage.getItem(k)) || []; } catch { return []; } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } };

export class DrawingLayer {
  constructor(container, ctrl, { onToolDone } = {}) {
    this.el = container;
    this.ctrl = ctrl;
    this.onToolDone = onToolDone;
    this.tool = 'cursor';
    this.key = null;
    this.drawings = [];
    this.pending = null;
    this.mouse = null;

    this.canvas = document.createElement('canvas');
    Object.assign(this.canvas.style, { position: 'absolute', inset: '0', zIndex: 3, pointerEvents: 'none' });
    container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');

    this.onClick = this.#onClick.bind(this);
    this.onMove = (e) => { this.mouse = this.#pt(e); };
    this.canvas.addEventListener('click', this.onClick);
    this.canvas.addEventListener('mousemove', this.onMove);
    this.alive = true;
    this.#loop();
  }

  destroy() {
    this.alive = false;
    this.canvas.remove();
  }

  setSymbol(symbol) {
    this.key = `drawings:${symbol}`;
    this.drawings = load(this.key);
    this.pending = null;
  }

  setTool(tool) {
    this.tool = tool;
    this.pending = null;
    const active = tool !== 'cursor';
    this.canvas.style.pointerEvents = active ? 'auto' : 'none';
    this.canvas.style.cursor = tool === 'erase' ? 'not-allowed' : active ? 'crosshair' : '';
  }

  clear() {
    this.drawings = [];
    save(this.key, this.drawings);
  }

  #pt(e) {
    const r = this.el.getBoundingClientRect();
    const x = e.clientX - r.left; const y = e.clientY - r.top;
    return { x, y, time: this.ctrl.xToTime(x), price: this.ctrl.yToPrice(y) };
  }

  #onClick(e) {
    const p = this.#pt(e);
    if (p.time == null || p.price == null) return;
    const point = { time: p.time, price: p.price };
    if (this.tool === 'erase') {
      const hit = this.#nearest(p.x, p.y);
      if (hit) { this.drawings = this.drawings.filter((d) => d !== hit); save(this.key, this.drawings); }
      return;
    }
    if (this.tool === 'hline') {
      this.#add({ type: 'hline', p1: point, p2: point });
      return;
    }
    if (!this.pending) { this.pending = { type: this.tool, p1: point }; return; }
    this.#add({ ...this.pending, p2: point });
  }

  #add(d) {
    this.drawings.push({ id: Date.now().toString(36), color: COLORS[d.type], ...d });
    this.pending = null;
    save(this.key, this.drawings);
    this.onToolDone?.();
  }

  #xy(pt) { return { x: this.ctrl.timeToX(pt.time), y: this.ctrl.priceToY(pt.price) }; }

  #nearest(x, y) {
    let best = null; let bestD = 8;
    for (const d of this.drawings) {
      const a = this.#xy(d.p1); const b = this.#xy(d.p2);
      if (a.x == null || a.y == null || b.y == null) continue;
      let dist;
      if (d.type === 'hline') dist = Math.abs(y - a.y);
      else if (d.type === 'rect' || d.type === 'fib') {
        const inside = x >= Math.min(a.x, b.x) && x <= Math.max(a.x, b.x) && y >= Math.min(a.y, b.y) && y <= Math.max(a.y, b.y);
        dist = inside ? 0 : Infinity;
      } else dist = segDist(x, y, a, b ?? a);
      if (dist < bestD) { bestD = dist; best = d; }
    }
    return best;
  }

  #loop() {
    if (!this.alive) return;
    this.#render();
    setTimeout(() => requestAnimationFrame(() => this.#loop()), 66); // ~15 fps is plenty
  }

  #render() {
    const dpr = window.devicePixelRatio || 1;
    const w = this.el.clientWidth; const h = this.el.clientHeight;
    if (this.canvas.width !== w * dpr || this.canvas.height !== h * dpr) {
      this.canvas.width = w * dpr; this.canvas.height = h * dpr;
      this.canvas.style.width = `${w}px`; this.canvas.style.height = `${h}px`;
    }
    const c = this.ctx;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    if (!this.drawings.length && !this.pending) return;
    const pane = this.ctrl.mainPaneSize();
    c.save();
    c.beginPath(); c.rect(0, 0, pane.width, pane.height); c.clip();
    const items = [...this.drawings];
    if (this.pending && this.mouse?.time != null) {
      items.push({ ...this.pending, p2: { time: this.mouse.time, price: this.mouse.price }, color: COLORS[this.pending.type], preview: true });
    }
    for (const d of items) this.#draw(c, d, pane);
    c.restore();
  }

  #draw(c, d, pane) {
    const a = this.#xy(d.p1); const b = this.#xy(d.p2);
    if (a.y == null || b.y == null) return;
    c.lineWidth = 1.5;
    c.strokeStyle = d.color;
    c.setLineDash(d.preview ? [4, 4] : []);
    c.font = `11px ${THEME.font}`;
    switch (d.type) {
      case 'hline':
        line(c, 0, a.y, pane.width, a.y);
        c.fillStyle = d.color; c.fillText(d.p1.price.toFixed(2), 4, a.y - 3);
        break;
      case 'trend':
        if (a.x != null && b.x != null) line(c, a.x, a.y, b.x, b.y);
        break;
      case 'ray': {
        if (a.x == null || b.x == null) break;
        const dx = b.x - a.x || 1e-6; const k = (b.y - a.y) / dx;
        const xEnd = dx > 0 ? pane.width : 0;
        line(c, a.x, a.y, xEnd, a.y + k * (xEnd - a.x));
        break;
      }
      case 'rect':
        if (a.x == null || b.x == null) break;
        c.fillStyle = d.color;
        c.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        c.strokeStyle = 'rgba(41,98,255,0.8)';
        c.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        break;
      case 'fib': {
        if (a.x == null || b.x == null) break;
        const x0 = Math.min(a.x, b.x);
        const range = d.p2.price - d.p1.price;
        FIB.forEach((lvl) => {
          const price = d.p2.price - range * lvl;
          const y = this.ctrl.priceToY(price);
          if (y == null) return;
          c.strokeStyle = lvl === 0.5 || lvl === 0.618 ? '#f7a600' : d.color;
          line(c, x0, y, pane.width, y);
          c.fillStyle = c.strokeStyle;
          c.fillText(`${lvl} (${price.toFixed(2)})`, x0 + 4, y - 3);
        });
        c.setLineDash([2, 3]);
        c.strokeStyle = d.color;
        line(c, a.x, a.y, b.x, b.y);
        break;
      }
      default:
    }
  }
}

function line(c, x1, y1, x2, y2) { c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2); c.stroke(); }

function segDist(px, py, a, b) {
  const dx = b.x - a.x; const dy = b.y - a.y;
  const len = dx * dx + dy * dy || 1;
  const t = Math.max(0, Math.min(1, ((px - a.x) * dx + (py - a.y) * dy) / len));
  return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
}
