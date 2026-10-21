import { h, replace, card, message } from '../lib/dom.js';
import { subscribe } from '../lib/store.js';
import { ago } from '../lib/clock.js';

/** A rotating weather-map viewer; tapping a layer chip pins it. */
function viewer(useSmall) {
  let layers = [];
  let index = 0;
  let pinned = false;
  let timer = null;
  let rotateMs = 10000;
  const img = h('img', { class: 'map-img', alt: '' });
  const chips = h('div', { class: 'chips', role: 'tablist' });
  const caption = h('p', { class: 'meta' });
  const el = h('div', { class: 'map-viewer' }, h('div', { class: 'map-frame' }, img), chips, caption);

  function show(i) {
    if (!layers.length) return;
    index = (i + layers.length) % layers.length;
    const l = layers[index];
    img.src = `${useSmall ? l.small_url : l.url}?t=${l.updated_ms}`;
    img.alt = `${l.title} map`;
    [...chips.children].forEach((c, k) => c.setAttribute('aria-selected', String(k === index)));
    caption.textContent = `${l.title} · ${l.stale ? 'stale, ' : ''}updated ${ago(l.updated_ms)}`;
    caption.classList.toggle('warn', !!l.stale);
  }

  function schedule() {
    clearInterval(timer);
    if (!pinned && layers.length > 1) timer = setInterval(() => show(index + 1), rotateMs);
  }

  function update(m) {
    rotateMs = Math.max(3, m.rotate_seconds || 10) * 1000;
    const prevName = layers[index]?.name;
    layers = m.layers;
    replace(chips, layers.map((l, k) => h('button', {
      type: 'button', class: 'chip', role: 'tab',
      onclick: () => { pinned = !(pinned && index === k); show(k); schedule(); },
    }, l.title)));
    const keep = layers.findIndex((l) => l.name === prevName);
    show(keep >= 0 ? keep : 0);
    schedule();
  }

  return { el, update, stop: () => clearInterval(timer) };
}

function problem(m, err) {
  if (err && !m) return message('Maps unavailable', 'Can’t reach the dashboard server.', 'warn');
  if (m.status === 'unconfigured') return message('Maps not set up', 'Add an OpenWeatherMap key on the admin page.');
  if (!m.layers?.length) return message(m.status === 'loading' ? 'Loading maps…' : 'Maps unavailable', m.error, 'warn');
  return null;
}

function attribution(m) {
  return h('p', { class: 'meta' }, (m.attribution ?? []).map((a, k) =>
    [k ? ' · ' : '', h('a', { href: a.url, target: '_blank', rel: 'noopener' }, a.text)]));
}

function mountInto(root, wrap, small) {
  const v = viewer(small);
  const status = h('div');
  const attrib = h('div');
  replace(root, wrap(status, v.el, attrib));
  let shown = false;
  const unsub = subscribe('maps', (m, err) => {
    const p = problem(m, err);
    replace(status, p);
    v.el.hidden = !!p;
    if (p) return;
    if (!shown || JSON.stringify(m.layers) !== v.lastLayers) {
      v.lastLayers = JSON.stringify(m.layers);
      v.update(m);
      shown = true;
    }
    replace(attrib, attribution(m));
  });
  return () => { v.stop(); unsub(); };
}

export default {
  id: 'maps',
  label: 'Maps',
  icon: 'map',

  mount(root) {
    return mountInto(root, (...kids) => h('div', { class: 'view-maps' }, h('h1', { class: 'view-title' }, 'Weather maps'), ...kids), false);
  },

  card(root) {
    return mountInto(root, (...kids) => card({ title: 'Maps', iconName: 'map', href: '#maps', cls: 'card-maps' }, ...kids), true);
  },
};
