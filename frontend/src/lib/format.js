export const fmtPrice = (v, digits = 2) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(digits));
export const fmtMoney = (v, ccy = '') => (v == null ? '—'
  : `${v < 0 ? '-' : ''}${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${ccy ? ` ${ccy}` : ''}`);
export const roundTo = (v, digits) => Number(Number(v).toFixed(digits));
