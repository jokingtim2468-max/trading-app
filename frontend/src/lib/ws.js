// Single multiplexed WebSocket to the Python bridge: request/response by id + push events.
const listeners = new Map(); // type -> Set<fn>
const pending = new Map();   // id -> {resolve, reject, timer}
let socket = null;
let nextId = 1;
let backoff = 500;
const queue = [];

function url() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = new URLSearchParams(location.search).get('token');
  return `${proto}://${location.host}/ws${token ? `?token=${encodeURIComponent(token)}` : ''}`;
}

function emit(type, msg) {
  listeners.get(type)?.forEach((fn) => {
    try { fn(msg); } catch (e) { console.error(`listener ${type}`, e); }
  });
}

export function connect() {
  if (socket && socket.readyState <= 1) return;
  socket = new WebSocket(url());
  socket.onopen = () => {
    backoff = 500;
    emit('open', {});
    while (queue.length) socket.send(queue.shift());
  };
  socket.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'response') {
      const p = pending.get(msg.id);
      if (!p) return;
      pending.delete(msg.id);
      clearTimeout(p.timer);
      msg.ok ? p.resolve(msg.data) : p.reject(new Error(msg.error));
      return;
    }
    emit(msg.type, msg);
  };
  socket.onclose = () => {
    emit('close', {});
    for (const [id, p] of pending) { p.reject(new Error('bridge disconnected')); pending.delete(id); }
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 8000);
  };
}

export function request(type, payload = {}, timeoutMs = 30000) {
  const id = nextId++;
  const data = JSON.stringify({ id, type, ...payload });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${type} timed out`)); }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    if (socket?.readyState === 1) socket.send(data); else queue.push(data);
  });
}

export function on(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn);
  return () => listeners.get(type).delete(fn);
}
