export function fmtW(w: number): string {
  const a = Math.abs(w);
  if (a >= 1000) return (w / 1000).toFixed(1) + ' kW';
  return Math.round(w) + ' W';
}

export function fmtWabs(w: number): string {
  return fmtW(Math.abs(w));
}

export function fmtNet(v: number): string {
  return v < 0 ? '\u2212$' + (-v).toFixed(2) : '$' + v.toFixed(2);
}

export function fmtKwh(v: number): string {
  return v < 0 ? '\u2212' + (-v).toFixed(1) : v.toFixed(1);
}

const MODE_LABELS: Record<string, string> = {
  self_consumption: 'Self-Powered',
  autonomous: 'Time-Based Control',
  backup: 'Backup',
};

export function modeLabel(m: string | null | undefined): string | null {
  if (!m) return null;
  return MODE_LABELS[m] || m.replace(/_/g, ' ');
}

export interface Badge {
  text: string;
  cls: string;
}

export function settingsBadges(e: {
  mode?: string | null;
  reserve?: number | null;
  grid_charging?: boolean | null;
  grid_export?: string | null;
}): Badge[] {
  const badges: Badge[] = [];
  if (e.mode) badges.push({ text: modeLabel(e.mode)!, cls: 'amber' });
  if (e.reserve != null) badges.push({ text: `reserve ${e.reserve}%`, cls: '' });
  if (e.grid_charging != null)
    badges.push({ text: `grid charge ${e.grid_charging ? 'ON' : 'OFF'}`, cls: e.grid_charging ? 'green' : '' });
  if (e.grid_export)
    badges.push({ text: e.grid_export === 'battery_ok' ? 'export solar+batt' : 'export solar only', cls: 'blue' });
  return badges;
}

export const DAYS_LBL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
export const MONTHS_LBL = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function fmtFireTime(iso: string): string {
  const d = new Date(iso);
  const day = DAYS_LBL[d.getDay()].slice(0, 3);
  const h = d.getHours(),
    m = d.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12 = h % 12 || 12;
  return `${day} ${h12}:${String(m).padStart(2, '0')} ${ampm}`;
}
