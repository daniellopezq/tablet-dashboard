/* Tablet dashboard client: Uptime tab.
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

// ---- uptime (Kuma monitors, then home services; polled only while the tab is visible) ----
var UP_WORDS = { ok: 'UP', fail: 'DOWN', warn: 'WAIT', unknown: '?' };

function uptimeSync() {
  clearTimer('upTimer');
  if (viewActive('uptime')) { pollUptime(); }
}

function pollUptime() {
  clearTimer('upTimer');
  if (!viewActive('uptime')) { return; }
  if (!state.upInFlight) {
    state.upInFlight = true;
    getJSON('/api/uptime', WEATHER_TIMEOUT_MS, function (err, data) {
      state.upInFlight = false;
      if (err || !data || !data.checks) {
        state.upError = err ? String(err.message || err) : 'bad response';
      } else {
        state.upError = null;
        state.up = data;
      }
      renderUptime();
    });
  }
  state.upTimer = setTimeout(pollUptime, UPTIME_POLL_MS);
}

function upMessage(title, body) {
  setClass($('up-grid'), 'is-hidden', true);
  setClass($('up-pager'), 'is-hidden', true);
  setClass($('up-msg'), 'is-hidden', false);
  setText($('up-msg-title'), title);
  setText($('up-msg-body'), body || '');
}

function upDuration(minutes) {
  return minutes < 60 ? minutes + ' min' : Math.floor(minutes / 60) + ' h';
}

function upFillCard(card, c) {
  while (card.firstChild) { card.removeChild(card.firstChild); }
  card.className = 'up-card col grow' + (c ? ' up-' + c.state : ' up-empty');
  if (!c) { return; }
  var head = el('div', 'up-head row');
  head.appendChild(el('span', 'up-dot'));
  head.appendChild(el('span', 'up-name grow', c.name));
  card.appendChild(head);
  var mid = el('div', 'up-mid row grow');
  mid.appendChild(el('span', 'up-word', UP_WORDS[c.state] || '?'));
  var parts = [];
  if (c.ping_ms !== null && c.ping_ms !== undefined) { parts.push(c.ping_ms + ' ms'); }
  if (c.uptime_pct !== null && c.uptime_pct !== undefined) {
    parts.push(c.uptime_pct + '% ' + (c.history_minutes < 1440 ? upDuration(c.history_minutes) : '24 h'));
  }
  mid.appendChild(el('span', 'up-stats grow', parts.join(' · ')));
  card.appendChild(mid);
  // last 30 checks, newest on the right
  var strip = el('div', 'up-strip row');
  var recent = c.recent || '';
  for (var i = 0; i < 30; i++) {
    var k = recent.length - 30 + i;
    strip.appendChild(el('span', 'up-tick grow' + (k >= 0 ? ' h-' + recent.charAt(k) : '')));
  }
  card.appendChild(strip);
}

function renderUptime() {
  var d = state.up;
  if (!d) {
    upMessage(state.upError ? 'Uptime unavailable' : 'Loading uptime…',
      state.upError ? state.upError + '. Retrying automatically.' : '');
    setText($('up-sub'), '');
    return;
  }
  var checks = d.checks;
  if (!checks.length) {
    upMessage('No uptime checks', d.enabled ? 'Waiting for the first check run.' :
      'Health checks are turned off on the admin page.');
    setText($('up-sub'), '');
    return;
  }
  setClass($('up-grid'), 'is-hidden', false);
  setClass($('up-pager'), 'is-hidden', false);
  setClass($('up-msg'), 'is-hidden', true);
  var pages = Math.ceil(checks.length / UPTIME_PER_PAGE);
  state.upPage = Math.max(0, Math.min(state.upPage || 0, pages - 1));
  for (var i = 0; i < UPTIME_PER_PAGE; i++) {
    upFillCard($('up-card-' + i), checks[state.upPage * UPTIME_PER_PAGE + i] || null);
  }
  setText($('up-page'), (state.upPage + 1) + ' / ' + pages);
  $('up-prev').disabled = state.upPage <= 0;
  $('up-next').disabled = state.upPage >= pages - 1;
  var down = d.counts.fail, other = d.counts.warn + d.counts.unknown;
  setText($('up-sub'), d.counts.ok + ' up · ' + down + ' down' + (other ? ' · ' + other + ' other' : '') +
    (d.last_run_ms ? ' · checked ' + formatHM(d.last_run_ms) : '') +
    (state.upError ? ' · offline, showing last data' : ''));
  setClass($('up-sub'), 'up-sub-bad', down > 0);
}
