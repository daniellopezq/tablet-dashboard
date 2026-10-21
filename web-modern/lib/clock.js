// Server-corrected clock in the dashboard's timezone (same idea as syncTime/serverNow in web/app.js).
import { getJSON } from './api.js';

const SYNC_MS = 5 * 60 * 1000;
const STORAGE_24H = 'dashboard.use24h';  // same key as the classic view

let skewMs = 0;
let timeZone = undefined;
let synced = false;
let use24h = true;
const listeners = new Set();

function storageGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function storageSet(key, v) { try { localStorage.setItem(key, v); } catch { /* private mode */ } }

export function initClock(config) {
  timeZone = config?.timezone;
  try { new Intl.DateTimeFormat('en', { timeZone }); } catch { timeZone = undefined; }
  const stored = storageGet(STORAGE_24H);
  use24h = stored === null ? config?.clock_default_24h !== false : stored === '1';
  sync();
  setInterval(sync, SYNC_MS);
  // Tick on the second boundary.
  const tick = () => {
    for (const fn of listeners) fn(now());
    setTimeout(tick, 1000 - (Date.now() % 1000) + 5);
  };
  tick();
}

async function sync() {
  const t0 = Date.now();
  try {
    const data = await getJSON('/api/time', { timeout: 8000 });
    const t1 = Date.now();
    skewMs = data.now_ms + (t1 - t0) / 2 - t1;
    synced = true;
  } catch { /* keep the last offset */ }
}

export const now = () => Date.now() + skewMs;
export const isSynced = () => synced;
export const is24h = () => use24h;

export function toggle24h() {
  use24h = !use24h;
  storageSet(STORAGE_24H, use24h ? '1' : '0');
  for (const fn of listeners) fn(now());
}

export function onTick(fn) {
  listeners.add(fn);
  fn(now());
  return () => listeners.delete(fn);
}

function fmt(ms, opts) {
  return new Intl.DateTimeFormat('en-US', { timeZone, ...opts }).format(new Date(ms));
}

/** {hm, ampm, sec} parts for the big clock. */
export function clockParts(ms) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone, hour: 'numeric', minute: '2-digit', second: '2-digit', hourCycle: use24h ? 'h23' : 'h12',
  }).formatToParts(new Date(ms));
  const get = (t) => parts.find((p) => p.type === t)?.value ?? '';
  const hour = use24h ? get('hour').padStart(2, '0') : get('hour');
  return { hm: `${hour}:${get('minute')}`, ampm: use24h ? '' : get('dayPeriod'), sec: get('second') };
}

export const formatHM = (ms) => {
  const p = clockParts(ms);
  return p.ampm ? `${p.hm} ${p.ampm}` : p.hm;
};
export const formatDate = (ms) => fmt(ms, { weekday: 'long', month: 'long', day: 'numeric' });
export const isoDay = (ms) => fmt(ms, { year: 'numeric', month: '2-digit', day: '2-digit' })
  .replace(/(\d+)\/(\d+)\/(\d+)/, '$3-$1-$2');

/** "Tue" / "Today" / "Tomorrow" for a YYYY-MM-DD date. */
export function dayLabel(iso) {
  const today = isoDay(now());
  const tomorrow = isoDay(now() + 86400000);
  if (iso === today) return 'Today';
  if (iso === tomorrow) return 'Tomorrow';
  const [y, m, d] = iso.split('-').map(Number);
  return new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: 'UTC' }).format(Date.UTC(y, m - 1, d));
}

/** "5 min ago" style age. */
export function ago(ms) {
  if (!ms) return '';
  const s = Math.max(0, Math.round((now() - ms) / 1000));
  if (s < 60) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

/** "14:30" (server wall-clock time) -> "14:30" or "2:30 PM" per the 12/24 h setting. */
export function formatWallHM(hm) {
  const m = /^(\d{2}):(\d{2})$/.exec(hm ?? '');
  if (!m) return '';
  if (use24h) return hm;
  const hour = +m[1];
  return `${hour % 12 || 12}:${m[2]} ${hour < 12 ? 'AM' : 'PM'}`;
}

/** "Today · Tue 20 Oct" style label for a YYYY-MM-DD date. */
export function longDayLabel(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const base = new Intl.DateTimeFormat('en-US', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })
    .format(Date.UTC(y, m - 1, d));
  const rel = dayLabel(iso);
  return rel === 'Today' || rel === 'Tomorrow' ? `${rel} · ${base}` : base;
}
