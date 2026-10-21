import { h, icon, replace, card, message, toast, confirmDialog } from '../lib/dom.js';
import { subscribe, patch, refreshSoon } from '../lib/store.js';
import { postJSON } from '../lib/api.js';

const ICONS = new Set(['media', 'fan', 'plug', 'light', 'home', 'shield', 'motion']);
const MEDIA = { plex: 'Plex', jellyfin: 'Jellyfin' };
const busy = new Set();  // button ids waiting for the server

/** Feed interval: fast while the media server is switching (same as the classic view). */
export function homeInterval(data) {
  return mediaOf(data)?.switching ? 2000 : 10000;
}

function mediaOf(data) {
  return data?.buttons?.find((b) => b.kind === 'media_switch')?.media ?? null;
}

/** -> { name, word, hint, cls } for one button (wording matches the tablet). */
function describe(b) {
  if (b.kind === 'media_switch') {
    const m = b.media ?? {};
    if (m.switching) return { name: MEDIA[m.switching] ?? b.label, word: 'Wait', cls: 'busy', hint: `Starting ${MEDIA[m.switching] ?? ''}…` };
    let d;
    if (MEDIA[m.current]) {
      d = { name: MEDIA[m.current], word: 'On', cls: 'on', hint: `Tap to switch to ${m.current === 'plex' ? 'Jellyfin' : 'Plex'}` };
    } else if (m.current === 'both') {
      d = { name: 'Plex + Jellyfin', word: 'Both', cls: 'warn', hint: 'Tap to keep Plex only' };
    } else {
      d = { name: b.label, word: 'Off', cls: 'off', hint: 'Tap to start Plex' };
    }
    if (m.error) { d.hint = m.error; d.cls += ' err'; }
    return d;
  }
  const st = b.state;
  if (st === undefined) return { name: b.label, word: 'Run', cls: 'off', hint: 'Tap to run' };
  if (st === null) return { name: b.label, word: '?', cls: 'unk', hint: 'Home Assistant not connected' };
  if (st === 'on') return { name: b.label, word: 'On', cls: 'on', hint: 'Tap to turn off' };
  if (st.startsWith('armed_')) return { name: b.label, word: 'Armed', cls: 'on', hint: 'Tap to disarm' };
  if (st === 'disarmed') return { name: b.label, word: 'Off', cls: 'off', hint: 'Disarmed · tap to arm' };
  if (st === 'arming' || st === 'pending') return { name: b.label, word: 'Arming', cls: 'busy', hint: 'Please wait' };
  if (st === 'triggered') return { name: b.label, word: 'Alarm', cls: 'warn err', hint: 'Tap to disarm' };
  if (st === 'off') return { name: b.label, word: 'Off', cls: 'off', hint: 'Tap to turn on' };
  if (['unavailable', 'missing', 'unknown'].includes(st)) {
    return { name: b.label, word: 'N/A', cls: 'unk', hint: st === 'missing' ? 'Not found in Home Assistant' : 'Device not responding' };
  }
  return { name: b.label, word: String(st), cls: 'off', hint: '' };
}

function question(b) {
  if (b.kind === 'media_switch') {
    const cur = b.media?.current ?? '';
    const to = cur === 'plex' ? 'Jellyfin' : 'Plex';
    const from = cur === 'plex' ? 'Plex' : cur === 'jellyfin' || cur === 'both' ? 'Jellyfin' : '';
    return { title: `Switch to ${to}?`, yes: 'Yes, switch', danger: !!from,
      body: `${from ? `${from} will stop, and anyone watching on it will be cut off. ` : ''}${to} takes up to a minute to start.` };
  }
  if (b.state === 'disarmed') return { title: `Arm ${b.label}?`, body: 'Cameras will record motion and send alerts.' };
  if (b.state?.startsWith('armed_') || b.state === 'triggered') {
    return { title: `Disarm ${b.label}?`, body: 'Cameras will stop recording motion.', danger: true };
  }
  return { title: `Run ${b.label}?` };
}

async function press(b) {
  if (busy.has(b.id)) return;
  if (b.kind === 'media_switch' && b.media?.switching) { toast('Already switching, please wait'); return; }
  if (b.confirm && !(await confirmDialog(question(b)))) return;
  busy.add(b.id);
  patch('home', () => {});
  try {
    const data = await postJSON('/api/home/action', { id: b.id }, { timeout: 15000 });
    patch('home', (d) => {
      const cur = d.buttons.find((x) => x.id === b.id);
      if (!cur) return;
      if (data.pending && cur.media) { cur.media.switching = data.target; cur.media.error = null; }
      else if (cur.state === 'on' || cur.state === 'off') cur.state = cur.state === 'on' ? 'off' : 'on';  // HA confirms on the next poll
    });
  } catch (err) {
    toast(err.status ? err.message : err.message === 'timed out' ? 'No answer from the server' : 'Could not reach the server');
  } finally {
    busy.delete(b.id);
    patch('home', () => {});
    refreshSoon('home', 1500);
  }
}

function tile(b, compact) {
  const d = describe(b);
  const isBusy = busy.has(b.id);
  const name = b.icon || (b.kind === 'media_switch' ? 'media' : 'home');
  return h('button', {
    type: 'button', class: `hc-tile ${d.cls} ${isBusy ? 'busy' : ''}`, 'aria-busy': isBusy ? 'true' : null,
    title: d.hint, onclick: () => press(b),
  },
  icon(ICONS.has(name) ? name : 'home', 'hc-icon'),
  h('span', { class: 'hc-name' }, d.name),
  h('span', { class: 'hc-word' }, isBusy ? '…' : d.word),
  !compact && h('span', { class: 'hc-hint' }, d.hint));
}

function problem(d, err) {
  if (err?.status === 403) return message('Not available here', 'Home controls only work on the home network.');
  if (!d) return message(err ? 'Home controls unavailable' : 'Loading…', err ? `${err.message}. Retrying automatically.` : '', err ? 'warn' : '');
  if (!d.enabled || !d.buttons.length) {
    return message('No home controls', d.enabled ? 'Add buttons under "home" in config.json.' : 'Home controls are turned off in config.json.');
  }
  return null;
}

function summary(d, err) {
  const parts = [];
  const m = mediaOf(d);
  if (m) {
    parts.push(m.switching ? 'Switching media server' : MEDIA[m.current] ? `${MEDIA[m.current]} running`
      : m.current === 'both' ? 'Plex + Jellyfin both running' : 'No media server');
  }
  const on = d.buttons.filter((b) => b.state === 'on').length;
  if (d.ha_ok === false) parts.push(d.ha_error || 'Home Assistant not connected');
  else if (on) parts.push(`${on} on`);
  if (err) parts.push('offline, showing last data');
  return h('p', { class: `meta ${d.ha_ok === false || err ? 'warn' : ''}` }, parts.join(' · '));
}

function mountInto(root, wrap, compact) {
  const body = h('div', { class: 'hc-body' });
  replace(root, wrap(body));
  return subscribe('home', (d, err) => {
    const p = problem(d, err);
    if (p) return replace(body, p);
    replace(body, h('div', { class: `hc-grid ${compact ? 'compact' : ''}` }, d.buttons.map((b) => tile(b, compact))), summary(d, err));
  });
}

export default {
  id: 'home',
  label: 'Home',
  icon: 'home',
  mount(root) {
    return mountInto(root, (body) => h('div', { class: 'view-home' }, h('h1', { class: 'view-title' }, 'Home'), body), false);
  },
  card(root) {
    return mountInto(root, (body) => card({ title: 'Home', iconName: 'home', href: '#home', cls: 'card-home' }, body), true);
  },
};
