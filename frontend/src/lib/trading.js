// Thin helpers so every order path (toolbar, context menu, modal, drag) reports the same way.
import { request } from './ws.js';
import { useStore } from './store.js';

const kindName = (o) => (o.kind === 'market' ? o.side.toUpperCase() : `${o.side.toUpperCase()} ${o.kind.toUpperCase()}`);

export async function placeOrder(order) {
  const { toast } = useStore.getState();
  try {
    const res = await request('order', order);
    toast('success', `${kindName(order)} ${order.volume} ${order.symbol} → #${res.order} @ ${res.price} (${res.comment})`);
    return res;
  } catch (err) {
    toast('error', `Order rejected: ${err.message}`);
    throw err;
  }
}

export async function tradeRequest(type, payload, okMessage) {
  const { toast } = useStore.getState();
  try {
    const res = await request(type, payload);
    if (okMessage) toast('success', okMessage);
    return res;
  } catch (err) {
    toast('error', `${type.replace('_', ' ')} failed: ${err.message}`);
    throw err;
  }
}
