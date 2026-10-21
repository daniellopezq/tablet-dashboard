// Modern dashboard for phones and PCs. The tablet keeps the classic ES5 view in /web.
import { getJSON } from './lib/api.js';
import { defineFeed } from './lib/store.js';
import { initClock, onTick, formatHM } from './lib/clock.js';
import { h, icon, replace } from './lib/dom.js';
import weather from './sections/weather.js';
import maps from './sections/maps.js';
import uptime from './sections/uptime.js';
import calendar from './sections/calendar.js';
import home, { homeInterval } from './sections/home.js';
import cameras, { watchMotion } from './sections/cameras.js';
import ask, { setHttpsPort } from './sections/ask.js';
import { overview } from './sections/overview.js';

const SECTIONS = [
  weather,
  home,
  maps,
  cameras,
  calendar,
  uptime,
  ask,
];
const OVERVIEW = overview(SECTIONS);
const ROUTES = [OVERVIEW, ...SECTIONS];
// Phone tab bar: these four, the rest under "More". Ask is here for one-tap voice; weather tops the Overview.
const TABBAR = ['overview', 'home', 'cameras', 'assistant'];

const $ = (id) => document.getElementById(id);
let unmount = null;
let config = {};

/** "#cameras/v380" -> [cameras section, "v380"] */
function route() {
  const [id, param] = location.hash.slice(1).split('/');
  const r = ROUTES.find((x) => x.id === id);
  return r ? [r, param ? decodeURIComponent(param) : null] : [OVERVIEW, null];
}

function render() {
  const [r, param] = route();
  unmount?.();
  unmount = null;
  const content = $('content');
  content.replaceChildren();
  content.dataset.view = r.id;
  unmount = r.mount(content, param) ?? null;
  document.title = r === OVERVIEW ? 'Dashboard' : `${r.label} · Dashboard`;
  for (const a of document.querySelectorAll('[data-route]')) {
    if (a.dataset.route === r.id) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  const moreBtn = $('tab-more');
  moreBtn?.classList.toggle('active', !TABBAR.includes(r.id));
  closeMore();
  content.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

const navLink = (r, cls) => h('a', { class: cls, href: `#${r.id}`, dataset: { route: r.id } },
  icon(r.icon, 'nav-icon'), h('span', null, r.label));

function buildNav() {
  replace($('side-nav'), ROUTES.map((r) => navLink(r, 'side-link')));
  replace($('tabbar'),
    ROUTES.filter((r) => TABBAR.includes(r.id)).map((r) => navLink(r, 'tab')),
    h('button', { type: 'button', class: 'tab', id: 'tab-more', onclick: openMore },
      h('span', { class: 'nav-icon more-dots', 'aria-hidden': 'true' }, '•••'), h('span', null, 'More')));
  replace($('more-grid'), ROUTES.filter((r) => !TABBAR.includes(r.id)).map((r) => navLink(r, 'tile')));
  $('more-close').addEventListener('click', closeMore);
  $('more-sheet').addEventListener('click', (e) => { if (e.target === $('more-sheet')) closeMore(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMore(); });
}

function openMore() { $('more-sheet').hidden = false; }
function closeMore() { $('more-sheet').hidden = true; }

function footer() {
  const links = [
    h('a', { href: '/?ui=classic' }, 'Classic view'),
    config.https_port && location.protocol === 'http:'
      ? h('a', { href: `https://${location.hostname}:${config.https_port}/${location.hash}` }, 'Secure (https) address')
      : null,
  ].filter(Boolean);
  const text = h('span', null, `v${config.version ?? '?'}`, ...links.flatMap((a) => [' · ', a]));
  replace($('foot'), text);
  replace($('side-foot'), text.cloneNode(true));
}

function banner() {
  const off = config.offline_test ?? [];
  const b = $('banner');
  b.hidden = !off.length;
  if (off.length) b.textContent = `Offline test running (${off.join(', ')}) — the dashboard is simulating an outage.`;
}

export async function start() {
  try {
    config = await getJSON('/api/config', { timeout: 8000 });
  } catch (err) {
    console.warn('config unavailable', err);
  }
  defineFeed('weather', '/api/weather', 60 * 1000);
  defineFeed('maps', '/api/maps', 5 * 60 * 1000);
  defineFeed('uptime', '/api/uptime', 30 * 1000);
  defineFeed('calendar', '/api/calendar', 60 * 1000);
  defineFeed('home', '/api/home', homeInterval);
  defineFeed('cameras', '/api/cameras', 15 * 1000);  // always on, for the motion alert
  setHttpsPort(config.https_port);
  initClock(config);
  onTick((ms) => { $('brand-clock').textContent = formatHM(ms); });
  buildNav();
  footer();
  banner();
  watchMotion($('alert'));
  window.addEventListener('hashchange', render);
  render();
}
