import { useEffect, useState } from 'react';
import { request } from '../lib/ws.js';
import { useStore } from '../lib/store.js';
import { fmtMoney } from '../lib/format.js';

const KIND_CLASS = { execute: 'up', reject: 'warn', flatten: 'down', lock: 'down', halt: 'down', error: 'down',
  proposal: 'accent', analyst: 'muted', system: 'muted' };

function Meter({ label, value, max, danger, display, hint, invert }) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const level = pct >= 90 ? 'down' : pct >= 70 ? 'warn' : invert ? 'accent' : 'up';
  return (
    <div className="meter" title={hint}>
      <div className="meter-head"><span>{label}</span><b className={danger ? 'down' : ''}>{display}</b></div>
      <div className="meter-bar"><div className={`meter-fill ${level}`} style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

function useCountdown(ts) {
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 1000); return () => clearInterval(t); }, []);
  if (!ts) return '—';
  const s = Math.max(0, Math.round(ts - Date.now() / 1000));
  return s > 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export default function AgentsPanel() {
  const ag = useStore((s) => s.agents);
  const { openModal, setSettings, toast } = useStore.getState();
  const [busy, setBusy] = useState('');
  const nextIn = useCountdown(ag?.next_analysis_at);

  useEffect(() => { request('agents_status').then((st) => useStore.setState({ agents: st })).catch(() => {}); }, []);

  const call = async (type, payload = {}, label = type) => {
    setBusy(label);
    try {
      const st = await request(type, payload, 180000);
      if (st && st.type === 'agents') useStore.setState({ agents: st });
      return st;
    } catch (e) {
      toast('error', e.message);
      return null;
    } finally { setBusy(''); }
  };
  const setCfg = (patch) => call('agents_config', { config: patch }, 'config');

  if (!ag) return <aside className="agents"><div className="agents-head"><b>AI Trading Agents</b></div><p className="muted pad">Connecting…</p></aside>;
  const r = ag.risk || {};
  const rules = r.rules || {};
  const cfg = ag.config;
  const dailyLoss = Math.max(0, -(r.daily_pnl || 0));
  const floorDist = r.equity_floor_distance ?? rules.max_overall_loss;
  const flattenAt = r.next_weekend_flatten ? new Date(r.next_weekend_flatten) : null;
  const banner = r.halted ? { cls: 'down', text: `HALTED - ${r.halt_reason}` }
    : r.daily_locked ? { cls: 'down', text: `Daily loss lock until ${new Date(r.next_daily_reset).toLocaleString()}` }
      : r.target_reached ? { cls: 'up', text: `Phase ${r.phase} target reached${r.trading_days_remaining ? ` - ${r.trading_days_remaining} trading day(s) still needed` : ''}` }
        : r.entries_blocked_weekend ? { cls: 'warn', text: 'Weekend window - no new entries, positions flattened (Standard account)' } : null;

  return (
    <aside className="agents" aria-label="AI Trading Agents">
      <div className="agents-head">
        <b>AI Trading Agents</b>
        <span className={`mode-badge ${cfg.demo_mode ? 'demo' : 'live'}`}>{cfg.demo_mode ? 'DEMO' : 'LIVE'}</span>
        <span className="spacer" />
        <button type="button" className="btn-sm" title="Agent account & Demo Mode" onClick={() => openModal('agentAccount')}>👤 Account</button>
        <button type="button" className="btn-sm" onClick={() => setSettings({ sidePanel: null })}>✕</button>
      </div>
      <div className="agents-body">
        <div className="agents-controls">
          {ag.running
            ? <button type="button" className="btn big sell" disabled={!!busy} onClick={() => call('agents_stop')}>■ Stop agents</button>
            : <button type="button" className="btn big buy" disabled={!!busy || r.halted} onClick={() => call('agents_start')}>▶ Start agents</button>}
          <button type="button" className="btn" disabled={!!busy} onClick={() => call('agents_run_now', {}, 'run')}>
            {busy === 'run' ? 'Analysing…' : 'Analyse now'}
          </button>
        </div>
        <p className="muted small">
          Agent 1: <b>{ag.analyst === 'claude' ? 'Claude market analyst' : 'rule-based analyst'}</b> · Agent 2: FTMO risk manager (magic {ag.magic})
          {ag.running && <> · next cycle in {nextIn}</>}
          {r.login && <> · acct {r.login} ({r.account_type})</>}
        </p>
        {banner && <div className={`banner ${banner.cls}`}>{banner.text}</div>}

        <h4>FTMO 10k Standard · Phase {r.phase}</h4>
        <Meter label="Daily loss (trigger -$480 / limit -$500)" value={dailyLoss} max={rules.max_daily_loss || 500}
          display={`${fmtMoney(r.daily_pnl)} / headroom ${fmtMoney(r.daily_headroom)}`} danger={dailyLoss >= 400}
          hint={`Day start balance ${fmtMoney(r.day_start_balance)} (midnight CE(S)T). Floating P/L counts.`} />
        <Meter label="Max loss (equity floor $9,000)" value={(rules.max_overall_loss || 1000) - floorDist} max={rules.max_overall_loss || 1000}
          display={`equity ${fmtMoney(r.equity)} · ${fmtMoney(floorDist)} above floor`} danger={floorDist < 200} />
        <Meter label={`Profit target +$${rules.phase_target}`} value={Math.max(0, (r.target_progress || 0) * 100)} max={100} invert
          display={`${fmtMoney(r.overall_pnl)} (${((r.target_progress || 0) * 100).toFixed(1)}%)`} />
        <div className="days">
          <span>Trading days</span>
          {[0, 1, 2, 3].map((i) => <span key={i} className={`day-dot ${i < (r.trading_days || []).length ? 'on' : ''}`} />)}
          <b>{(r.trading_days || []).length}/{rules.min_trading_days}</b>
        </div>
        <p className="muted small">
          Open risk to SL {fmtMoney(r.open_risk)} · leverage 1:{rules.leverage} · weekend flatten {flattenAt ? flattenAt.toLocaleString() : '—'}
        </p>

        <h4>Settings</h4>
        <div className="agent-cfg">
          <label className="field"><span>Analyst (Agent 1)</span>
            <select value={cfg.analyst} onChange={(e) => setCfg({ analyst: e.target.value })}>
              <option value="auto">Auto (Claude if API key set)</option><option value="claude">Claude</option><option value="rules">Rule-based</option>
            </select></label>
          <label className="field"><span>Risk per trade (max $100)</span>
            <input type="number" min="5" max="100" step="5" value={cfg.risk_per_trade}
              onChange={(e) => setCfg({ risk_per_trade: Number(e.target.value) })} /></label>
          <label className="field"><span>Analysis interval (s)</span>
            <input type="number" min="60" step="60" value={cfg.interval_s} onChange={(e) => setCfg({ interval_s: Number(e.target.value) })} /></label>
          <label className="field"><span>Symbols</span>
            <input value={cfg.symbols.join(', ')} onChange={(e) => setCfg({ symbols: e.target.value.split(',').map((x) => x.trim()).filter(Boolean) })} /></label>
        </div>
        <label className="check"><input type="checkbox" checked={cfg.guardian_always_on}
          onChange={(e) => setCfg({ guardian_always_on: e.target.checked })} /> Guardian always on (enforce FTMO limits on manual & EA trades even when agents are stopped)</label>
        <div className="modal-actions left">
          <button type="button" className="btn" disabled={ag.running} onClick={() => {
            const phase = window.prompt('Start a new challenge phase (1 or 2). This resets daily/overall tracking and trading days.', String(r.phase || 1));
            if (!phase) return;
            const confirm = window.prompt('Type RESET to confirm');
            call('agents_new_challenge', { phase: Number(phase), confirm });
          }}>New challenge phase…</button>
        </div>

        <h4>Proposals (Agent 1 → Agent 2)</h4>
        <div className="proposals">
          {(ag.proposals || []).slice().reverse().map((p, i) => (
            <div key={i} className={`proposal ${p.decision}`}>
              <div><b className={p.direction === 'buy' ? 'up' : 'down'}>{p.direction.toUpperCase()}</b> {p.symbol} @ {p.entry} · SL {p.stop_loss} · TP {p.take_profit}
                <span className={`tag ${p.decision === 'executed' ? 'manual' : 'ea'}`}>{p.decision}</span></div>
              <div className="muted small">{p.decision === 'executed' ? `${p.order?.volume} lots · risk $${p.order?.risk_usd}` : p.reason}</div>
              <div className="muted small">{p.rationale}</div>
            </div>
          ))}
          {!(ag.proposals || []).length && <p className="muted small">No proposals yet.</p>}
        </div>

        <h4>Activity</h4>
        <ul className="log agent-log">
          {(ag.activity || []).slice().reverse().map((e, i) => (
            <li key={i}><span className="muted">{new Date(e.time * 1000).toLocaleTimeString()}</span>{' '}
              <span className={KIND_CLASS[e.kind] || ''}>[{e.kind}]</span> {e.message}</li>
          ))}
        </ul>
      </div>
    </aside>
  );
}
