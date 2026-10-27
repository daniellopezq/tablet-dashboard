import { h, replace, card, message, toast } from '../lib/dom.js';
import { subscribe, current, refresh, refreshSoon } from '../lib/store.js';
import { getJSON, postJSON } from '../lib/api.js';
import { ago, formatHM } from '../lib/clock.js';
import { laundryPanel } from './laundry.js';

// Setting a blank picture (not removing src) is what makes browsers drop an MJPEG connection.
const BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';

const stillUrl = (c) => `/api/cameras/${encodeURIComponent(c.id)}.jpg?v=${encodeURIComponent(c.version || '')}`;
const liveUrl = (c, token, hd) => `/api/cameras/${encodeURIComponent(c.id)}/live.mjpg?t=${token}${hd && c.hd ? '&q=hd' : ''}`;

// Phones and PCs default to the HD stream; the choice is remembered on this device.
const HD_KEY = 'dashboard.camHd';
function hdWanted() { try { return localStorage.getItem(HD_KEY) !== '0'; } catch { return true; } }
function setHdWanted(on) { try { localStorage.setItem(HD_KEY, on ? '1' : '0'); } catch { /* private mode */ } }

const MODES = [['auto', 'Auto'], ['day', 'Day'], ['night', 'Night']];
const MODE_TOAST = { night: 'Night mode: infrared, black and white', day: 'Day mode: colour', auto: 'Auto: the camera picks day or night' };
const ARROWS = [['up', '▲'], ['down', '▼'], ['left', '◀'], ['right', '▶']];
const KEYS = { ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right' };

const errText = (err) => (err.status ? err.message : 'Could not reach the server');

/** Change an <img> only when the URL really changed: no reload, no flicker, no dropped stream. */
function setSrc(img, url) {
  if (img.dataset.url === (url || '')) return;
  img.dataset.url = url || '';
  img.parentElement?.classList.toggle('noimg', !url);
  img.src = url || BLANK;
}

/** An <img> whose frame says "No picture" when loading fails (e.g. Home Assistant has none). */
function camImg(cls, alt) {
  const img = h('img', { class: cls, alt });
  img.addEventListener('error', () => img.parentElement?.classList.add('noimg'));
  img.addEventListener('load', () => { if (img.dataset.url) img.parentElement?.classList.remove('noimg'); });
  return img;
}

/** [text, class] badges, most important first. */
function badges(c) {
  const out = [];
  if (!c.available) out.push(['Offline', 'bad']);
  if (c.live && c.available) out.push(['Live', 'live']);
  if (c.motion) out.push(['Motion', 'bad']);
  if (c.battery_low) out.push(['Battery low', 'warn']);
  if (c.motion_detection === false) out.push(['Motion off', 'dim']);
  return out;
}
const badge = ([text, cls]) => h('span', { class: `cam-badge ${cls}` }, text);

function problem(d, err) {
  if (err?.status === 403) return message('Not available here', 'Cameras are only shown on the home network.');
  if (!d) return message(err ? 'Cameras unavailable' : 'Loading cameras…', err ? `${err.message}. Retrying automatically.` : '', err ? 'warn' : '');
  if (!d.enabled || !d.cameras.length) return message('No cameras', 'Add cameras under "cameras" in config.json.');
  if (d.ha_ok === false && !d.cameras.some((c) => c.live)) {
    return message('Home Assistant not connected', d.ha_error || 'Check that Home Assistant is running.', 'warn');
  }
  return null;
}

function summary(d, err) {
  const moving = d.cameras.filter((c) => c.motion).map((c) => c.label);
  const live = d.cameras.filter((c) => c.live).map((c) => c.label);
  const text = (moving.length ? `Motion: ${moving.join(', ')}` : 'No motion')
    + (live.length ? ` · live: ${live.join(', ')}` : '')
    + (d.ha_ok === false ? ` · Home Assistant: ${d.ha_error || 'not connected'}` : '')
    + (err ? ' · offline, showing last data' : '');
  return h('p', { class: `meta ${moving.length || err ? 'warn' : ''}` }, text);
}

/** Grid of stills. Keeps the <img> elements between polls so pictures only reload when they change. */
function grid(compact) {
  const el = h('div', { class: `cam-grid ${compact ? 'compact' : ''}` });
  const tiles = new Map();
  function update(d) {
    const ids = d.cameras.map((c) => c.id).join('|');
    if (el.dataset.ids !== ids) {
      el.dataset.ids = ids;
      tiles.clear();
      replace(el, d.cameras.map((c) => {
        const img = camImg('cam-img', c.label);
        const cap = h('div', { class: 'cam-cap' });
        const a = h('a', { class: 'cam-tile', href: `#cameras/${encodeURIComponent(c.id)}` }, h('div', { class: 'cam-frame' }, img), cap);
        tiles.set(c.id, { a, img, cap });
        return a;
      }));
    }
    for (const c of d.cameras) {
      const t = tiles.get(c.id);
      setSrc(t.img, c.available === false ? '' : stillUrl(c));
      t.a.classList.toggle('motion', !!c.motion);
      const b = badges(c);
      replace(t.cap, h('span', { class: 'cam-name' }, c.label), b[0] && badge(b[0]),
        !compact && h('span', { class: 'cam-time' }, ago(c.updated_ms)));
    }
  }
  return { el, update };
}

/** One camera: live MJPEG for RTSP cameras (paused after live_max_seconds or when hidden), else snapshots. */
function detail(root, id) {
  const img = camImg('cam-big', '');
  const title = h('h1', { class: 'view-title' }, '');
  const info = h('div', { class: 'cam-info' });
  const action = h('button', { type: 'button', class: 'btn' }, '');
  // On-demand live view of a Blink camera (through the Blink proxy); each start is a Blink cloud session.
  const liveBtn = h('button', { type: 'button', class: 'btn btn-quiet', hidden: true, onclick: () => toggleBlinkLive() }, '');
  const arrows = ARROWS.map(([dir, text]) => h('button',
    { type: 'button', class: `cam-ptz cam-ptz-${dir}`, 'aria-label': `Move ${dir}`, hidden: true, onclick: () => move(dir) }, text));
  const quality = h('div', { class: 'chips', role: 'group', 'aria-label': 'Picture quality' });
  const modes = h('div', { class: 'chips', role: 'group', 'aria-label': 'Day or night' });
  const controls = h('div', { class: 'cam-ctl', hidden: true }, quality, modes);
  replace(root, h('div', { class: 'view-cam-detail' },
    h('div', { class: 'cam-detail-head' }, h('a', { class: 'btn btn-quiet', href: '#cameras' }, '← All cameras'), title),
    h('div', { class: 'cam-big-frame' }, img, ...arrows), controls, h('div', { class: 'cam-detail-foot' }, info, h('div', { class: 'cam-actions' }, liveBtn, action))));

  let token = 0;
  let liveOn = false;
  let paused = false;
  let liveTimer = null;
  let cam = null;
  let hd = hdWanted();
  let mode = null;        // 'day' | 'night' | 'auto' | null (unknown)
  let modeFetched = false;
  let moving = false;
  let blinkLive = false;

  function toggleBlinkLive() {
    if (!cam?.can_live) return;
    if (blinkLive) {
      blinkLive = false;
      paused = false;
      stopLive();
    } else {
      blinkLive = true;
      paused = false;
      toast('Starting live view; Blink takes a few seconds');
    }
    draw();
  }

  async function move(dir) {
    if (!cam?.ptz || moving) return;
    moving = true;
    drawControls();
    try {
      await postJSON('/api/cameras/control', { id: cam.id, move: dir }, { timeout: 10000 });
    } catch (err) {
      toast(errText(err));
    }
    moving = false;
    drawControls();
  }

  async function setMode(next) {
    if (!cam?.day_night || next === mode) return;
    try {
      const r = await postJSON('/api/cameras/control', { id: cam.id, mode: next }, { timeout: 10000 });
      mode = r.mode;
      toast(MODE_TOAST[mode]);
    } catch (err) {
      toast(errText(err));
    }
    drawControls();
  }

  async function fetchMode() {
    modeFetched = true;
    try {
      mode = (await getJSON(`/api/cameras/${encodeURIComponent(cam.id)}/controls`, { timeout: 8000 })).mode;
    } catch { mode = null; }
    drawControls();
  }

  function chip(text, selected, onclick) {
    return h('button', { type: 'button', class: 'chip', 'aria-pressed': String(selected), 'aria-selected': String(selected), onclick }, text);
  }

  function drawControls() {
    const live = !!cam?.live;
    for (const b of arrows) { b.hidden = !(live && cam.ptz); b.disabled = moving; }
    controls.hidden = !(live && (cam.hd || cam.day_night));
    if (controls.hidden) return;
    if (cam.day_night && !modeFetched) fetchMode();
    replace(quality, cam.hd && [chip('SD', !hd, () => setHd(false)), chip('HD', hd, () => setHd(true))]);
    replace(modes, cam.day_night && MODES.map(([m, text]) => chip(text, mode === m, () => setMode(m))));
  }

  function setHd(on) {
    if (on === hd) return;
    hd = on;
    setHdWanted(on);
    draw();
  }

  function onKey(e) {
    const dir = KEYS[e.key];
    if (!dir || !cam?.ptz || e.altKey || e.ctrlKey || e.metaKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    e.preventDefault();
    move(dir);
  }

  function startLive() {
    clearTimeout(liveTimer);
    token += 1;
    liveOn = true;
    paused = false;
    const secs = current('cameras')?.live_max_seconds || 600;
    liveTimer = setTimeout(() => { paused = true; draw(); }, secs * 1000);
  }
  function stopLive() {
    clearTimeout(liveTimer);
    liveOn = false;
    setSrc(img, '');
  }
  function onVisibility() {
    if (!document.hidden) return;
    if (blinkLive) { blinkLive = false; paused = false; stopLive(); draw(); return; }
    if (cam?.live) { paused = true; stopLive(); draw(); }
  }

  function draw() {
    if (!cam) return;
    title.textContent = cam.label;
    img.alt = cam.label;
    liveBtn.hidden = !cam.can_live;
    liveBtn.textContent = blinkLive ? 'Stop live' : 'Live view';
    if (cam.live || blinkLive) {
      if (!liveOn && !paused) startLive();
      setSrc(img, paused ? '' : liveUrl(cam, token, hd));
      drawControls();
      replace(info, h('div', null, paused ? 'Live view paused' : cam.available ? 'Live' : 'Camera not answering'),
        !cam.available && cam.error && h('div', { class: 'meta warn' }, cam.error),
        cam.live_error && h('div', { class: 'meta warn' }, cam.live_error));
      action.textContent = paused || !cam.available ? 'Resume live' : 'Restart live';
      action.disabled = false;
      return;
    }
    drawControls();
    setSrc(img, cam.available === false ? '' : stillUrl(cam));
    replace(info,
      h('div', null, cam.updated_ms ? `Picture ${formatHM(cam.updated_ms)} (${ago(cam.updated_ms)})` : 'No picture yet'),
      cam.motion_ms && h('div', { class: 'meta' }, `Motion ${ago(cam.motion_ms)}`),
      h('div', { class: 'cam-badges' }, badges(cam).map(badge)));
    action.textContent = 'New snapshot';
    action.disabled = !cam.can_snapshot || !cam.available;
  }

  action.addEventListener('click', async () => {
    if (!cam) return;
    if (cam.live || blinkLive) { stopLive(); startLive(); draw(); return; }
    action.disabled = true;
    try {
      await postJSON('/api/cameras/snapshot', { id: cam.id }, { timeout: 15000 });
      toast('Snapshot requested; the picture updates in about 15 s');
      refreshSoon('cameras', 8000);
      setTimeout(() => refresh('cameras'), 18000);
    } catch (err) {
      toast(err.status ? err.message : 'Could not reach the server');
      action.disabled = false;
    }
  });
  document.addEventListener('visibilitychange', onVisibility);
  document.addEventListener('keydown', onKey);

  const unsub = subscribe('cameras', (d, err) => {
    const p = problem(d, err);
    if (p && !d) return replace(info, p);
    cam = d?.cameras.find((c) => c.id === id) ?? null;
    if (!cam) { replace(root, message('Camera not found', 'It may have been removed from config.json.'), h('a', { class: 'btn', href: '#cameras' }, 'All cameras')); return; }
    draw();
  });
  return () => {
    unsub(); stopLive();
    document.removeEventListener('visibilitychange', onVisibility);
    document.removeEventListener('keydown', onKey);
  };
}

export default {
  id: 'cameras',
  label: 'Cameras',
  icon: 'camera',

  mount(root, param) {
    if (param) return detail(root, param);
    const g = grid(false);
    const status = h('div');
    const foot = h('div');
    const laundry = laundryPanel();
    replace(root, h('div', { class: 'view-cameras' }, h('h1', { class: 'view-title' }, 'Cameras'), status, g.el, foot,
      laundry.el));
    const unsub = subscribe('cameras', (d, err) => {
      const p = problem(d, err);
      replace(status, p);
      g.el.hidden = !!p;
      if (p) return replace(foot);
      g.update(d);
      replace(foot, summary(d, err));
    });
    return () => { unsub(); laundry.stop(); };
  },

  card(root) {
    const g = grid(true);
    const status = h('div');
    replace(root, card({ title: 'Cameras', iconName: 'camera', href: '#cameras', cls: 'card-cams' }, status, g.el));
    return subscribe('cameras', (d, err) => {
      const p = problem(d, err);
      replace(status, p);
      g.el.hidden = !!p;
      if (!p) g.update(d);
    });
  },
};

/**
 * Motion alerts on every screen. Unlike the tablet, which jumps to the camera, phones and PCs get a
 * banner with a link, so whatever the user is doing isn't interrupted. Each alert shows once.
 */
export function watchMotion(bannerEl) {
  const seen = new Set();
  let hideTimer = null;
  return subscribe('cameras', (d) => {
    const a = d?.alert;
    if (!a || seen.has(a.key)) return;
    seen.add(a.key);
    const c = d.cameras.find((x) => x.id === a.id);
    if (!c || location.hash === `#cameras/${a.id}`) return;
    replace(bannerEl,
      h('span', null, `Motion: ${c.label}`),
      h('a', { class: 'btn btn-small', href: `#cameras/${encodeURIComponent(c.id)}`, onclick: () => { bannerEl.hidden = true; } }, 'View'),
      h('button', { type: 'button', class: 'btn-plain', 'aria-label': 'Dismiss', onclick: () => { bannerEl.hidden = true; } }, '✕'));
    bannerEl.hidden = false;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { bannerEl.hidden = true; }, Math.max(10, d.popup_seconds || 60) * 1000);
  });
}
