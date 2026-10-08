import { useState } from 'react';
import { useStore } from '../lib/store.js';
import { fmtMoney } from '../lib/format.js';
import { tradeRequest } from '../lib/trading.js';

const FILTERS = ['all', 'manual', 'ea'];

function SourceTag({ row }) {
  return <span className={`tag ${row.source}`} title={row.comment}>{row.label}</span>;
}

export default function PositionsPanel() {
  const positions = useStore((s) => s.positions);
  const orders = useStore((s) => s.orders);
  const account = useStore((s) => s.account);
  const tradeLog = useStore((s) => s.tradeLog);
  const [tab, setTab] = useState('positions');
  const [filter, setFilter] = useState('all');
  const keep = (r) => filter === 'all' || r.source === filter;
  const pos = positions.filter(keep);
  const ord = orders.filter(keep);
  const magics = [...new Set(positions.filter((p) => p.magic).map((p) => p.magic))];

  return (
    <section className="panel">
      <div className="panel-tabs">
        <button type="button" className={tab === 'positions' ? 'on' : ''} onClick={() => setTab('positions')}>Positions ({positions.length})</button>
        <button type="button" className={tab === 'orders' ? 'on' : ''} onClick={() => setTab('orders')}>Orders ({orders.length})</button>
        <button type="button" className={tab === 'log' ? 'on' : ''} onClick={() => setTab('log')}>EA activity ({tradeLog.length})</button>
        <span className="sep" />
        {FILTERS.map((f) => (
          <button key={f} type="button" className={`btn-sm ${filter === f ? 'on' : ''}`} onClick={() => setFilter(f)}>
            {f === 'ea' ? `EA (${magics.length} magic)` : f}
          </button>
        ))}
        {account && (
          <span className="panel-acct">
            {account.login} · {account.account_type} · Margin {fmtMoney(account.margin)} · Free {fmtMoney(account.margin_free)}
            {account.margin_level ? ` · ML ${account.margin_level.toFixed(0)}%` : ''} · P/L{' '}
            <b className={account.profit >= 0 ? 'up' : 'down'}>{fmtMoney(account.profit, account.currency)}</b>
          </span>
        )}
      </div>
      <div className="panel-body">
        {tab === 'positions' && (
          <table>
            <thead><tr><th>Ticket</th><th>Symbol</th><th>Type</th><th>Vol</th><th>Open</th><th>SL</th><th>TP</th><th>Price</th><th>Profit</th><th>Source</th><th>Comment</th><th /></tr></thead>
            <tbody>
              {pos.map((p) => (
                <tr key={p.ticket}>
                  <td>{p.ticket}</td><td>{p.symbol}</td><td className={p.side === 'buy' ? 'up' : 'down'}>{p.side.toUpperCase()}</td>
                  <td>{p.volume}</td><td>{p.price_open}</td><td>{p.sl || '—'}</td><td>{p.tp || '—'}</td><td>{p.price_current}</td>
                  <td className={p.profit >= 0 ? 'up' : 'down'}>{fmtMoney(p.profit)}</td><td><SourceTag row={p} /></td>
                  <td className="muted">{p.comment}</td>
                  <td><button type="button" className="btn-x" onClick={() => tradeRequest('close', { ticket: p.ticket }, `Closed #${p.ticket}`)}>Close</button></td>
                </tr>
              ))}
              {!pos.length && <tr><td colSpan={12} className="muted center">No open positions</td></tr>}
            </tbody>
          </table>
        )}
        {tab === 'orders' && (
          <table>
            <thead><tr><th>Ticket</th><th>Symbol</th><th>Type</th><th>Vol</th><th>Price</th><th>SL</th><th>TP</th><th>Source</th><th>Comment</th><th /></tr></thead>
            <tbody>
              {ord.map((o) => (
                <tr key={o.ticket}>
                  <td>{o.ticket}</td><td>{o.symbol}</td><td className={o.side === 'buy' ? 'up' : 'down'}>{o.type_name}</td><td>{o.volume}</td>
                  <td>{o.price_open}</td><td>{o.sl || '—'}</td><td>{o.tp || '—'}</td><td><SourceTag row={o} /></td><td className="muted">{o.comment}</td>
                  <td><button type="button" className="btn-x" onClick={() => tradeRequest('cancel_order', { ticket: o.ticket }, `Cancelled #${o.ticket}`)}>Cancel</button></td>
                </tr>
              ))}
              {!ord.length && <tr><td colSpan={10} className="muted center">No pending orders</td></tr>}
            </tbody>
          </table>
        )}
        {tab === 'log' && (
          <ul className="log">
            {tradeLog.map((l) => <li key={l.id}><span className="muted">{new Date(l.time).toLocaleTimeString()}</span> {l.message}</li>)}
            {!tradeLog.length && <li className="muted">Opens, SL/TP changes and closes by EAs and manual trades appear here live.</li>}
          </ul>
        )}
      </div>
    </section>
  );
}
