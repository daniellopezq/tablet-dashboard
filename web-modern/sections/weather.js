import { h, icon, replace, card, message } from '../lib/dom.js';
import { subscribe } from '../lib/store.js';
import { onTick, clockParts, formatDate, dayLabel, ago, toggle24h, isSynced } from '../lib/clock.js';

const temp = (v) => (v == null ? '--' : `${Math.round(v)}°`);

function clockBlock() {
  const hm = h('span', { class: 'clock-hm' }, '--:--');
  const side = h('span', { class: 'clock-side' });
  const date = h('div', { class: 'clock-date' }, '');
  const el = h('button', { type: 'button', class: 'clock', title: 'Tap to switch 12/24 h', onclick: toggle24h },
    h('span', { class: 'clock-time' }, hm, side), date);
  const stop = onTick((ms) => {
    const p = clockParts(ms);
    hm.textContent = p.hm;
    replace(side, p.ampm && h('span', { class: 'clock-ampm' }, p.ampm), h('span', { class: 'clock-sec' }, p.sec));
    date.textContent = formatDate(ms) + (isSynced() ? '' : ' · syncing…');
  });
  return { el, stop };
}

function problem(w, err) {
  if (err && !w) return message('Weather unavailable', 'Can’t reach the dashboard server.', 'warn');
  if (w.status === 'unconfigured') return message('Weather not set up', 'Set weather.latitude and longitude in config.json.');
  if (w.status === 'loading') return message('Loading weather…');
  if (!w.data) return message('Weather unavailable', w.error || 'No data yet.', 'warn');
  return null;
}

function metaLine(w) {
  const text = (w.stale ? 'Stale · updated ' : 'Updated ') + ago(w.updated_ms);
  return h('p', { class: `meta ${w.stale ? 'warn' : ''}` }, text, ' · ',
    h('a', { href: w.attribution?.url, target: '_blank', rel: 'noopener' }, w.attribution?.text ?? 'Open-Meteo'));
}

function nowBlock(w) {
  const { current: c, today: t } = w.data;
  return h('div', { class: 'wx-now' },
    icon(c.icon, 'wx-icon'),
    h('div', null,
      h('div', { class: 'wx-temp' }, temp(c.temperature)),
      h('div', { class: 'wx-cond' }, c.condition),
      h('div', { class: 'wx-sub' }, `High ${temp(t.max)} · Low ${temp(t.min)}`,
        c.humidity != null ? ` · ${c.humidity}% humidity` : '')));
}

function forecast(w) {
  return h('div', { class: 'wx-days' }, w.data.forecast.map((d) =>
    h('div', { class: 'wx-day' },
      h('div', { class: 'wx-day-name' }, dayLabel(d.date)),
      icon(d.icon, 'wx-day-icon'),
      h('div', { class: 'wx-day-cond' }, d.condition),
      h('div', { class: 'wx-day-temps' }, h('strong', null, temp(d.max)), ' ', h('span', null, temp(d.min))))));
}

export default {
  id: 'weather',
  label: 'Weather',
  icon: 'partly',

  mount(root) {
    const clock = clockBlock();
    const body = h('div', { class: 'wx-body' });
    replace(root, h('div', { class: 'view-weather' }, h('div', { class: 'wx-top' }, clock.el, body)));
    const unsub = subscribe('weather', (w, err) => {
      const p = problem(w, err);
      if (p) return replace(body, p);
      replace(body, h('p', { class: 'wx-place' }, w.label), nowBlock(w), forecast(w), metaLine(w));
    });
    return () => { clock.stop(); unsub(); };
  },

  card(root) {
    const clock = clockBlock();
    const body = h('div');
    replace(root, card({ title: 'Now', iconName: 'partly', href: '#weather', cls: 'card-wx' }, clock.el, body));
    const unsub = subscribe('weather', (w, err) => {
      const p = problem(w, err);
      if (p) return replace(body, p);
      replace(body, nowBlock(w), forecast(w));
    });
    return () => { clock.stop(); unsub(); };
  },
};
