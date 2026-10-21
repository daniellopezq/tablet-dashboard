// Shared polled feeds. Several views can watch the same endpoint; polling runs only
// while someone is subscribed and pauses while the page is hidden.
import { getJSON } from './api.js';

const feeds = new Map();

/** intervalMs may be a function of the latest data, e.g. faster polling while something is switching. */
export function defineFeed(key, url, intervalMs, { timeout = 10000 } = {}) {
  feeds.set(key, { key, url, intervalMs, timeout, data: null, error: null, subs: new Set(), timer: null, busy: false });
}

function emit(f) {
  for (const fn of f.subs) {
    try { fn(f.data, f.error); } catch (err) { console.error(err); }
  }
}

async function poll(f) {
  clearTimeout(f.timer);
  f.timer = null;
  if (!f.subs.size || document.hidden || f.busy) return;
  f.busy = true;
  try {
    f.data = await getJSON(f.url, { timeout: f.timeout });
    f.error = null;
  } catch (err) {
    f.error = err;
  }
  f.busy = false;
  emit(f);
  const next = typeof f.intervalMs === 'function' ? f.intervalMs(f.data) : f.intervalMs;
  if (f.subs.size && !document.hidden) f.timer = setTimeout(() => poll(f), next);
}

export function subscribe(key, fn) {
  const f = feeds.get(key);
  if (!f) throw new Error(`unknown feed ${key}`);
  f.subs.add(fn);
  if (f.data || f.error) fn(f.data, f.error);
  if (!f.timer && !f.busy) poll(f);
  return () => {
    f.subs.delete(fn);
    if (!f.subs.size) { clearTimeout(f.timer); f.timer = null; }
  };
}

/** Poll now (e.g. after a user action), keeping the schedule afterwards. */
export function refresh(key) {
  const f = feeds.get(key);
  if (f) poll(f);
}

/** Poll again after delayMs (e.g. shortly after a button press). */
export function refreshSoon(key, delayMs) {
  const f = feeds.get(key);
  if (!f || !f.subs.size) return;
  clearTimeout(f.timer);
  f.timer = setTimeout(() => poll(f), delayMs);
}

/** Change the cached data in place (optimistic update) and tell every watcher. */
export function patch(key, fn) {
  const f = feeds.get(key);
  if (!f?.data) return;
  fn(f.data);
  emit(f);
}

export function current(key) {
  return feeds.get(key)?.data ?? null;
}

document.addEventListener('visibilitychange', () => {
  for (const f of feeds.values()) {
    if (document.hidden) { clearTimeout(f.timer); f.timer = null; } else if (f.subs.size) poll(f);
  }
});
