import { useEffect, useState } from 'react';
import { request } from '../lib/ws.js';
import { useStore } from '../lib/store.js';
import { BUILTINS } from '../indicators/builtins.js';

const ICON = { source: '📄', header: '📎', compiled: '⚙', preset: '🎛', file: '·' };

function Tree({ node, depth, onOpen }) {
  const [open, setOpen] = useState(depth < 1);
  if (node.type === 'file') {
    return (
      <li>
        <button type="button" className="tree-file" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => onOpen(node)}
          title={`${node.size} bytes · ${new Date(node.mtime * 1000).toLocaleString()}`}>
          {ICON[node.kind] || '·'} {node.name}
        </button>
      </li>
    );
  }
  return (
    <li>
      <button type="button" className="tree-dir" style={{ paddingLeft: 8 + depth * 12 }} onClick={() => setOpen(!open)}>
        {open ? '▾' : '▸'} {node.name} <span className="muted">({node.children.length})</span>
      </button>
      {open && <ul>{node.children.map((c) => <Tree key={c.path} node={c} depth={depth + 1} onOpen={onOpen} />)}</ul>}
    </li>
  );
}

export default function FileExplorer() {
  const [tree, setTree] = useState(null);
  const [error, setError] = useState('');
  const wsEpoch = useStore((s) => s.wsEpoch);
  const mt5Connected = useStore((s) => s.status.mt5?.connected);
  const customs = useStore((s) => s.customIndicators);
  const { openModal, addIndicator, activeChart, setSettings, toast } = useStore.getState();

  const load = () => {
    setError('');
    request('files_list').then(setTree).catch((e) => setError(e.message));
  };
  useEffect(load, [wsEpoch, mt5Connected]);

  const openFile = (node) => request('file_read', { path: node.path })
    .then((f) => openModal('file', { file: f }))
    .catch((e) => toast('error', e.message));
  const add = (def) => { addIndicator(activeChart().id, def); toast('success', `${def.name} added to ${activeChart().symbol}`); };

  const groups = BUILTINS.reduce((acc, d) => ({ ...acc, [d.group]: [...(acc[d.group] || []), d] }), {});

  return (
    <aside className="explorer">
      <div className="explorer-head">
        <b>Explorer</b>
        <span>
          <button type="button" className="btn-sm" onClick={load} title="Refresh">⟳</button>
          <button type="button" className="btn-sm" onClick={() => setSettings({ sidePanel: null })}>✕</button>
        </span>
      </div>
      <div className="explorer-body">
        <h4>MT5 · MQL5 (read-only)</h4>
        {error && <p className="error small">{error}</p>}
        {tree && <p className="muted small" title={tree.root}>{tree.root}</p>}
        <ul className="tree">{tree?.sections.map((s) => <Tree key={s.path} node={s} depth={0} onOpen={openFile} />)}</ul>

        <h4>App indicators <span className="muted">(click = add to active chart)</span></h4>
        <ul className="tree">
          {Object.entries(groups).map(([g, defs]) => (
            <Tree key={g} depth={0} onOpen={(node) => add(node.def)} node={{ type: 'dir', name: g, path: g,
              children: defs.map((d) => ({ type: 'file', name: d.name, path: d.id, kind: 'source', size: 0, mtime: 0, def: d })) }} />
          ))}
        </ul>
        <IndicatorClicks groups={groups} onAdd={add} />

        <h4>Custom JS indicators</h4>
        <ul className="tree">
          {customs.map((c) => (
            <li key={c.id}>
              <button type="button" className="tree-file" onClick={() => add({ id: c.id, name: c.name })}>📄 {c.name}</button>
            </li>
          ))}
          <li><button type="button" className="tree-file muted" onClick={() => openModal('indicators', { tab: 'custom' })}>＋ New custom indicator…</button></li>
        </ul>
      </div>
    </aside>
  );
}

/** Flat, clickable list under the tree so built-ins are one click away. */
function IndicatorClicks({ groups, onAdd }) {
  return (
    <div className="ind-quick">
      {groups['High/Low Projection']?.map((d) => (
        <button key={d.id} type="button" className="chip" title={d.description} onClick={() => onAdd(d)}>＋ {d.name}</button>
      ))}
    </div>
  );
}
