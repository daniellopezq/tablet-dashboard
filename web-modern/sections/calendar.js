import { h, replace, card, message } from '../lib/dom.js';
import { subscribe } from '../lib/store.js';
import { now, onTick, isoDay, formatWallHM, longDayLabel, ago, is24h } from '../lib/clock.js';

function problem(c, err) {
  if (err?.status === 403) return message('Not available here', 'The calendar is only shown on the home network.');
  if (err && !c) return message('Calendar unavailable', 'Can’t reach the dashboard server. Retrying.', 'warn');
  if (c.status === 'disabled') return message('Calendar is turned off', 'Set calendar.enabled to true in config.json.');
  if (c.status === 'setup') return message('Google Calendar not connected', c.message || 'Connect it on the admin page.');
  if (c.status === 'loading') return message('Loading calendar…', 'Contacting Google.');
  if (!c.days) return message('Calendar unavailable', `${c.message ? c.message + ' ' : ''}Retrying automatically.`, 'warn');
  return null;
}

function eventRow(ev, t) {
  let when;
  let cls = 'cal-ev';
  if (ev.all_day) {
    when = h('div', { class: 'cal-time' }, ev.multi_day ? 'All days' : 'All day');
  } else {
    when = h('div', { class: 'cal-time' }, formatWallHM(ev.start_hm),
      h('span', { class: 'cal-end' }, (ev.multi_day ? '→ ' : '') + formatWallHM(ev.end_hm)));
    if (ev.end_ms <= t) cls += ' past';
    else if (ev.start_ms <= t) cls += ' now';
  }
  // Plain text nodes only (h() never parses HTML): titles and places come from the calendar.
  return h('li', { class: cls }, when,
    h('div', { class: 'cal-what' }, h('div', { class: 'cal-title' }, ev.title),
      ev.location && h('div', { class: 'cal-loc' }, ev.location)));
}

/** Upcoming days (skipping ones before today, in case the cache is from before midnight). */
function upcomingDays(c, limitEvents) {
  const today = isoDay(now());
  const t = now();
  const out = [];
  let shown = 0;
  for (const day of c.days) {
    if (day.date < today) continue;
    let events = day.events;
    if (limitEvents != null) {
      events = events.filter((ev) => ev.all_day || ev.end_ms > t);  // compact view: hide what's over
      if (shown >= limitEvents) break;
      events = events.slice(0, limitEvents - shown);
      if (!events.length) continue;
      shown += events.length;
    }
    out.push(h('div', { class: `cal-day ${day.date === today ? 'today' : ''}` },
      h('h2', { class: 'cal-day-title' }, longDayLabel(day.date)),
      events.length ? h('ul', { class: 'cal-list' }, events.map((ev) => eventRow(ev, t)))
        : h('p', { class: 'cal-empty' }, 'Nothing scheduled.')));
  }
  return out;
}

function meta(c, err) {
  let text = (c.stale ? 'Stale · updated ' : 'Updated ') + ago(c.updated_ms);
  if (c.truncated) text += ` · showing the first ${c.count}`;
  if (c.stale && c.message) text += ` · ${c.message}`;
  if (err) text += ' · server unreachable';
  return h('p', { class: `meta ${c.stale || err ? 'warn' : ''}` }, text);
}

/** Re-render on new data, every minute (past/now marks) and on a 12/24 h switch. */
function watch(render) {
  let last = { c: null, err: null };
  let key = '';
  const draw = () => {
    if (last.c || last.err) render(last.c, last.err);
  };
  const unsub = subscribe('calendar', (c, err) => { last = { c, err }; draw(); });
  const stopTick = onTick((ms) => {
    const k = `${Math.floor(ms / 60000)}|${is24h()}`;
    if (k !== key) { key = k; draw(); }
  });
  return () => { unsub(); stopTick(); };
}

export default {
  id: 'calendar',
  label: 'Calendar',
  icon: 'calendar',

  mount(root) {
    const body = h('div', { class: 'cal-body' });
    replace(root, h('div', { class: 'view-calendar' }, h('h1', { class: 'view-title' }, 'Calendar'), body));
    return watch((c, err) => {
      const p = problem(c, err);
      if (p) return replace(body, p);
      replace(body, upcomingDays(c),
        c.count === 0 && h('p', { class: 'cal-empty' }, `No events in the next ${c.days_ahead} days.`),
        meta(c, err));
    });
  },

  card(root) {
    const body = h('div', { class: 'cal-body' });
    replace(root, card({ title: 'Calendar', iconName: 'calendar', href: '#calendar', cls: 'card-cal' }, body));
    return watch((c, err) => {
      const p = problem(c, err);
      if (p) return replace(body, p);
      const days = upcomingDays(c, 5);
      replace(body, days.length ? days : h('p', { class: 'cal-empty' }, 'Nothing coming up.'));
    });
  },
};
