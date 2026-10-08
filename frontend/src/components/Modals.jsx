import { useEffect, useMemo, useState } from 'react';
import { request } from '../lib/ws.js';
import { useStore } from '../lib/store.js';
import { BUILTINS } from '../indicators/builtins.js';
import { CUSTOM_TEMPLATE, compileCustom, definitionOf } from '../indicators/engine.js';
import { placeOrder } from '../lib/trading.js';
import { fmtMoney } from '../lib/format.js';

function Modal({ title, children, wide, onClose }) {
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? 'wide' : ''}`} role="dialog" aria-label={title}>
        <div className="modal-head"><b>{title}</b><button type="button" className="btn-sm" onClick={onClose}>✕</button></div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

const Field = ({ label, children }) => <label className="field"><span>{label}</span>{children}</label>;

// ------------------------------------------------------------------ MT5
function Mt5Modal({ onClose }) {
  const status = useStore((s) => s.status.mt5);
  const account = useStore((s) => s.account);
  const [f, setF] = useState({ server: '', login: '', password: '', path: '' });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr('');
    try {
      await request('mt5_login', f, 90000);
      useStore.getState().toast('success', `MT5 connected: ${f.login} @ ${f.server}`, 'mt5');
      setF({ ...f, password: '' });
    } catch (ex) { setErr(ex.message); }
    setBusy(false);
  };
  return (
    <Modal title="MetaTrader 5 account" onClose={onClose}>
      <p className={`status-line ${status?.status}`}>
        Status: <b>{status?.status}</b> · mode {status?.mode}{status?.error ? ` · ${status.error}` : ''}
      </p>
      {account && <p className="muted">Logged in: {account.login} · {account.server} · {account.company} · {account.account_type} · Balance {fmtMoney(account.balance, account.currency)}</p>}
      {status?.sim && <p className="hint">Simulator active (no MetaTrader5 package on this OS, or BRIDGE_SIM=1). Any credentials are accepted.</p>}
      <form onSubmit={submit}>
        <Field label="Broker server"><input required value={f.server} placeholder="e.g. ICMarketsSC-Demo" onChange={(e) => setF({ ...f, server: e.target.value })} /></Field>
        <Field label="Login (account #)"><input required inputMode="numeric" value={f.login} onChange={(e) => setF({ ...f, login: e.target.value })} /></Field>
        <Field label="Password"><input required type="password" autoComplete="current-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
        <Field label="terminal64.exe path (optional)"><input value={f.path} placeholder="C:\Program Files\MetaTrader 5\terminal64.exe" onChange={(e) => setF({ ...f, path: e.target.value })} /></Field>
        <p className="hint">Credentials go only to your local bridge for <code>mt5.login()</code>; they are never stored in the browser.</p>
        {err && <p className="error">{err}</p>}
        <div className="modal-actions">
          {status?.connected && <button type="button" className="btn" onClick={() => request('mt5_logout')}>Log out</button>}
          <button type="submit" className="btn primary" disabled={busy}>{busy ? 'Connecting…' : 'Connect'}</button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ TradingView
function TvModal({ onClose }) {
  const tv = useStore((s) => s.status.tv);
  const [mode, setMode] = useState('session');
  const [f, setF] = useState({ sessionid: '', sessionid_sign: '', username: '', password: '' });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [lists, setLists] = useState(null);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr('');
    try {
      const payload = mode === 'session' ? { sessionid: f.sessionid, sessionid_sign: f.sessionid_sign }
        : { username: f.username, password: f.password };
      const info = await request('tv_auth', payload, 60000);
      useStore.getState().toast('success', `TradingView: signed in as ${info.username}`, 'tv');
      setF({ sessionid: '', sessionid_sign: '', username: '', password: '' });
    } catch (ex) { setErr(ex.message); }
    setBusy(false);
  };
  const loadLists = () => request('tv_watchlists').then(setLists).catch((e) => setErr(e.message));
  const open = (sym) => { useStore.getState().setSymbol(sym); onClose(); };
  return (
    <Modal title="TradingView account" onClose={onClose}>
      <p className="muted">Current: <b>{tv?.username || 'anonymous'}</b> ({tv?.mode}{tv?.plan ? ` · ${tv.plan}` : ''})</p>
      <div className="seg">
        <button type="button" className={mode === 'session' ? 'on' : ''} onClick={() => setMode('session')}>sessionid cookie</button>
        <button type="button" className={mode === 'creds' ? 'on' : ''} onClick={() => setMode('creds')}>username / password</button>
      </div>
      <form onSubmit={submit}>
        {mode === 'session' ? (
          <>
            <Field label="sessionid"><input required type="password" value={f.sessionid} onChange={(e) => setF({ ...f, sessionid: e.target.value })} /></Field>
            <Field label="sessionid_sign (optional)"><input type="password" value={f.sessionid_sign} onChange={(e) => setF({ ...f, sessionid_sign: e.target.value })} /></Field>
            <p className="hint">tradingview.com → DevTools (F12) → Application → Cookies → copy <code>sessionid</code>. This is your own login; data access follows your TradingView plan.</p>
          </>
        ) : (
          <>
            <Field label="Username / e-mail"><input required value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} /></Field>
            <Field label="Password"><input required type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
            <p className="hint">Password login fails on accounts with 2FA or when TradingView shows a captcha - use the sessionid instead.</p>
          </>
        )}
        {err && <p className="error">{err}</p>}
        <div className="modal-actions">
          {tv?.mode === 'session' && <button type="button" className="btn" onClick={loadLists}>Load my watchlists</button>}
          <button type="submit" className="btn primary" disabled={busy}>{busy ? 'Signing in…' : 'Sign in'}</button>
        </div>
      </form>
      {lists && lists.map((w) => (
        <details key={w.id} className="watchlist">
          <summary>{w.name} ({w.symbols.length})</summary>
          <div className="chips">{w.symbols.map((sym) => <button key={sym} type="button" className="chip" onClick={() => open(sym)}>{sym}</button>)}</div>
        </details>
      ))}
    </Modal>
  );
}

// ------------------------------------------------------------------ Indicators
function ParamInput({ p, value, onChange }) {
  if (p.type === 'select') {
    return <select value={value} onChange={(e) => onChange(e.target.value)}>{p.options.map((o) => <option key={o}>{o}</option>)}</select>;
  }
  if (p.type === 'text') return <input value={value} onChange={(e) => onChange(e.target.value)} />;
  return <input type="number" value={value} step={p.step || 1} min={p.min} onChange={(e) => onChange(Number(e.target.value))} />;
}

function IndicatorModal({ onClose, tab: initialTab }) {
  const s = useStore();
  const chart = s.activeChart();
  const [q, setQ] = useState('');
  const [tab, setTab] = useState(initialTab || 'library');
  const [edit, setEdit] = useState({ id: '', name: 'My indicator', code: CUSTOM_TEMPLATE });
  const [codeErr, setCodeErr] = useState('');
  const results = useMemo(() => {
    const all = [...BUILTINS, ...s.customIndicators.map((c) => ({ id: c.id, name: c.name, group: 'Custom', description: 'Your JavaScript indicator' }))];
    const needle = q.toLowerCase();
    return all.filter((d) => !needle || `${d.name} ${d.group} ${d.id}`.toLowerCase().includes(needle));
  }, [q, s.customIndicators]);
  const grouped = results.reduce((acc, d) => ({ ...acc, [d.group]: [...(acc[d.group] || []), d] }), {});

  const saveCustom = () => {
    try { compileCustom(edit.code); } catch (ex) { setCodeErr(ex.message); return; }
    const id = edit.id || `custom:${Date.now().toString(36)}`;
    s.saveCustomIndicator({ id, name: edit.name, code: edit.code });
    setEdit({ ...edit, id });
    setCodeErr('');
    s.toast('success', `Saved "${edit.name}"`);
  };

  return (
    <Modal title={`Indicators · ${chart?.symbol} ${chart?.timeframe}`} wide onClose={onClose}>
      <div className="seg">
        <button type="button" className={tab === 'library' ? 'on' : ''} onClick={() => setTab('library')}>Library</button>
        <button type="button" className={tab === 'chart' ? 'on' : ''} onClick={() => setTab('chart')}>On this chart ({chart?.indicators.length})</button>
        <button type="button" className={tab === 'custom' ? 'on' : ''} onClick={() => setTab('custom')}>Custom JS</button>
      </div>
      {tab === 'library' && (
        <>
          <input className="search" autoFocus placeholder="Search indicators… (no limit on how many you add)" value={q} onChange={(e) => setQ(e.target.value)} />
          {Object.entries(grouped).map(([g, defs]) => (
            <div key={g} className="ind-group">
              <h4>{g}</h4>
              {defs.map((d) => (
                <button key={d.id} type="button" className="ind-row" onClick={() => { s.addIndicator(chart.id, d); s.toast('success', `${d.name} added`); }}>
                  <b>{d.name}</b><span className="muted">{d.description || (d.overlay ? 'overlay' : 'separate pane')}</span>
                </button>
              ))}
            </div>
          ))}
        </>
      )}
      {tab === 'chart' && (
        <div>
          {chart.indicators.map((inst) => {
            const def = definitionOf(inst, s.customIndicators);
            return (
              <div key={inst.uid} className="inst">
                <div className="inst-head">
                  <label><input type="checkbox" checked={inst.visible !== false}
                    onChange={(e) => s.updateIndicator(chart.id, inst.uid, { visible: e.target.checked })} /> <b>{def?.name || inst.defId}</b></label>
                  <button type="button" className="btn-x" onClick={() => s.removeIndicator(chart.id, inst.uid)}>Remove</button>
                </div>
                <div className="inst-params">
                  {(def?.params || []).map((p) => (
                    <Field key={p.key} label={p.label}>
                      <ParamInput p={p} value={inst.params[p.key] ?? p.default}
                        onChange={(v) => s.updateIndicator(chart.id, inst.uid, { params: { ...inst.params, [p.key]: v } })} />
                    </Field>
                  ))}
                </div>
              </div>
            );
          })}
          {!chart.indicators.length && <p className="muted">No indicators yet.</p>}
          {s.settings.brokerClock && <p className="hint">Session indicators use New York time. MT5 server clock: <b>{s.settings.brokerClock}</b> (change in ⚙ settings).</p>}
        </div>
      )}
      {tab === 'custom' && (
        <div>
          <div className="chips">
            {s.customIndicators.map((c) => (
              <span key={c.id} className="chip">
                <button type="button" onClick={() => setEdit(c)}>{c.name}</button>
                <button type="button" onClick={() => s.deleteCustomIndicator(c.id)}>✕</button>
              </span>
            ))}
            <button type="button" className="chip" onClick={() => setEdit({ id: '', name: 'My indicator', code: CUSTOM_TEMPLATE })}>＋ New</button>
          </div>
          <Field label="Name"><input value={edit.name} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></Field>
          <textarea className="code" spellCheck={false} value={edit.code} onChange={(e) => setEdit({ ...edit, code: e.target.value })} />
          {codeErr && <p className="error">{codeErr}</p>}
          <div className="modal-actions">
            <button type="button" className="btn" onClick={saveCustom}>Save</button>
            <button type="button" className="btn primary" onClick={() => {
              saveCustom();
              const id = edit.id || useStore.getState().customIndicators.at(-1)?.id;
              if (id) s.addIndicator(chart.id, { id, name: edit.name });
            }}>Save & add to chart</button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// ------------------------------------------------------------------ Order ticket
function OrderModal({ onClose, order: init }) {
  const [o, setO] = useState({ kind: 'market', side: 'buy', volume: 1, price: '', sl: '', tp: '', comment: '', magic: 0, ...init });
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await placeOrder({ ...o, volume: Number(o.volume), price: Number(o.price) || 0, sl: Number(o.sl) || 0,
        tp: Number(o.tp) || 0, magic: Number(o.magic) || 0 });
      onClose();
    } catch { /* toast already shown */ }
    setBusy(false);
  };
  return (
    <Modal title={`New order · ${o.symbol}`} onClose={onClose}>
      <form onSubmit={submit}>
        <div className="seg">
          {['market', 'limit', 'stop'].map((k) => <button key={k} type="button" className={o.kind === k ? 'on' : ''} onClick={() => setO({ ...o, kind: k })}>{k}</button>)}
        </div>
        <div className="seg">
          <button type="button" className={`sell ${o.side === 'sell' ? 'on' : ''}`} onClick={() => setO({ ...o, side: 'sell' })}>SELL</button>
          <button type="button" className={`buy ${o.side === 'buy' ? 'on' : ''}`} onClick={() => setO({ ...o, side: 'buy' })}>BUY</button>
        </div>
        <Field label="Volume (lots)"><input type="number" step="0.01" min="0.01" value={o.volume} onChange={(e) => setO({ ...o, volume: e.target.value })} /></Field>
        {o.kind !== 'market' && <Field label="Entry price"><input type="number" step="any" required value={o.price} onChange={(e) => setO({ ...o, price: e.target.value })} /></Field>}
        <Field label="Stop loss (0 = none)"><input type="number" step="any" value={o.sl} onChange={(e) => setO({ ...o, sl: e.target.value })} /></Field>
        <Field label="Take profit (0 = none)"><input type="number" step="any" value={o.tp} onChange={(e) => setO({ ...o, tp: e.target.value })} /></Field>
        <Field label="Comment"><input maxLength={31} value={o.comment} onChange={(e) => setO({ ...o, comment: e.target.value })} /></Field>
        <Field label="Magic (0 = manual)"><input type="number" value={o.magic} onChange={(e) => setO({ ...o, magic: e.target.value })} /></Field>
        <div className="modal-actions">
          <button type="submit" className={`btn ${o.side}`} disabled={busy}>
            {busy ? 'Sending…' : `${o.side.toUpperCase()} ${o.kind === 'market' ? '' : `${o.kind.toUpperCase()} @ ${o.price}`} ${o.volume}`}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ Status / settings
function StatusModal({ onClose }) {
  const s = useStore();
  const ext = s.status.external || { providers: [] };
  const [busy, setBusy] = useState(false);
  const retry = async () => {
    setBusy(true);
    try { await request('external_retry', {}, 90000); } catch (e) { s.toast('error', e.message); }
    setBusy(false);
  };
  return (
    <Modal title="Connections & settings" onClose={onClose}>
      <h4>MetaTrader 5</h4>
      <p className={`status-line ${s.status.mt5?.status}`}>{s.status.mt5?.status} · {s.status.mt5?.mode}{s.status.mt5?.error ? ` · ${s.status.mt5.error}` : ''}</p>
      <h4>External data (TradingView + 4 fallbacks)</h4>
      <table className="small-table">
        <thead><tr><th>Source</th><th>Status</th><th>Last error</th></tr></thead>
        <tbody>
          {ext.providers.map((p) => (
            <tr key={p.name}><td>{p.label}</td><td className={`st-${p.status}`}>{p.status}</td><td className="muted">{p.last_error}</td></tr>
          ))}
        </tbody>
      </table>
      {ext.gave_up && <p className="error">All sources failed, so automatic retries have stopped. Retry only runs when you press the button.</p>}
      <div className="modal-actions"><button type="button" className="btn" disabled={busy} onClick={retry}>{busy ? 'Checking…' : 'Retry all sources'}</button></div>
      <h4>Settings</h4>
      <Field label="MT5 server clock (for NY-session indicators)">
        <select value={s.settings.brokerClock} onChange={(e) => s.setSettings({ brokerClock: e.target.value })}>
          <option value="ny+7">New York + 7h (most index/gold brokers)</option>
          <option value="utc+2">UTC+2 fixed</option>
          <option value="utc+3">UTC+3 fixed</option>
          <option value="utc">UTC</option>
        </select>
      </Field>
    </Modal>
  );
}

function FileModal({ onClose, file }) {
  return (
    <Modal title={file.path} wide onClose={onClose}>
      <p className="muted">{file.kind} · {file.size} bytes · modified {new Date(file.mtime * 1000).toLocaleString()}{file.truncated ? ' · truncated' : ''}</p>
      {file.binary ? <p className="hint">Compiled file (.ex5) - binary, not viewable. Open the matching .mq5 source.</p>
        : <pre className="code-view">{file.text}</pre>}
    </Modal>
  );
}


// ------------------------------------------------------------------ AI agents account
function AgentAccountModal({ onClose }) {
  const ag = useStore((s) => s.agents);
  const demo = ag?.config?.demo_mode ?? true;
  const [profile, setProfile] = useState(demo ? 'demo' : 'live');
  const [f, setF] = useState({ server: '', login: '', password: '' });
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [saved, setSaved] = useState(null);
  const toggleDemo = async (on) => {
    setErr('');
    try {
      const st = await request('agents_config', { config: { demo_mode: on, live_confirmed: !on && ack } }, 90000);
      useStore.setState({ agents: st });
      setProfile(on ? 'demo' : 'live');
    } catch (e) { setErr(e.message); }
  };
  const connect = async (e) => {
    e.preventDefault();
    setBusy(true); setErr('');
    try {
      const res = await request('agents_account', { profile, ...f, live_confirmed: profile === 'live' && ack }, 90000);
      setSaved(res.profiles);
      useStore.setState({ agents: res });
      useStore.getState().toast('success', `Agents connected to ${profile} account ${res.account.login} (${res.account.account_type})`);
      setF({ ...f, password: '' });
    } catch (ex) { setErr(ex.message); }
    setBusy(false);
  };
  return (
    <Modal title="AI agent trading account" onClose={onClose}>
      <div className="toggle-row">
        <div>
          <b>Demo Mode</b>
          <p className="muted small">Agents trade only on an MT5 demo account (simulated funds) - same code path, checks and order latency as live.</p>
        </div>
        <button type="button" role="switch" aria-checked={demo} className={`switch ${demo ? 'on' : ''}`}
          disabled={ag?.running} onClick={() => toggleDemo(!demo)}><span /></button>
      </div>
      {ag?.running && <p className="hint">Stop the agents to switch accounts or modes.</p>}
      <p className={`status-line ${ag?.risk?.account_type === (demo ? 'demo' : 'real') ? 'ok' : 'down'}`}>
        Connected: {ag?.risk?.login ? `${ag.risk.login} @ ${ag.risk.server} (${ag.risk.account_type})` : 'none'}
      </p>
      <div className="seg">
        <button type="button" className={profile === 'demo' ? 'on' : ''} onClick={() => setProfile('demo')}>Demo account</button>
        <button type="button" className={profile === 'live' ? 'on' : ''} onClick={() => setProfile('live')}>Live / FTMO account</button>
      </div>
      <form onSubmit={connect}>
        <Field label="Broker server"><input required value={f.server} placeholder={profile === 'demo' ? 'e.g. FTMO-Demo' : 'e.g. FTMO-Server'} onChange={(e) => setF({ ...f, server: e.target.value })} /></Field>
        <Field label="Login"><input required inputMode="numeric" value={f.login} onChange={(e) => setF({ ...f, login: e.target.value })} /></Field>
        <Field label="Password"><input required type="password" autoComplete="current-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>
        {profile === 'live' && (
          <label className="check danger-box"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            {' '}I understand the agents will place real orders on this account. The FTMO guardrails limit losses but cannot guarantee them (gaps, slippage, outages).</label>
        )}
        <p className="hint">Credentials go only to your local bridge for <code>mt5.login()</code> and are kept in memory - never written to disk or the browser.</p>
        {saved && <p className="muted small">Saved this session: {Object.entries(saved).map(([k, v]) => `${k} ${v.login}@${v.server}`).join(' · ')}</p>}
        {err && <p className="error">{err}</p>}
        <div className="modal-actions">
          <button type="submit" className="btn primary" disabled={busy || ag?.running || (profile === 'live' && !ack)}>
            {busy ? 'Connecting…' : `Connect ${profile} account`}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export default function Modals() {
  const modal = useStore((s) => s.modal);
  const close = useStore((s) => s.closeModal);
  if (!modal) return null;
  const C = { mt5: Mt5Modal, tv: TvModal, indicators: IndicatorModal, order: OrderModal, status: StatusModal, file: FileModal, agentAccount: AgentAccountModal }[modal.type];
  return C ? <C {...modal} onClose={close} /> : null;
}

