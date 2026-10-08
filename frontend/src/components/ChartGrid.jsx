import { Fragment, useRef } from 'react';
import { ROWS_FOR, useStore } from '../lib/store.js';
import ChartPane from './ChartPane.jsx';

const MIN = 0.12;

/** Drag handle between two neighbours. Moves `delta` of the parent size from one to the other. */
function Splitter({ dir, onDrag }) {
  const start = (e) => {
    e.preventDefault();
    const parent = e.currentTarget.parentElement.getBoundingClientRect();
    const size = dir === 'col' ? parent.width : parent.height;
    let last = dir === 'col' ? e.clientX : e.clientY;
    const move = (ev) => {
      const pos = dir === 'col' ? ev.clientX : ev.clientY;
      onDrag((pos - last) / size);
      last = pos;
    };
    const up = () => {
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      document.body.classList.remove('resizing');
    };
    document.body.classList.add('resizing');
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  };
  return <div className={`splitter ${dir}`} onMouseDown={start} onDoubleClick={() => onDrag(null)} role="separator" />;
}

function shift(arr, i, delta) {
  if (delta === null) return arr.map(() => 1 / arr.length); // double-click resets
  const a = arr[i] + delta; const b = arr[i + 1] - delta;
  if (a < MIN || b < MIN) return arr;
  const next = [...arr]; next[i] = a; next[i + 1] = b;
  return next;
}

export default function ChartGrid() {
  const charts = useStore((s) => s.charts);
  const activeId = useStore((s) => s.activeId);
  useStore((s) => s.fracs);
  const { layoutFracs, setFracs } = useStore.getState();
  const gridRef = useRef(null);

  const visible = charts.filter((c) => !c.minimized);
  const n = visible.length;
  const rows = ROWS_FOR[n] || [n];
  const fr = layoutFracs(n);
  const activeVisible = visible.find((c) => c.id === activeId)?.id || visible[0]?.id;

  let k = 0;
  return (
    <div className="grid" ref={gridRef}>
      {rows.map((cols, r) => {
        const rowCharts = visible.slice(k, k + cols);
        k += cols;
        return (
          <Fragment key={`r${r}`}>
            {r > 0 && <Splitter dir="row" onDrag={(d) => setFracs(n, { ...fr, rows: shift(fr.rows, r - 1, d) })} />}
            <div className="grid-row" style={{ flex: fr.rows[r] }}>
              {rowCharts.map((c, i) => (
                <Fragment key={c.id}>
                  {i > 0 && <Splitter dir="col" onDrag={(d) => setFracs(n, {
                    ...fr, cols: fr.cols.map((cf, ri) => (ri === r ? shift(cf, i - 1, d) : cf)) })} />}
                  <div className="grid-cell" style={{ flex: fr.cols[r][i] }}>
                    <ChartPane chart={c} active={c.id === activeVisible} canMinimize={n > 1} />
                  </div>
                </Fragment>
              ))}
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
