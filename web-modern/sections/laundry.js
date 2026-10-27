// Laundry check (laundry.py): rain coming -> the V380 looks at the clotheslines. Its status rides on the
// cameras feed (/api/cameras -> laundry), so this adds no polling of its own.
import { h, replace, toast } from '../lib/dom.js';
import { subscribe, patch, refreshSoon } from '../lib/store.js';
import { postJSON } from '../lib/api.js';
import { ago, formatHM } from '../lib/clock.js';

const RESULT = {
  ok: null,
  skipped: 'Skipped',
  interrupted: 'Stopped',
  error: 'Failed',
};
const DISMISSED_KEY = 'dashboard.laundryDismissed';

function dismissed() { try { return localStorage.getItem(DISMISSED_KEY) || ''; } catch { return ''; } }
function setDismissed(key) { try { localStorage.setItem(DISMISSED_KEY, key); } catch { /* private mode */ } }

async function checkNow() {
  try {
    await postJSON('/api/laundry/check', {}, { timeout: 10000 });
    toast('Checking the clotheslines; it takes about 6 minutes');
    patch('cameras', (d) => { if (d.laundry) d.laundry.running = true; });
    refreshSoon('cameras', 5000);
  } catch (err) {
    toast(err.status ? err.message : 'Could not reach the server');
  }
}

/** One row per clothesline: the last result if there is one, else just the name. */
function lineRows(l) {
  const seen = new Map((l.last?.lines ?? []).map((x) => [x.id, x]));
  return h('ul', { class: 'ln-lines' }, (l.lines ?? []).map((line) => {
    const r = seen.get(line.id);
    const word = !r ? 'Not checked' : r.laundry ? 'Laundry out' : 'Empty';
    return h('li', { class: `ln-line ${r?.laundry ? 'out' : r ? 'empty' : ''}` },
      h('span', { class: 'dot' }), h('span', { class: 'ln-name' }, line.label), h('span', { class: 'ln-word' }, word));
  }));
}

function lastLine(last) {
  if (!last) return h('p', { class: 'meta' }, 'No check since the dashboard started.');
  const bad = RESULT[last.result];
  return h('p', { class: `meta ${bad ? 'warn' : ''}` },
    `${bad ? `${bad}: ${last.detail || last.result} · ` : ''}Last check ${formatHM(last.at_ms)} (${ago(last.at_ms)}) · ${last.why}`);
}

/** Panel for the Cameras view. Returns { el, stop }. */
export function laundryPanel() {
  const btn = h('button', { type: 'button', class: 'btn btn-small', onclick: checkNow }, 'Check now');
  const body = h('div', { class: 'ln-body' });
  const el = h('section', { class: 'ln-panel', hidden: true },
    h('div', { class: 'ln-head' }, h('h2', null, 'Clotheslines'), btn), body);
  const stop = subscribe('cameras', (d) => {
    const l = d?.laundry;
    el.hidden = !l?.lines?.length;
    if (el.hidden) return;
    btn.disabled = l.running || !l.enabled;
    btn.textContent = l.running ? 'Checking…' : 'Check now';
    replace(body,
      !l.enabled && h('p', { class: 'meta' }, 'The automatic check is off (laundry.enabled in config.json).'),
      l.alert && h('div', { class: 'msg warn' }, h('strong', null, l.alert.text)),
      lineRows(l),
      lastLine(l.last));
  });
  return { el, stop };
}

/** Banner on every screen when a check finds laundry out, until dismissed (remembered on this device). */
export function watchLaundry(bannerEl) {
  return subscribe('cameras', (d) => {
    const a = d?.laundry?.alert;
    if (!a || a.key === dismissed()) { bannerEl.hidden = true; return; }
    const close = () => { setDismissed(a.key); bannerEl.hidden = true; };
    replace(bannerEl,
      h('span', null, a.text),
      h('a', { class: 'btn btn-small', href: '#cameras', onclick: close }, 'View'),
      h('button', { type: 'button', class: 'btn-plain', 'aria-label': 'Dismiss', onclick: close }, '✕'));
    bannerEl.hidden = false;
  });
}
