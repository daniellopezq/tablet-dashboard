import { h, replace, card, message } from '../lib/dom.js';
import { subscribe } from '../lib/store.js';
import { ago } from '../lib/clock.js';

const STATE_LABEL = { ok: 'Up', warn: 'Degraded', fail: 'Down', unknown: 'Unknown' };
const GROUP_LABEL = { kuma: 'Monitors', service: 'Home services' };
const HISTORY = { o: 'ok', w: 'warn', f: 'fail', u: 'unknown' };

function problem(u, err) {
  if (err?.status === 403) return message('Not available here', 'Uptime is only shown on the home network.');
  if (err && !u) return message('Uptime unavailable', 'Can’t reach the dashboard server.', 'warn');
  if (!u.enabled) return message('Uptime checks are off', 'Turn them on in the admin page.');
  if (!u.checks.length) return message('No checks yet', 'The first run is under way.');
  return null;
}

function summary(u) {
  const c = u.counts;
  const bad = (c.fail || 0) + (c.warn || 0);
  return h('div', { class: `up-summary ${c.fail ? 'fail' : c.warn ? 'warn' : 'ok'}` },
    h('strong', null, bad ? `${bad} need${bad === 1 ? 's' : ''} attention` : 'All systems up'),
    h('span', null, `${c.ok} up`, c.warn ? ` · ${c.warn} degraded` : '', c.fail ? ` · ${c.fail} down` : '',
      c.unknown ? ` · ${c.unknown} unknown` : ''),
    u.kuma_ok === false && h('span', { class: 'warn' }, ' · Kuma unreachable'));
}

function bars(recent) {
  return h('span', { class: 'up-bars', 'aria-hidden': 'true' },
    [...(recent || '')].map((ch) => h('i', { class: HISTORY[ch] || 'unknown' })));
}

function row(c, compact) {
  return h('li', { class: `up-row ${c.state}` },
    h('span', { class: 'dot', title: STATE_LABEL[c.state] }),
    h('span', { class: 'up-name' }, c.name),
    !compact && bars(c.recent),
    h('span', { class: 'up-pct' }, c.uptime_pct == null ? '' : `${c.uptime_pct}%`),
    !compact && h('span', { class: 'up-ping' }, c.ping_ms == null ? '' : `${c.ping_ms} ms`),
    h('span', { class: 'up-state' }, STATE_LABEL[c.state] ?? c.state));
}

function groups(u) {
  const by = new Map();
  for (const c of u.checks) {
    if (!by.has(c.group)) by.set(c.group, []);
    by.get(c.group).push(c);
  }
  return [...by].map(([g, list]) => h('div', { class: 'up-group' },
    h('h2', { class: 'up-group-title' }, GROUP_LABEL[g] ?? g),
    h('ul', { class: 'up-list' }, list.map((c) => row(c, false)))));
}

export default {
  id: 'uptime',
  label: 'Uptime',
  icon: 'uptime',

  mount(root) {
    const body = h('div');
    replace(root, h('div', { class: 'view-uptime' }, h('h1', { class: 'view-title' }, 'Uptime'), body));
    return subscribe('uptime', (u, err) => {
      const p = problem(u, err);
      if (p) return replace(body, p);
      const mins = u.checks[0]?.history_minutes;
      replace(body, summary(u), groups(u),
        h('p', { class: 'meta' }, `Checked ${ago(u.last_run_ms)} every ${u.interval_seconds} s`,
          mins ? ` · % over the last ${mins} min (resets when the server restarts)` : ''));
    });
  },

  card(root) {
    const body = h('div');
    replace(root, card({ title: 'Uptime', iconName: 'uptime', href: '#uptime', cls: 'card-uptime' }, body));
    return subscribe('uptime', (u, err) => {
      const p = problem(u, err);
      if (p) return replace(body, p);
      // Problems first across both groups, at most 6 rows.
      const rank = { fail: 0, warn: 1, unknown: 2, ok: 3 };
      const top = [...u.checks].sort((a, b) => (rank[a.state] ?? 2) - (rank[b.state] ?? 2)).slice(0, 6);
      replace(body, summary(u), h('ul', { class: 'up-list compact' }, top.map((c) => row(c, true))));
    });
  },
};
