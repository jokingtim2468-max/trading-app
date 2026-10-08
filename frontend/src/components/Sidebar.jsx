import { useStore } from '../lib/store.js';

const TABS = [
  { id: null, icon: '📈', label: 'Charts' },
  { id: 'agents', icon: '🤖', label: 'AI Trading Agents' },
  { id: 'explorer', icon: '📁', label: 'EAs & indicators' },
];

export default function Sidebar() {
  const side = useStore((s) => s.settings.sidePanel);
  const running = useStore((s) => s.agents?.running);
  const halted = useStore((s) => s.agents?.risk?.halted || s.agents?.risk?.daily_locked);
  const setSettings = useStore((s) => s.setSettings);
  return (
    <nav className="sidebar" aria-label="Main">
      {TABS.map((t) => (
        <button key={t.label} type="button" title={t.label} aria-label={t.label}
          className={`side-btn ${side === t.id ? 'on' : ''}`}
          onClick={() => setSettings({ sidePanel: t.id === side ? null : t.id })}>
          <span className="side-icon">{t.icon}</span>
          <span className="side-label">{t.id === 'agents' ? 'AI Agents' : t.label.split(' ')[0]}</span>
          {t.id === 'agents' && (running || halted) && <span className={`side-dot ${halted ? 'down' : 'ok'}`} />}
        </button>
      ))}
    </nav>
  );
}
