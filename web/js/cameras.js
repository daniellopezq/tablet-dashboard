/* Tablet dashboard client: Cameras tab, live view, the motion pop-up and the laundry banner.
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

// ---- cameras (Blink snapshots, live view of RTSP cameras) and the motion pop-up ----
function camerasSync() {
  clearTimer('camTimer');
  if (!isHidden()) { pollCameras(); }
}

function pollCameras() {
  clearTimer('camTimer');
  if (isHidden()) { return; }
  if (!state.camInFlight) {
    state.camInFlight = true;
    getJSON('/api/cameras', WEATHER_TIMEOUT_MS, function (err, data) {
      state.camInFlight = false;
      if (err || !data || !data.cameras) {
        state.camError = err ? String(err.message || err) : 'bad response';
      } else {
        state.camError = null;
        state.cam = data;
        camCheckAlert();
        laundryCheckAlert();
      }
      if (viewActive('cameras')) { renderCameras(); }
    });
  }
  state.camTimer = setTimeout(pollCameras, CAM_POLL_MS);
}

function camById(id) {
  var list = state.cam ? state.cam.cameras : [];
  for (var i = 0; i < list.length; i++) { if (list[i].id === id) { return list[i]; } }
  return null;
}

function camAgo(ms) {
  if (!ms) { return ''; }
  var now = serverNow();
  var min = Math.round(((now === null ? Date.now() : now) - ms) / 60000);
  if (min < 1) { return 'just now'; }
  if (min < 60) { return min + ' min ago'; }
  if (min < 1440) { return Math.floor(min / 60) + ' h ago'; }
  return Math.floor(min / 1440) + ' d ago';
}

// Badges for a camera: [text, class] pairs, most important first.
function camBadges(c) {
  var out = [];
  if (!c.available) { out.push(['OFFLINE', 'cam-b-bad']); }
  if (c.live && c.available) { out.push(['LIVE', 'cam-b-live']); }
  if (c.motion) { out.push(['MOTION', 'cam-b-bad']); }
  if (c.battery_low) { out.push(['BATTERY LOW', 'cam-b-warn']); }
  if (c.motion_detection === false) { out.push(['MOTION OFF', 'cam-b-dim']); }
  return out;
}

function camImageUrl(c) {
  return '/api/cameras/' + c.id + '.jpg?v=' + encodeURIComponent(c.version || '');
}

function camSetImage(img, c) {
  camSetUrl(img, c && c.available !== false ? camImageUrl(c) : '');
}

function camSetUrl(img, url) {
  if (img.getAttribute('data-url') === url) { return; }  // unchanged: no reload, no flicker
  img.setAttribute('data-url', url);
  // A blank picture (not a removed src) is what makes the browser drop a live stream's connection.
  img.src = url || CAM_BLANK;
}

// Live view (MJPEG in the big <img>): started by opening an RTSP camera, ended by leaving it, by
// hiding the page, or after the server's live_max_seconds; then "Resume live" starts a new one.
function camStartLive(id) {
  clearTimer('camLiveTimer');
  state.camLiveId = id;
  state.camLiveToken++;
  state.camLiveEnded = false;
  var secs = (state.cam && state.cam.live_max_seconds) || 600;
  state.camLiveTimer = setTimeout(function () {
    state.camLiveTimer = null;
    state.camLiveEnded = true;
    camSetUrl($('cam-big-img'), '');
    renderCameras();
  }, secs * 1000);
}

// Leaving a camera (or hiding the page) also forgets an on-demand Blink live view, so it isn't
// started again by itself: each start is a Blink cloud session.
function camEndLive() {
  state.camBlinkLive = null;
  camStopLive();
}

function camStopLive() {
  clearTimer('camLiveTimer');
  if (state.camLiveId === null) { return; }
  state.camLiveId = null;
  camSetUrl($('cam-big-img'), '');
}

function camMessage(title, body) {
  camStopLive();
  setClass($('cam-grid'), 'is-hidden', true);
  setClass($('cam-detail'), 'is-hidden', true);
  setClass($('cam-msg'), 'is-hidden', false);
  setText($('cam-msg-title'), title);
  setText($('cam-msg-body'), body || '');
}

function renderCameras() {
  var d = state.cam;
  if (!d) {
    camMessage(state.camError ? 'Cameras unavailable' : 'Loading cameras…',
      state.camError ? state.camError + '. Retrying automatically.' : '');
    setText($('cam-sub'), '');
    return;
  }
  if (!d.enabled || !d.cameras.length) {
    camMessage('No cameras', 'Add cameras under "cameras" in config.json.');
    setText($('cam-sub'), '');
    return;
  }
  var live = [];
  for (var n = 0; n < d.cameras.length; n++) { if (d.cameras[n].live) { live.push(d.cameras[n].label); } }
  if (d.ha_ok === false && !live.length) {
    camMessage('Home Assistant not connected', d.ha_error || 'Check that Home Assistant is running.');
    setText($('cam-sub'), '');
    return;
  }
  setClass($('cam-msg'), 'is-hidden', true);
  var moving = [];
  for (var k = 0; k < d.cameras.length; k++) { if (d.cameras[k].motion) { moving.push(d.cameras[k].label); } }
  setText($('cam-sub'), (moving.length ? 'Motion: ' + moving.join(', ') : 'No motion') +
    (live.length ? ' · live: ' + live.join(', ') : ' · snapshots, no live view') +
    (d.ha_ok === false ? ' · Home Assistant: ' + (d.ha_error || 'not connected') : '') +
    (state.camError ? ' · offline, showing last data' : ''));
  setClass($('cam-sub'), 'up-sub-bad', moving.length > 0);
  var detail = state.camDetail ? camById(state.camDetail) : null;
  setClass($('cam-grid'), 'is-hidden', !!detail);
  setClass($('cam-detail'), 'is-hidden', !detail);
  if (detail) { camRenderDetail(detail); return; }
  camStopLive();
  var slots = d.cameras.length > 4 ? CAM_SLOTS_6 : CAM_SLOTS_4;
  setClass($('cam-grid'), 'cam-grid-3', slots === CAM_SLOTS_6);
  for (var i = 0; i < CAM_COUNT; i++) {
    var slot = i < slots.length ? slots[i] : (i === 4 ? 4 : 5);
    var c = i < slots.length ? d.cameras[i] || null : null;
    var card = $('cam-card-' + slot);
    card.className = 'cam-card grow' + (i >= slots.length ? ' is-hidden' : '') +
      (c ? (c.motion ? ' cam-motion' : '') : ' cam-empty');
    card.setAttribute('data-id', c ? c.id : '');
    card.disabled = !c;
    camSetImage($('cam-img-' + slot), c);
    var cap = $('cam-cap-' + slot);
    while (cap.firstChild) { cap.removeChild(cap.firstChild); }
    if (!c) { continue; }
    cap.appendChild(el('span', 'cam-name grow', c.label));
    var badges = camBadges(c);
    if (badges.length) { cap.appendChild(el('span', 'cam-badge ' + badges[0][1], badges[0][0])); }
    cap.appendChild(el('span', 'cam-time', camAgo(c.updated_ms)));
  }
}

function camRenderDetail(c) {
  var info = $('cam-d-info');
  while (info.firstChild) { info.removeChild(info.firstChild); }
  setText($('cam-d-name'), c.label);
  if (c.live || state.camBlinkLive === c.id) {
    if (state.camLiveId !== c.id) { camStopLive(); camStartLive(c.id); }
    camSetUrl($('cam-big-img'), state.camLiveEnded ? '' :
      '/api/cameras/' + c.id + '/live.mjpg?t=' + state.camLiveToken + (state.camHd && c.hd ? '&q=hd' : ''));
    camRenderControls(c);
    info.appendChild(el('div', 'cam-d-line', state.camLiveEnded ? 'Live view paused' :
      (c.available ? 'Live' : 'Camera not answering')));
    if (!c.available && c.error) { info.appendChild(el('div', 'cam-d-line', c.error)); }
    if (c.live_error) { info.appendChild(el('div', 'cam-d-line', c.live_error)); }
    setText($('cam-snap'), state.camLiveEnded || !c.available ? 'Resume live' : 'Restart live');
    $('cam-snap').disabled = false;
    return;
  }
  camStopLive();
  camRenderControls(c);
  setText($('cam-snap'), 'New snapshot');
  camSetImage($('cam-big-img'), c);
  info.appendChild(el('div', 'cam-d-line', c.updated_ms ? 'Picture ' + formatHM(c.updated_ms) + ' (' + camAgo(c.updated_ms) + ')' : 'No picture yet'));
  if (c.motion_ms) { info.appendChild(el('div', 'cam-d-line', 'Motion ' + camAgo(c.motion_ms))); }
  var badges = camBadges(c);
  for (var i = 0; i < badges.length; i++) { info.appendChild(el('span', 'cam-badge ' + badges[i][1], badges[i][0])); }
  $('cam-snap').disabled = !c.can_snapshot || !c.available;
}

// Pan/tilt arrows, HD/SD and day/night for live cameras that have them; null hides them all.
var CAM_MODE_NEXT = { auto: 'night', night: 'day', day: 'auto' };
var CAM_MODE_TEXT = { auto: 'Auto', night: 'Night', day: 'Day' };

function camRenderControls(c) {
  var rtsp = !!(c && c.live);  // pan/tilt, HD and day/night are for local live cameras
  var dirs = ['up', 'down', 'left', 'right'];
  for (var i = 0; i < dirs.length; i++) {
    var b = $('cam-ptz-' + dirs[i]);
    setClass(b, 'is-hidden', !(rtsp && c.ptz));
    b.disabled = state.camMoving;
  }
  setClass($('cam-ctl'), 'is-hidden', !(c && ((rtsp && (c.hd || c.day_night)) || c.can_live)));
  if (!c) { return; }
  setClass($('cam-live'), 'is-hidden', !c.can_live);
  setText($('cam-live'), state.camBlinkLive === c.id ? 'Stop' : 'Live');
  setClass($('cam-hd'), 'is-hidden', !(rtsp && c.hd));
  setText($('cam-hd'), state.camHd ? 'HD' : 'SD');
  setClass($('cam-mode'), 'is-hidden', !(rtsp && c.day_night));
  if (rtsp && c.day_night && state.camModeFor !== c.id) {
    state.camModeFor = c.id;
    state.camMode = null;
    aiRequest('GET', '/api/cameras/' + c.id + '/controls', null, 8000, function (err, data) {
      if (!err && data && state.camModeFor === c.id) { state.camMode = data.mode || null; renderCameras(); }
    });
  }
  setText($('cam-mode'), CAM_MODE_TEXT[state.camMode] || '…');
}

function camMove(dir) {
  var id = state.camDetail;
  if (!id || state.camMoving) { return; }
  state.camMoving = true;
  renderCameras();
  aiRequest('POST', '/api/cameras/control', { id: id, move: dir }, 10000, function (err, data) {
    state.camMoving = false;
    if (err || !data || !data.ok) {
      showToast(data && data.error && data.error.message ? data.error.message : 'Could not reach the server');
    }
    renderCameras();
  });
}

function camToggleBlinkLive() {
  var id = state.camDetail;
  if (!id) { return; }
  if (state.camBlinkLive === id) {
    camEndLive();
  } else {
    state.camBlinkLive = id;
    showToast('Starting live view; Blink takes a few seconds');
  }
  renderCameras();
}

function camToggleHd() {
  state.camHd = !state.camHd;
  renderCameras();
}

function camNextMode() {
  var id = state.camDetail;
  if (!id) { return; }
  var mode = CAM_MODE_NEXT[state.camMode] || 'auto';
  $('cam-mode').disabled = true;
  aiRequest('POST', '/api/cameras/control', { id: id, mode: mode }, 10000, function (err, data) {
    $('cam-mode').disabled = false;
    if (err || !data || !data.ok) {
      showToast(data && data.error && data.error.message ? data.error.message : 'Could not reach the server');
    } else {
      state.camMode = data.mode;
      state.camModeFor = id;
      showToast(mode === 'night' ? 'Night mode: infrared, black and white' :
        mode === 'day' ? 'Day mode: colour' : 'Auto: the camera picks day or night');
    }
    renderCameras();
  });
}

function camOpen(id) {
  state.camDetail = id;
  state.camBlinkLive = null;
  renderCameras();
}

function camSnapshot() {
  var id = state.camDetail;
  if (!id) { return; }
  var cam = camById(id);
  if (cam && (cam.live || state.camBlinkLive === id)) { camStartLive(id); renderCameras(); return; }
  $('cam-snap').disabled = true;
  aiRequest('POST', '/api/cameras/snapshot', { id: id }, 15000, function (err, data) {
    if (err || !data || !data.ok) {
      showToast(data && data.error && data.error.message ? data.error.message : 'Could not reach the server');
    } else {
      showToast('Snapshot requested; the picture updates in about 15 s');
      setTimeout(pollCameras, 8000);
      setTimeout(pollCameras, 18000);
    }
    renderCameras();
  });
}

// A new motion alert jumps to that camera for popup_seconds, then returns to the previous tab
// unless someone touched the screen meanwhile.
function camCheckAlert() {
  var a = state.cam && state.cam.alert;
  if (!a || a.key === state.camAlertSeen || !camById(a.id)) { return; }
  state.camAlertSeen = a.key;
  if (state.camReturnView === null) { state.camReturnView = VIEWS[currentView].id; }
  state.camTouched = false;
  setOverlay('jump', false);
  setOverlay('more', false);
  showView('cameras');
  camOpen(a.id);
  clearTimer('camPopupTimer');
  state.camPopupTimer = setTimeout(function () {
    state.camPopupTimer = null;
    var back = state.camReturnView;
    state.camReturnView = null;
    if (!state.camTouched && back && back !== 'cameras') { showView(back); }
  }, Math.max(10, state.cam.popup_seconds || 60) * 1000);
}

// Laundry warning (laundry.py): a banner until tapped, spoken once when speech is on and the tablet has
// had a tap since loading (Android Chrome blocks sound before that).
function laundryCheckAlert() {
  var a = state.cam && state.cam.laundry && state.cam.laundry.alert;
  var el = $('laundry-banner');
  if (!a) { setClass(el, 'is-hidden', true); return; }
  if (a.key === state.laundrySeen) { return; }
  state.laundrySeen = a.key;
  setText(el, a.text);
  setClass(el, 'is-hidden', false);
  if (a.speech_id && ai.audio && ai.speak) { aiPlaySpeech(a.speech_id); }
}
