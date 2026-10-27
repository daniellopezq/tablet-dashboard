/* Tablet dashboard client: 12/24 hour, fullscreen, diagnostics, visibility, and startup (loaded last).
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

// ---- 12/24 hour ----
function applyHourButton() {
  setText($('btn-hours'), state.use24h ? '24h' : '12h');
}

function toggleHours() {
  state.use24h = !state.use24h;
  state.hourPrefStored = true;
  storageSet(STORAGE_KEY_24H, state.use24h ? '1' : '0');
  applyHourButton();
  renderClock();
  renderWeather();
  renderCalendar();
  renderUptime();
}

// ---- fullscreen (only offered when an API is present) ----
function fullscreenRequest() {
  var el = document.documentElement;
  return el.requestFullscreen || el.webkitRequestFullscreen || el.webkitRequestFullScreen || null;
}

function fullscreenApiName() {
  var el = document.documentElement;
  if (el.requestFullscreen) { return 'standard'; }
  if (el.webkitRequestFullscreen || el.webkitRequestFullScreen) { return 'webkit'; }
  return 'none';
}

function fullscreenEnabled() {
  if (!fullscreenRequest()) { return false; }
  var enabled = document.fullscreenEnabled;
  if (enabled === undefined) { enabled = document.webkitFullscreenEnabled; }
  return enabled !== false;
}

function isFullscreen() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement ||
    document.webkitCurrentFullScreenElement || document.webkitIsFullScreen);
}

function updateFullscreenButton() {
  var btn = $('btn-fullscreen');
  setClass(btn, 'unsupported', !fullscreenEnabled());
  setText(btn, isFullscreen() ? 'Exit full' : 'Fullscreen');
}

function toggleFullscreen() {
  if (!fullscreenEnabled()) {
    showToast('This browser does not support fullscreen. Use the browser\'s own fullscreen or kiosk option.');
    return;
  }
  try {
    if (isFullscreen()) {
      var exit = document.exitFullscreen || document.webkitExitFullscreen || document.webkitCancelFullScreen;
      if (exit) { exit.call(document); }
    } else {
      fullscreenRequest().call(document.documentElement);
    }
  } catch (e) {
    showToast('Fullscreen request was refused by the browser.');
  }
  // Some WebViews never fire a change event; refresh the label regardless.
  setTimeout(updateFullscreenButton, 600);
}

var toastTimer = null;
function showToast(text) {
  var el = $('toast');
  setText(el, text);
  setClass(el, 'is-hidden', false);
  if (toastTimer) { clearTimeout(toastTimer); }
  toastTimer = setTimeout(function () { setClass(el, 'is-hidden', true); }, 5000);
}

// ---- diagnostics ----
function diagRows() {
  var de = document.documentElement;
  var sn = serverNow();
  var rows = [
    ['User agent', navigator.userAgent],
    ['Viewport (inner)', window.innerWidth + ' × ' + window.innerHeight],
    ['Viewport (document)', de.clientWidth + ' × ' + de.clientHeight],
    ['Screen', screen.width + ' × ' + screen.height +
      ' (avail ' + screen.availWidth + ' × ' + screen.availHeight + ')'],
    ['devicePixelRatio', String(window.devicePixelRatio || 'n/a')],
    ['Orientation', window.orientation !== undefined ? String(window.orientation) : 'n/a'],
    ['Server time', sn === null ? 'not synced' : formatHM(sn) + ':' + pad2(zoned(sn).getUTCSeconds())],
    ['Timezone', (state.tzName || '--') + ' ' + (state.tzAbbr || '') + ' (UTC offset ' + state.offsetMin + ' min)'],
    ['Last successful sync', state.lastSyncOkLocal === null ? 'never' :
      formatStamp(state.anchorServerMs) + ' (' + Math.round((Date.now() - state.lastSyncOkLocal) / 1000) + ' s ago)'],
    ['Sync round trip', state.lastRttMs === null ? 'n/a' : state.lastRttMs + ' ms'],
    ['Sync failures', state.syncFailures + (state.lastSyncError ? ' (' + state.lastSyncError + ')' : '')],
    ['Tablet clock offset', sn === null ? 'n/a' : Math.round((Date.now() - sn) / 1000) + ' s vs server'],
    ['Weather status', state.weather ? state.weather.status + (state.weather.stale ? ' (stale)' : '') : 'not loaded'],
    ['Weather updated', state.weather && state.weather.updated_ms ? formatStamp(state.weather.updated_ms) : 'never'],
    ['Weather error', state.weather && state.weather.error ? state.weather.error : 'none'],
    ['Calendar', state.cal ? state.cal.status + (state.cal.stale ? ' (stale)' : '') +
      (state.cal.problem ? ' · ' + state.cal.problem : '') : 'not loaded'],
    ['Fullscreen API', fullscreenApiName()],
    ['Assistant', ai.configured === null ? 'not checked' : (!ai.enabled ? 'off' : (ai.configured ? 'ready' : 'not set up'))],
    ['localStorage', storageWorks() ? 'available' : 'unavailable'],
    ['Page uptime', Math.round((Date.now() - state.loadedLocal) / 60000) + ' min'],
    ['Server version', state.serverVersion || '--']
  ];
  return rows;
}

function renderDiag() {
  var body = $('diag-body');
  var rows = diagRows();
  while (body.rows.length < rows.length) {
    var tr = body.insertRow(-1);
    tr.appendChild(document.createElement('th'));
    tr.appendChild(document.createElement('td'));
  }
  for (var i = 0; i < rows.length; i++) {
    setText(body.rows[i].cells[0], rows[i][0]);
    setText(body.rows[i].cells[1], rows[i][1]);
  }
}

function setDiag(open) {
  state.diagOpen = open;
  setClass($('diag'), 'is-hidden', !open);
  if (open) { renderDiag(); }
}

// ---- visibility ----
function isHidden() {
  if (typeof document.hidden === 'boolean') { return document.hidden; }
  if (typeof document.webkitHidden === 'boolean') { return document.webkitHidden; }
  return false;
}

function onBecameVisible() {
  if (isHidden()) { return; }
  syncTime();
  pollWeather();
  pollCalendar();
  mapsSync();
  uptimeSync();
  homeSync();
  camerasSync();
}

// ---- startup ----
function loadConfig() {
  getJSON('/api/config', SYNC_TIMEOUT_MS, function (err, data) {
    if (err || !data) { setTimeout(loadConfig, SYNC_RETRY_MS); return; }
    state.serverVersion = data.version || '';
    state.httpsPort = data.https_port || null;
    if (state.httpsPort) {
      var link = $('diag-https-link');
      link.href = 'https://' + window.location.hostname + ':' + state.httpsPort + '/';
      setText(link, link.href);
    }
    var tag = document.getElementById('offline-tag');
    if (tag) {
      var sim = data.offline_test || [];
      setClass(tag, 'show', sim.length > 0);
      tag.title = sim.length ? 'Simulated offline: ' + sim.join(', ') : '';
    }
    setTimeout(loadConfig, 60000);  // re-read once a minute (offline test switch)
    if (!state.hourPrefStored && typeof data.clock_default_24h === 'boolean') {
      state.use24h = data.clock_default_24h;
      applyHourButton();
      renderClock();
      renderCalendar();
    }
  });
}

function init() {
  var stored = storageGet(STORAGE_KEY_24H);
  if (stored === '1' || stored === '0') {
    state.use24h = stored === '1';
    state.hourPrefStored = true;
  }
  applyHourButton();

  $('nav-back').addEventListener('click', function () { stepView(-1); }, false);
  $('nav-next').addEventListener('click', function () { stepView(1); }, false);
  $('nav-current').addEventListener('click', function () { setOverlay('jump', true); }, false);
  $('btn-jump-close').addEventListener('click', function () { setOverlay('jump', false); }, false);
  var tiles = document.querySelectorAll('#jump .jump-tile');
  for (var i = 0; i < tiles.length; i++) {
    tiles[i].addEventListener('click', overlayTap(function () {
      setOverlay('jump', false);
      showView(this.getAttribute('data-view'));
    }), false);
  }
  $('btn-more').addEventListener('click', function () { setOverlay('more', true); }, false);
  $('btn-more-close').addEventListener('click', function () { setOverlay('more', false); }, false);
  $('btn-hours').addEventListener('click', overlayTap(toggleHours), false);
  $('ai-ask').addEventListener('click', function () { aiUnlockAudio(); aiAsk(); }, false);
  $('ai-speak').addEventListener('click', aiToggleSpeak, false);
  $('ai-clear').addEventListener('click', aiClear, false);
  $('ai-voice').addEventListener('click', voiceClick, false);
  $('ai-voice-file').addEventListener('change', voiceFileChosen, false);
  for (var t = 0; t < HOME_PER_PAGE; t++) {
    $('hc-tile-' + t).addEventListener('click', function () { homePress(this.getAttribute('data-id')); }, false);
  }
  for (var c = 0; c < CAM_COUNT; c++) {  // card ids 0-5; data-id says which camera
    $('cam-card-' + c).addEventListener('click', function () { camOpen(this.getAttribute('data-id')); }, false);
  }
  $('cam-back').addEventListener('click', function () { state.camDetail = null; renderCameras(); }, false);
  $('cam-snap').addEventListener('click', camSnapshot, false);
  var ptzDirs = ['up', 'down', 'left', 'right'];
  for (var p = 0; p < ptzDirs.length; p++) {
    $('cam-ptz-' + ptzDirs[p]).addEventListener('click', function () { camMove(this.getAttribute('data-dir')); }, false);
  }
  $('cam-hd').addEventListener('click', camToggleHd, false);
  $('cam-live').addEventListener('click', camToggleBlinkLive, false);
  $('cam-mode').addEventListener('click', camNextMode, false);
  // Any touch during a motion pop-up keeps the tablet where the person left it.
  document.addEventListener('click', function () { state.camTouched = true; state.camReturnView = null; }, true);
  $('hc-prev').addEventListener('click', function () { state.hcPage--; renderHome(); }, false);
  $('hc-next').addEventListener('click', function () { state.hcPage++; renderHome(); }, false);
  $('hc-yes').addEventListener('click', overlayTap(function () {
    setOverlay('hc-confirm', false);
    if (ai.confirmToken) { aiConfirmSend(); return; }
    if (state.hcConfirmId) { homeSend(state.hcConfirmId); }
    state.hcConfirmId = null;
  }), false);
  $('hc-no').addEventListener('click', overlayTap(function () {
    setOverlay('hc-confirm', false);
    ai.confirmToken = null;
    state.hcConfirmId = null;
  }), false);
  $('up-prev').addEventListener('click', function () { state.upPage--; renderUptime(); }, false);
  $('up-next').addEventListener('click', function () { state.upPage++; renderUptime(); }, false);
  $('ai-prev').addEventListener('click', function () { aiShowPage(ai.page - 1); }, false);
  $('ai-next').addEventListener('click', function () { aiShowPage(ai.page + 1); }, false);
  window.addEventListener('resize', aiPaginate, false);
  $('ai-input').addEventListener('input', aiRender, false);
  $('ai-input').addEventListener('keyup', aiRender, false);
  aiRender();
  $('btn-diag').addEventListener('click', overlayTap(function () { setOverlay('more', false); setDiag(true); }), false);
  $('btn-diag-close').addEventListener('click', function () { setDiag(false); }, false);
  $('btn-fullscreen').addEventListener('click', overlayTap(toggleFullscreen), false);
  $('toast').addEventListener('click', function () { setClass($('toast'), 'is-hidden', true); }, false);
  $('laundry-banner').addEventListener('click', function () { setClass($('laundry-banner'), 'is-hidden', true); }, false);
  $('hc-alert').addEventListener('click', function () {
    state.hcAlertSeen = this.getAttribute('data-key');
    setClass(this, 'is-hidden', true);
  }, false);
  document.addEventListener('fullscreenchange', updateFullscreenButton, false);
  document.addEventListener('webkitfullscreenchange', updateFullscreenButton, false);
  updateFullscreenButton();

  document.addEventListener('visibilitychange', onBecameVisible, false);
  // A hidden page (screen off, other app) must not keep a camera stream open.
  document.addEventListener('visibilitychange', function () { if (isHidden()) { camEndLive(); } }, false);
  document.addEventListener('webkitvisibilitychange', function () { if (isHidden()) { camEndLive(); } }, false);
  document.addEventListener('webkitvisibilitychange', onBecameVisible, false);
  window.addEventListener('pageshow', onBecameVisible, false);
  window.addEventListener('focus', onBecameVisible, false);

  var hash = String(window.location.hash || '').replace('#', '');
  if (hash === 'weather' || hash === 'home' || hash === 'maps' || hash === 'cameras' || hash === 'calendar' ||
      hash === 'uptime' || hash === 'assistant') {
    showView(hash);  // lets a bookmark open a given tab, e.g. /#assistant
  }

  loadConfig();
  syncTime();
  pollWeather();
  renderWeather();
  pollCalendar();
  mapsSync();
  camerasSync();
  $('wx-map').addEventListener('click', function () { showView('maps'); }, false);
  window.addEventListener('resize', homeMapFit, false);
  tick();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, false);
} else {
  init();
}
