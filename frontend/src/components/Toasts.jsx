import { useEffect } from 'react';
import { useStore } from '../lib/store.js';

const TTL = { success: 4000, info: 5000, trade: 6000, warn: 9000, error: 0 }; // errors stay until dismissed

export default function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const dismiss = useStore((s) => s.dismissToast);
  useEffect(() => {
    const timers = toasts.filter((t) => TTL[t.level]).map((t) => setTimeout(() => dismiss(t.id), TTL[t.level] - (Date.now() - t.time)));
    return () => timers.forEach(clearTimeout);
  }, [toasts]);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.level}`}>
          <span>{t.message}</span>
          <button type="button" onClick={() => dismiss(t.id)} aria-label="Dismiss">✕</button>
        </div>
      ))}
    </div>
  );
}
