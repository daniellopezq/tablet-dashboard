/* Tablet dashboard client: maps slideshow (Maps tab) and the small map card (Weather tab).
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

// ---- maps slideshow (Maps tab) and small card (Weather tab) ----
var mapsStage = { a: 'map-img-a', b: 'map-img-b', front: 'a', shown: '', bg: true };
var homeStage = { a: 'hmap-img-a', b: 'hmap-img-b', front: 'a', shown: '', bg: true };

function viewActive(name) {
  return $('view-' + name).className.indexOf('active') >= 0 && !isHidden();
}
function mapsActive() { return viewActive('maps'); }
function homeMapActive() { return viewActive('weather'); }

function clearTimer(name) {
  if (state[name]) { clearTimeout(state[name]); state[name] = null; }
}

// Call whenever the visible view or page visibility may have changed.
function mapsSync() {
  clearTimer('mapsRotateTimer');
  clearTimer('homeRotateTimer');
  if (!mapsActive() && !homeMapActive()) { clearTimer('mapsPollTimer'); return; }
  if (state.maps) {
    if (mapsActive()) { mapsShow(state.mapsIdx < 0 ? 0 : state.mapsIdx); }
    if (homeMapActive()) { homeMapShow(state.homeIdx < 0 ? 0 : state.homeIdx); }
  }
  mapsPoll();
}

function mapsPoll() {
  clearTimer('mapsPollTimer');
  if (!mapsActive() && !homeMapActive()) { return; }
  if (!state.mapsInFlight) {
    state.mapsInFlight = true;
    getJSON('/api/maps', WEATHER_TIMEOUT_MS, function (err, data) {
      state.mapsInFlight = false;
      if (!err && data) {
        state.maps = data;
        if (mapsActive() && !state.mapsRotateTimer) { mapsShow(state.mapsIdx < 0 ? 0 : state.mapsIdx); }
        if (homeMapActive() && !state.homeRotateTimer) { homeMapShow(state.homeIdx < 0 ? 0 : state.homeIdx); }
      } else if (!state.maps) {
        setText($('map-msg'), 'Cannot reach the server.');
      }
    });
  }
  state.mapsPollTimer = setTimeout(mapsPoll, MAPS_POLL_MS);
}

function mapsMessage(text) {
  setText($('map-msg'), text || '');
  setClass($('map-msg'), 'is-hidden', !text);
}

function mapsShow(idx) {
  clearTimer('mapsRotateTimer');
  var m = state.maps;
  if (!m || !mapsActive()) { return; }
  var n = m.layers ? m.layers.length : 0;
  if (!m.configured) {
    mapsMessage('Maps are off. Set weather latitude/longitude and enable maps in config.json.');
    return;
  }
  if (n === 0) {
    mapsMessage(m.status === 'error'
      ? 'Maps unavailable: ' + (m.error || 'error') + '. Retrying automatically.'
      : 'Preparing maps\u2026 the first set takes a minute or two.');
    state.mapsRotateTimer = setTimeout(function () { mapsShow(0); }, 5000);
    return;
  }
  mapsMessage('');
  idx = idx % n;
  state.mapsIdx = idx;
  var layer = m.layers[idx];
  var key = layer.url + '?v=' + layer.updated_ms;
  renderMapChrome(m, idx);
  if (key !== mapsStage.shown) { swapStage(mapsStage, key); }
  state.mapsRotateTimer = setTimeout(function () { mapsShow(idx + 1); }, (m.rotate_seconds || 10) * 1000);
}

function homeMapShow(idx) {
  clearTimer('homeRotateTimer');
  var m = state.maps;
  if (!m || !homeMapActive()) { return; }
  var card = $('wx-map');
  var n = m.layers ? m.layers.length : 0;
  if (!m.configured || n === 0) {
    setClass(card, 'is-hidden', true);
    setClass($('wx-map-attrib'), 'is-hidden', true);
    if (m.configured) {  // maps still being prepared: look again shortly
      state.homeRotateTimer = setTimeout(function () { homeMapShow(0); }, 15000);
    }
    return;
  }
  idx = idx % n;
  state.homeIdx = idx;
  var layer = m.layers[idx];
  var key = layer.small_url + '?v=' + layer.updated_ms;
  setClass(card, 'is-hidden', false);
  setClass($('wx-map-attrib'), 'is-hidden', false);
  setText($('hmap-cap'), layer.title);
  homeMapFit();
  if (key !== homeStage.shown) { swapStage(homeStage, key); }
  state.homeRotateTimer = setTimeout(function () { homeMapShow(idx + 1); }, (m.rotate_seconds || 10) * 1000);
}

// Hide the card when the space right of the clock is too small to show anything.
function homeMapFit() {
  var card = $('wx-map');
  if (card.className.indexOf('is-hidden') >= 0) { return; }
  setClass(card, 'is-tiny', card.offsetWidth < 80 || card.offsetHeight < 40);
}

// Crossfade: load the new image into the hidden element, then swap visibility.
function swapStage(stage, key) {
  var front = $(stage.front === 'a' ? stage.a : stage.b);
  var back = $(stage.front === 'a' ? stage.b : stage.a);
  var done = false;
  back.onload = function () {
    if (done) { return; }
    done = true;
    setClass(back, 'show', true);
    setClass(front, 'show', false);
    stage.front = stage.front === 'a' ? 'b' : 'a';
    stage.shown = key;
  };
  if (stage.bg) {  // div with a background image (scaled with 'cover'; no object-fit on Android 4.4)
    var pre = new Image();
    pre.onload = function () {
      back.style.backgroundImage = 'url("' + key + '")';
      back.onload();
    };
    pre.src = key;
    return;
  }
  back.src = key;
  if (back.complete && back.naturalWidth > 0) { back.onload(); }
}

function renderMapChrome(m, idx) {
  var layer = m.layers[idx];
  setText($('map-title'), layer.title);
  var sub = (m.label ? m.label + ' \u00b7 ' : '') + 'updated ' + formatStamp(layer.updated_ms) +
    (layer.stale ? ' (stale)' : '');
  setText($('map-sub'), sub);
  var dots = '';
  for (var i = 0; i < m.layers.length; i++) { dots += i === idx ? '\u25cf ' : '\u25cb '; }
  setText($('map-dots'), dots);
  var foot = $('map-foot');
  if (!foot.firstChild && m.attribution) {
    for (var j = 0; j < m.attribution.length; j++) {
      var a = document.createElement('a');
      a.className = 'attrib';
      a.href = m.attribution[j].url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.appendChild(document.createTextNode(m.attribution[j].text));
      foot.appendChild(a);
    }
  }
}
