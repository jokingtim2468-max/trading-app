import { useEffect } from 'react';
import { connect, on } from './lib/ws.js';
import { useStore } from './lib/store.js';
import TopToolbar from './components/TopToolbar.jsx';
import ChartGrid from './components/ChartGrid.jsx';
import MinimizedDock from './components/MinimizedDock.jsx';
import PositionsPanel from './components/PositionsPanel.jsx';
import FileExplorer from './components/FileExplorer.jsx';
import Sidebar from './components/Sidebar.jsx';
import AgentsPanel from './components/AgentsPanel.jsx';
import Modals from './components/Modals.jsx';
import ContextMenu from './components/ContextMenu.jsx';
import Toasts from './components/Toasts.jsx';

export default function App() {
  const sidePanel = useStore((s) => s.settings.sidePanel);
  const showPanel = useStore((s) => s.settings.showPanel);

  useEffect(() => {
    const st = useStore.getState;
    const offs = [
      on('open', () => useStore.setState((s) => ({ wsConnected: true, wsEpoch: s.wsEpoch + 1 }))),
      on('close', () => {
        if (st().wsConnected) st().toast('error', 'Lost connection to the bridge - reconnecting…', 'ws');
        useStore.setState({ wsConnected: false });
      }),
      on('status', (msg) => useStore.setState({ status: msg })),
      on('positions', (msg) => useStore.setState({ positions: msg.positions, orders: msg.orders, account: msg.account })),
      on('notify', (msg) => st().toast(msg.level, msg.message, msg.key)),
      on('trade_event', (msg) => { st().logTrade(msg.message); st().toast('trade', msg.message); }),
      on('agents', (msg) => useStore.setState({ agents: msg })),
      on('agent_event', (msg) => useStore.setState((s) => (s.agents ? {
        agents: { ...s.agents, activity: [...(s.agents.activity || []), msg].slice(-60) } } : {}))),
    ];
    connect();
    return () => offs.forEach((off) => off());
  }, []);

  return (
    <div className="app">
      <TopToolbar />
      <div className="workspace">
        <Sidebar />
        <div className="charts-col">
          <ChartGrid />
          <MinimizedDock />
        </div>
        {sidePanel === 'explorer' && <FileExplorer />}
        {sidePanel === 'agents' && <AgentsPanel />}
      </div>
      {showPanel && <PositionsPanel />}
      <Modals />
      <ContextMenu />
      <Toasts />
    </div>
  );
}
