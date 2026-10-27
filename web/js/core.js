/* Tablet dashboard client: constants, shared state, DOM and storage helpers, XHR, server-driven time and the view rail.
 * ES5 + XMLHttpRequest only (Android 4.4 WebView). No fetch, Promises, modules or Intl.
 * The clock is driven by server time: each sync anchors server ms to the local
 * Date.now(), and the display advances locally until the next sync.
 * index.html loads the js/ files in order (this one first, main.js last); they share globals, so every
 * top-level name must stay unique and must not shadow a window property. */
'use strict';

var SYNC_INTERVAL_MS = 60000;
var SYNC_RETRY_MS = 15000;
var SYNC_TIMEOUT_MS = 8000;
var CLOCK_STALE_MS = 150000;
var WEATHER_POLL_MS = 60000;
var WEATHER_TIMEOUT_MS = 10000;
var MAPS_POLL_MS = 60000;
var CAL_POLL_MS = 60000;
var UPTIME_POLL_MS = 30000;
var UPTIME_PER_PAGE = 4;
var HOME_POLL_MS = 10000;
var HOME_BUSY_POLL_MS = 2000;   // while the media server is switching
var HOME_WATCH_POLL_MS = 30000; // other tabs, while a tile can warn (garage left open)
var HOME_PER_PAGE = 8;
var CAM_POLL_MS = 15000;        // on every tab, so a motion pop-up can appear anywhere
var CAM_COUNT = 6;
// Grid cards in camera order: rows are cards 0,1,4 and 2,3,5, so up to 4 cameras fill a 2x2 grid.
var CAM_SLOTS_4 = [0, 1, 2, 3];
var CAM_SLOTS_6 = [0, 1, 4, 2, 3, 5];
var CAM_BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==';
var STORAGE_KEY_24H = 'dashboard.use24h';

var DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
var DAYS_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
  'August', 'September', 'October', 'November', 'December'];
var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
var ICONS = {
  'clear': 1, 'clear-night': 1, 'partly': 1, 'partly-night': 1, 'cloud': 1,
  'fog': 1, 'rain': 1, 'snow': 1, 'storm': 1, 'unknown': 1
};

var state = {
  // time sync
  anchorServerMs: null,
  anchorLocalMs: null,
  offsetMin: 0,
  tzName: '',
  tzAbbr: '',
  lastSyncOkLocal: null,
  lastRttMs: null,
  syncInFlight: false,
  syncFailures: 0,
  lastSyncError: '',
  syncTimer: null,
  lastTickLocal: null,
  // weather
  weather: null,
  weatherInFlight: false,
  weatherReachable: true,
  weatherTimer: null,
  // maps slideshow
  maps: null,
  mapsRotateTimer: null,
  mapsPollTimer: null,
  mapsInFlight: false,
  mapsIdx: -1,
  homeIdx: -1,
  homeRotateTimer: null,
  // calendar
  cal: null,
  calInFlight: false,
  calReachable: true,
  calTimer: null,
  calRenderKey: '',
  // ui
  use24h: true,
  hourPrefStored: false,
  diagOpen: false,
  // uptime tab
  up: null,
  upError: null,
  upPage: 0,
  upTimer: null,
  upInFlight: false,
  // home tab
  hc: null,
  hcError: null,
  hcPage: 0,
  hcTimer: null,
  hcInFlight: false,
  hcBusy: {},
  hcAlertSeen: '',          // key of the garage-style warning already dismissed
  hcConfirmId: null,
  // cameras tab + motion pop-up
  cam: null,
  camError: null,
  camTimer: null,
  camInFlight: false,
  camDetail: null,          // id of the camera shown full size, or null for the grid
  camAlertSeen: '',         // key of the last alert already shown
  laundrySeen: '',          // key of the last laundry warning already shown
  camReturnView: null,      // view to go back to when a pop-up times out
  camPopupTimer: null,
  camTouched: false,
  camLiveId: null,          // RTSP camera whose live view is open, or null
  camLiveToken: 0,          // changes on every (re)start, so the <img> opens a new stream
  camLiveEnded: false,      // the live view reached live_max_seconds or failed
  camLiveTimer: null,
  camHd: false,             // live view from the camera's HD stream (tablet default: SD)
  camMode: null,            // day/night mode of the open camera: 'day', 'night', 'auto' or null
  camModeFor: null,         // camera id camMode belongs to
  camMoving: false,
  camBlinkLive: null,       // Blink camera whose on-demand live view was asked for, or null
  loadedLocal: Date.now(),
  serverVersion: ''
};

function $(id) { return document.getElementById(id); }

function setText(el, text) {
  if (el && el.textContent !== text) { el.textContent = text; }
}

function setClass(el, name, on) {
  if (!el) { return; }
  if (on) { el.classList.add(name); } else { el.classList.remove(name); }
}

function pad2(n) { return (n < 10 ? '0' : '') + n; }

// ---- storage (may be unavailable) ----
function storageGet(key) {
  try { return window.localStorage ? window.localStorage.getItem(key) : null; } catch (e) { return null; }
}
function storageSet(key, value) {
  try { if (window.localStorage) { window.localStorage.setItem(key, value); } } catch (e) { /* ignore */ }
}
function storageWorks() {
  try {
    window.localStorage.setItem('dashboard.test', '1');
    window.localStorage.removeItem('dashboard.test');
    return true;
  } catch (e) { return false; }
}

// ---- XHR helper: one callback, explicit timeout, cache-busting ----
function getJSON(url, timeoutMs, cb) {
  var xhr = new XMLHttpRequest();
  var finished = false;
  function finish(err, data) {
    if (finished) { return; }
    finished = true;
    clearTimeout(timer);
    cb(err, data);
  }
  var timer = setTimeout(function () {
    finish(new Error('timeout'));
    try { xhr.abort(); } catch (e) { /* ignore */ }
  }, timeoutMs);
  xhr.onreadystatechange = function () {
    if (xhr.readyState !== 4 || finished) { return; }
    if (xhr.status >= 200 && xhr.status < 300) {
      var data;
      try { data = JSON.parse(xhr.responseText); } catch (e) { finish(new Error('invalid JSON')); return; }
      finish(null, data);
    } else {
      finish(new Error(xhr.status ? 'HTTP ' + xhr.status : 'network error'));
    }
  };
  try {
    xhr.open('GET', url + (url.indexOf('?') < 0 ? '?' : '&') + '_=' + Date.now(), true);
    xhr.send(null);
  } catch (e) {
    finish(new Error('request failed'));
  }
}

// ---- time ----
function serverNow() {
  if (state.anchorServerMs === null) { return null; }
  return state.anchorServerMs + (Date.now() - state.anchorLocalMs);
}

// A Date whose UTC fields hold the wall-clock time in the configured timezone.
function zoned(ms) { return new Date(ms + state.offsetMin * 60000); }

function hourParts(d) {
  var h = d.getUTCHours();
  if (state.use24h) { return { h: pad2(h), ampm: '' }; }
  var ampm = h < 12 ? 'AM' : 'PM';
  h = h % 12;
  if (h === 0) { h = 12; }
  return { h: String(h), ampm: ampm };
}

function formatHM(ms) {
  var d = zoned(ms);
  var p = hourParts(d);
  return p.h + ':' + pad2(d.getUTCMinutes()) + (p.ampm ? ' ' + p.ampm : '');
}

// Short timestamp; adds the date when it is not today (server-time based).
function formatStamp(ms) {
  if (!ms) { return '--'; }
  var now = serverNow();
  var d = zoned(ms);
  var text = formatHM(ms);
  if (now !== null) {
    var n = zoned(now);
    if (n.getUTCFullYear() !== d.getUTCFullYear() || n.getUTCMonth() !== d.getUTCMonth() ||
        n.getUTCDate() !== d.getUTCDate()) {
      text = MONTHS_SHORT[d.getUTCMonth()] + ' ' + d.getUTCDate() + ' ' + text;
    }
  }
  return text;
}

function scheduleSync(delay) {
  if (state.syncTimer) { clearTimeout(state.syncTimer); }
  state.syncTimer = setTimeout(syncTime, delay);
}

function syncTime() {
  if (state.syncInFlight) { return; }
  state.syncInFlight = true;
  var t0 = Date.now();
  getJSON('/api/time', SYNC_TIMEOUT_MS, function (err, data) {
    state.syncInFlight = false;
    var t1 = Date.now();
    if (err || !data || typeof data.now_ms !== 'number' || typeof data.utc_offset_minutes !== 'number') {
      state.syncFailures += 1;
      state.lastSyncError = err ? err.message : 'invalid response';
      scheduleSync(SYNC_RETRY_MS);
      renderClock();
      return;
    }
    var rtt = t1 - t0;
    if (rtt < 0) { rtt = 0; }
    state.anchorServerMs = data.now_ms + rtt / 2;
    state.anchorLocalMs = t1;
    state.offsetMin = data.utc_offset_minutes;
    state.tzName = data.timezone || '';
    state.tzAbbr = data.abbreviation || '';
    state.lastRttMs = rtt;
    state.lastSyncOkLocal = t1;
    state.syncFailures = 0;
    state.lastSyncError = '';
    scheduleSync(SYNC_INTERVAL_MS);
    renderClock();
    renderWeather();
    homeCheckAlert();  // its "since" time may have been drawn before the timezone was known
  });
}

function clockHealth() {
  if (state.anchorServerMs === null) {
    return state.syncFailures > 0 ? 'bad' : 'pending';
  }
  var age = Date.now() - state.lastSyncOkLocal;
  if (state.syncFailures >= 2) { return 'bad'; }
  if (age > CLOCK_STALE_MS || state.syncFailures > 0) { return 'warn'; }
  return 'ok';
}

function renderClock() {
  var now = serverNow();
  var health = clockHealth();
  var statusEl = $('sync-status');
  var railEl = $('rail-clock');

  setClass(statusEl, 'ok', health === 'ok');
  setClass(statusEl, 'warn', health === 'warn');
  setClass(statusEl, 'bad', health === 'bad');
  setClass(railEl, 'warn', health === 'warn');
  setClass(railEl, 'bad', health === 'bad');

  if (now === null) {
    setText($('time-main'), '--:--');
    setText($('time-sec'), '');
    setText($('time-ampm'), '');
    setText(railEl, '--:--');
    setText($('date-line'), health === 'bad' ? 'Cannot reach server' : 'Connecting to server…');
    setText($('sync-text'), health === 'bad' ? 'Disconnected' : 'Connecting…');
    return;
  }

  var d = zoned(now);
  var p = hourParts(d);
  var hm = p.h + ':' + pad2(d.getUTCMinutes());
  setText($('time-main'), hm);
  setText($('time-sec'), pad2(d.getUTCSeconds()));
  setText($('time-ampm'), p.ampm);
  setText(railEl, hm + (p.ampm ? ' ' + p.ampm.charAt(0) : ''));
  setText($('date-line'), DAYS[d.getUTCDay()] + ', ' + MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate() +
    ', ' + d.getUTCFullYear());

  var last = formatStamp(state.anchorServerMs);
  if (health === 'ok') {
    setText($('sync-text'), 'Synced');
  } else if (health === 'warn') {
    setText($('sync-text'), state.syncFailures > 0 ? 'Sync failed, retrying · last ' + last : 'Clock stale · last sync ' + last);
  } else {
    setText($('sync-text'), 'Disconnected · last sync ' + last);
  }
}

function tick() {
  var n = Date.now();
  if (state.lastTickLocal !== null) {
    var delta = n - state.lastTickLocal;
    // Local clock jumped or the page was asleep: resync now.
    if (delta < -2000 || delta > 30000) { syncTime(); }
  }
  state.lastTickLocal = n;
  renderClock();
  if (state.diagOpen) { renderDiag(); }
  if (viewActive('calendar')) { renderCalendar(); }
  if (ai.blockedUntil || ai.inFlight || voice.state === 'recording') { aiRender(); }  // countdown on the Ask button
  if (voice.askWhenReady && !ai.inFlight && ai.blockedUntil <= Date.now()) {  // voice question waiting
    voice.askWhenReady = false;
    aiAsk();
  }
  var sn = serverNow();
  setTimeout(tick, sn === null ? 1000 : 1000 - (sn % 1000) + 15);
}

// ---- views ----
// Rail order for Back/Next (wraps around) and the jump grid.
var VIEWS = [
  { id: 'weather', label: 'Weather', icon: 'icons/partly.svg' },
  { id: 'home', label: 'Home', icon: 'icons/home.svg' },
  { id: 'maps', label: 'Maps', icon: 'icons/map.svg' },
  { id: 'cameras', label: 'Cameras', icon: 'icons/camera.svg' },
  { id: 'calendar', label: 'Calendar', icon: 'icons/calendar.svg' },
  { id: 'uptime', label: 'Uptime', icon: 'icons/uptime.svg' },
  { id: 'assistant', label: 'Ask', icon: 'icons/assistant.svg' }
];
var currentView = 0;

function showView(name) {
  for (var k = 0; k < VIEWS.length; k++) {
    if (VIEWS[k].id === name) { currentView = k; }
  }
  var v = VIEWS[currentView];
  $('nav-current-icon').src = v.icon;
  setText($('nav-current-name'), v.label);
  setText($('nav-current-pos'), (currentView + 1) + ' / ' + VIEWS.length);
  var tiles = document.querySelectorAll('#jump .jump-tile');
  for (var i = 0; i < tiles.length; i++) {
    setClass(tiles[i], 'active', tiles[i].getAttribute('data-view') === name);
  }
  var views = document.querySelectorAll('.view');
  for (var j = 0; j < views.length; j++) {
    setClass(views[j], 'active', views[j].id === 'view-' + name);
  }
  if (name === 'assistant') { aiLoadStatus(); aiRender(); aiPaginate(); }
  if (name === 'calendar') { renderCalendar(); pollCalendar(); }
  if (name === 'cameras') { renderCameras(); } else { state.camDetail = null; camEndLive(); }
  mapsSync();
  uptimeSync();
  homeSync();
}

function stepView(delta) {
  showView(VIEWS[(currentView + delta + VIEWS.length) % VIEWS.length].id);
}

// Jump grid and More panel. Taps in the first 300 ms after opening are ignored so the
// finger that opened a panel (or a bounce from the poor touchscreen) can't pick a tile.
var overlayOpenedAt = 0;
function setOverlay(id, open) {
  setClass($(id), 'is-hidden', !open);
  if (open) { overlayOpenedAt = Date.now(); }
}
function overlayTap(fn) {
  return function () {
    if (Date.now() - overlayOpenedAt < 300) { return; }
    fn.call(this);
  };
}
