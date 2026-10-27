/* Tablet dashboard client: Home tab: Home Assistant buttons, the Plex/Jellyfin switch, read-only tiles and their warning.
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

// ---- home controls (Home Assistant buttons + Plex/Jellyfin switch; polled while visible, and slowly on other
// tabs while a read-only tile can warn, e.g. the garage left open) ----
var HC_ICONS = { media: 1, fan: 1, plug: 1, light: 1, home: 1, shield: 1, motion: 1, garage: 1 };
var HC_MEDIA = { plex: 'Plex', jellyfin: 'Jellyfin' };

function homeSync() {
  clearTimer('hcTimer');
  if (viewActive('home') || homeWatching()) { pollHome(); }
}

// True until the first answer, then while some tile has warn_minutes.
function homeWatching() {
  if (!state.hc) { return true; }
  var list = state.hc.buttons || [];
  for (var i = 0; i < list.length; i++) { if (list[i].warn_minutes) { return true; } }
  return false;
}

function homeButton(id) {
  var list = state.hc ? state.hc.buttons : [];
  for (var i = 0; i < list.length; i++) { if (list[i].id === id) { return list[i]; } }
  return null;
}

function homeMedia() {
  var list = state.hc ? state.hc.buttons : [];
  for (var i = 0; i < list.length; i++) { if (list[i].kind === 'media_switch') { return list[i].media; } }
  return null;
}

function pollHome(delayMs) {
  clearTimer('hcTimer');
  if (!viewActive('home') && !homeWatching()) { return; }
  if (delayMs) { state.hcTimer = setTimeout(function () { pollHome(); }, delayMs); return; }
  if (!state.hcInFlight) {
    state.hcInFlight = true;
    getJSON('/api/home', WEATHER_TIMEOUT_MS, function (err, data) {
      state.hcInFlight = false;
      if (err || !data || !data.buttons) {
        state.hcError = err ? String(err.message || err) : 'bad response';
      } else {
        state.hcError = null;
        state.hc = data;
      }
      renderHome();
      homeCheckAlert();
      clearTimer('hcTimer');
      var m = homeMedia();
      if (viewActive('home')) {
        state.hcTimer = setTimeout(function () { pollHome(); }, m && m.switching ? HOME_BUSY_POLL_MS : HOME_POLL_MS);
      } else if (homeWatching()) {
        state.hcTimer = setTimeout(function () { pollHome(); }, HOME_WATCH_POLL_MS);
      }
    });
  }
}

function hcMessage(title, body) {
  setClass($('hc-grid'), 'is-hidden', true);
  setClass($('hc-pager'), 'is-hidden', true);
  setClass($('hc-msg'), 'is-hidden', false);
  setText($('hc-msg-title'), title);
  setText($('hc-msg-body'), body || '');
}

// -> { name, word, hint, cls } for one button
function hcDescribe(b) {
  if (b.kind === 'media_switch') {
    var m = b.media || {};
    if (m.switching) {
      return { name: HC_MEDIA[m.switching] || b.label, word: 'WAIT', cls: 'hc-busy',
        hint: 'Starting ' + (HC_MEDIA[m.switching] || '') + '…' };
    }
    var d;
    if (HC_MEDIA[m.current]) {
      var other = m.current === 'plex' ? 'Jellyfin' : 'Plex';
      d = { name: HC_MEDIA[m.current], word: 'ON', cls: 'hc-on', hint: 'Tap to switch to ' + other };
    } else if (m.current === 'both') {
      d = { name: 'Plex + Jellyfin', word: 'BOTH', cls: 'hc-warn', hint: 'Tap to keep Plex only' };
    } else {
      d = { name: b.label, word: 'OFF', cls: 'hc-off', hint: 'Tap to start Plex' };
    }
    if (m.error) { d.hint = m.error; d.cls += ' hc-err'; }
    return d;
  }
  if (b.kind === 'sensor') { return hcSensor(b); }
  var st = b.state;
  if (st === undefined) { return { name: b.label, word: 'RUN', cls: 'hc-off', hint: 'Tap to run' }; }
  if (st === null) { return { name: b.label, word: '?', cls: 'hc-unk', hint: 'HA not connected' }; }
  if (st === 'on') { return { name: b.label, word: 'ON', cls: 'hc-on', hint: b.hint || 'Tap to turn off' }; }
  if (st.indexOf('armed_') === 0) { return { name: b.label, word: 'ARMED', cls: 'hc-on', hint: 'Tap to disarm' }; }
  if (st === 'disarmed') { return { name: b.label, word: 'OFF', cls: 'hc-off', hint: 'Disarmed · tap to arm' }; }
  if (st === 'arming' || st === 'pending') { return { name: b.label, word: 'ARMING', cls: 'hc-busy', hint: 'Please wait' }; }
  if (st === 'triggered') { return { name: b.label, word: 'ALARM', cls: 'hc-warn hc-err', hint: 'Tap to disarm' }; }
  if (st === 'off') { return { name: b.label, word: 'OFF', cls: 'hc-off', hint: 'Tap to turn on' }; }
  if (st === 'unavailable' || st === 'missing' || st === 'unknown') {
    return { name: b.label, word: 'N/A', cls: 'hc-unk', hint: st === 'missing' ? 'Not found in Home Assistant' : 'Device not responding' };
  }
  return { name: b.label, word: String(st).toUpperCase(), cls: 'hc-off', hint: '' };
}

// Read-only tile (kind "sensor", e.g. the garage): its words, since when, amber once it warns.
function hcSensor(b) {
  var st = b.state, w = b.words || { on: 'On', off: 'Off' };
  if (st === null) { return { name: b.label, word: '?', cls: 'hc-unk', hint: 'HA not connected' }; }
  if (st !== 'on' && st !== 'off' && st !== 'open' && st !== 'closed') {
    return { name: b.label, word: 'N/A', cls: 'hc-unk', hint: st === 'missing' ? 'Not found in Home Assistant' : 'Unknown' };
  }
  var on = st === 'on' || st === 'open';
  return { name: b.label, word: (on ? w.on : w.off).toUpperCase(), cls: b.warn ? 'hc-warn' : on ? 'hc-on' : 'hc-off',
    hint: b.since_ms ? 'Since ' + formatHM(b.since_ms) : '' };
}

// Banner on every tab while a sensor tile warns; tapping hides it until the next warning.
function homeCheckAlert() {
  var list = state.hc ? state.hc.buttons : [], b = null;
  for (var i = 0; i < list.length; i++) { if (list[i].warn) { b = list[i]; break; } }
  var el = $('hc-alert');
  if (!b) { setClass(el, 'is-hidden', true); return; }
  var key = b.id + ':' + b.since_ms;
  if (key === state.hcAlertSeen) { return; }
  setText(el, b.label + ': ' + (b.words ? b.words.on : 'On').toLowerCase() + ' since ' + formatHM(b.since_ms) +
    ' (over ' + b.warn_minutes + ' min)');
  setClass(el, 'is-hidden', false);
  el.setAttribute('data-key', key);
}

function hcFillTile(tile, b) {
  while (tile.firstChild) { tile.removeChild(tile.firstChild); }
  tile.setAttribute('data-id', b ? b.id : '');
  tile.disabled = !b;
  if (!b) { tile.className = 'hc-tile col grow hc-empty'; return; }
  var d = hcDescribe(b);
  var busy = !!state.hcBusy[b.id];
  tile.className = 'hc-tile col grow ' + d.cls + (busy ? ' hc-busy' : '');
  // Stacked (icon, state, name, hint) so it fits narrow tiles when the pager is showing.
  var img = el('img', 'hc-icon');
  var icon = b.icon || (b.kind === 'media_switch' ? 'media' : 'home');
  img.src = 'icons/' + (HC_ICONS[icon] ? icon : 'home') + '.svg';
  img.alt = '';
  tile.appendChild(img);
  tile.appendChild(el('span', 'hc-word', busy ? '…' : d.word));
  tile.appendChild(el('span', 'hc-name grow', d.name));
  tile.appendChild(el('span', 'hc-hint', d.hint));
}

function renderHome() {
  var d = state.hc;
  if (!d) {
    hcMessage(state.hcError ? 'Home controls unavailable' : 'Loading…',
      state.hcError ? state.hcError + '. Retrying automatically.' : '');
    setText($('hc-sub'), '');
    return;
  }
  if (!d.enabled || !d.buttons.length) {
    hcMessage('No home controls', d.enabled ? 'Add buttons under "home" in config.json.' :
      'Home controls are turned off in config.json.');
    setText($('hc-sub'), '');
    return;
  }
  setClass($('hc-grid'), 'is-hidden', false);
  setClass($('hc-msg'), 'is-hidden', true);
  var list = d.buttons;
  var pages = Math.ceil(list.length / HOME_PER_PAGE);
  state.hcPage = Math.max(0, Math.min(state.hcPage || 0, pages - 1));
  setClass($('hc-pager'), 'is-hidden', pages <= 1);
  for (var i = 0; i < HOME_PER_PAGE; i++) {
    hcFillTile($('hc-tile-' + i), list[state.hcPage * HOME_PER_PAGE + i] || null);
  }
  setText($('hc-page'), (state.hcPage + 1) + ' / ' + pages);
  $('hc-prev').disabled = state.hcPage <= 0;
  $('hc-next').disabled = state.hcPage >= pages - 1;
  var parts = [], on = 0;
  var m = homeMedia();
  if (m) { parts.push(m.switching ? 'switching media server' : HC_MEDIA[m.current] ? HC_MEDIA[m.current] + ' running' :
    m.current === 'both' ? 'Plex + Jellyfin both running' : 'no media server'); }
  for (var k = 0; k < list.length; k++) { if (list[k].state === 'on' && list[k].kind !== 'sensor') { on++; } }
  if (d.ha_ok === false) { parts.push(d.ha_error || 'Home Assistant not connected'); } else if (on) { parts.push(on + ' on'); }
  if (state.hcError) { parts.push('offline, showing last data'); }
  setText($('hc-sub'), parts.join(' · '));
  setClass($('hc-sub'), 'up-sub-bad', d.ha_ok === false);
}

function homePress(id) {
  var b = homeButton(id);
  if (!b || state.hcBusy[id]) { return; }
  if (b.kind === 'sensor') { showToast(b.label + ' only shows its state'); return; }
  if (b.kind === 'media_switch' && b.media && b.media.switching) { showToast('Already switching, please wait'); return; }
  if (!b.confirm) { homeSend(id); return; }
  var title = 'Run ' + b.label + '?', body = '';
  if (b.state === 'disarmed') { title = 'Arm ' + b.label + '?'; body = 'Cameras will record motion and send alerts.'; }
  if (b.state && (b.state.indexOf('armed_') === 0 || b.state === 'triggered')) {
    title = 'Disarm ' + b.label + '?'; body = 'Cameras will stop recording motion.';
  }
  if (b.kind === 'media_switch') {
    var cur = b.media ? b.media.current : '';
    var to = cur === 'plex' ? 'Jellyfin' : 'Plex', from = cur === 'plex' ? 'Plex' : cur === 'jellyfin' || cur === 'both' ? 'Jellyfin' : '';
    title = 'Switch to ' + to + '?';
    body = (from ? from + ' will stop, and anyone watching on it will be cut off. ' : '') +
      to + ' takes up to a minute to start.';
  }
  setText($('hc-confirm-title'), title);
  setText($('hc-confirm-body'), body);
  setText($('hc-yes'), b.kind === 'media_switch' ? 'Yes, switch' : 'Yes');
  state.hcConfirmId = id;
  setOverlay('hc-confirm', true);
}

function homeSend(id) {
  var b = homeButton(id);
  state.hcBusy[id] = true;
  renderHome();
  aiRequest('POST', '/api/home/action', { id: id }, 15000, function (err, data) {
    delete state.hcBusy[id];
    if (err || !data || !data.ok) {
      showToast(data && data.error && data.error.message ? data.error.message :
        err === 'timeout' ? 'No answer from the server' : 'Could not reach the server');
    } else if (data.pending && b && b.media) {
      b.media.switching = data.target;  // shown at once; polling picks up the result
      b.media.error = null;
    } else if (b && (b.state === 'on' || b.state === 'off')) {
      b.state = b.state === 'on' ? 'off' : 'on';  // optimistic; HA confirms on the next poll
    }
    renderHome();
    pollHome(1500);
  });
}
