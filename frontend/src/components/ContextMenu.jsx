import { useEffect } from 'react';
import { useStore } from '../lib/store.js';
import { placeOrder, tradeRequest } from '../lib/trading.js';

export default function ContextMenu() {
  const modal = useStore((s) => s.modal);
  const s = useStore.getState();
  const mt5Connected = useStore((st) => st.status.mt5?.connected);
  useEffect(() => {
    if (modal?.type !== 'context') return undefined;
    const close = () => s.closeModal();
    window.addEventListener('scroll', close, true);
    return () => window.removeEventListener('scroll', close, true);
  }, [modal]);
  if (modal?.type !== 'context') return null;

  const { x, y, price, level, last, meta } = modal;
  const tradable = meta?.tradable && mt5Connected;
  const volume = s.settings.defaultVolume;
  const send = (side, kind) => {
    s.closeModal();
    const order = { symbol: meta.symbol, side, kind, volume, price };
    if (s.settings.oneClick) placeOrder(order).catch(() => {});
    else s.openModal('order', { order });
  };
  const items = [];
  if (level) {
    const r = level.ref;
    if (!level.isOrder) {
      items.push({ label: `Close ${r.side.toUpperCase()} ${r.volume} #${r.ticket} (${r.label})`, danger: true,
        run: () => tradeRequest('close', { ticket: r.ticket }, `Closed #${r.ticket}`) });
      if (r.sl) items.push({ label: 'Remove stop loss', run: () => tradeRequest('modify_sltp', { ticket: r.ticket, sl: 0, tp: r.tp }, 'SL removed') });
      if (r.tp) items.push({ label: 'Remove take profit', run: () => tradeRequest('modify_sltp', { ticket: r.ticket, sl: r.sl, tp: 0 }, 'TP removed') });
    } else {
      items.push({ label: `Cancel ${r.type_name} #${r.ticket}`, danger: true, run: () => tradeRequest('cancel_order', { ticket: r.ticket }, `Cancelled #${r.ticket}`) });
    }
    items.push({ sep: true });
  }
  if (tradable && price != null) {
    const above = price > last;
    items.push({ label: `Buy ${above ? 'Stop' : 'Limit'} ${volume} @ ${price}`, cls: 'up', run: () => send('buy', above ? 'stop' : 'limit') });
    items.push({ label: `Sell ${above ? 'Limit' : 'Stop'} ${volume} @ ${price}`, cls: 'down', run: () => send('sell', above ? 'limit' : 'stop') });
    items.push({ label: 'New order…', run: () => { s.closeModal(); s.openModal('order', { order: { symbol: meta.symbol, side: 'buy', kind: 'market', volume } }); } });
  } else {
    items.push({ label: meta?.source === 'external' ? 'Trading disabled - external (chart-only) feed' : 'Trading disabled - MT5 offline', disabled: true });
  }

  return (
    <div className="ctx-backdrop" onMouseDown={() => s.closeModal()} onContextMenu={(e) => { e.preventDefault(); s.closeModal(); }}>
      <ul className="ctx" style={{ left: Math.min(x, window.innerWidth - 260), top: Math.min(y, window.innerHeight - 220) }} onMouseDown={(e) => e.stopPropagation()}>
        {items.map((it, i) => (it.sep ? <li key={i} className="ctx-sep" /> : (
          <li key={i}>
            <button type="button" disabled={it.disabled} className={`${it.cls || ''} ${it.danger ? 'danger' : ''}`}
              onClick={() => { if (!it.disabled) { s.closeModal(); it.run(); } }}>{it.label}</button>
          </li>
        )))}
      </ul>
    </div>
  );
}
