/* Tablet dashboard client: Calendar tab (the server caches Google Calendar; this only reads /api/calendar).
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

// ---- calendar (server caches Google Calendar; we only read /api/calendar) ----
function scheduleCalendar(delay) {
  if (state.calTimer) { clearTimeout(state.calTimer); }
  state.calTimer = setTimeout(pollCalendar, delay);
}

function pollCalendar() {
  if (state.calInFlight) { return; }
  state.calInFlight = true;
  getJSON('/api/calendar', WEATHER_TIMEOUT_MS, function (err, data) {
    state.calInFlight = false;
    if (err || !data || typeof data.status !== 'string') {
      state.calReachable = false;
    } else {
      state.calReachable = true;
      state.cal = data;
    }
    renderCalendar();
    scheduleCalendar(CAL_POLL_MS);
  });
}

// "14:30" -> "14:30" or "2:30 PM" depending on the 12/24h setting.
function fmtHM(hm) {
  var m = /^(\d{2}):(\d{2})$/.exec(hm || '');
  if (!m) { return ''; }
  var p = hourParts(new Date(Date.UTC(2000, 0, 1, +m[1], +m[2])));
  return p.h + ':' + m[2] + (p.ampm ? ' ' + p.ampm : '');
}

function isoDay(d) {
  return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
}

function calDayLabel(iso, todayIso, tomorrowIso) {
  var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
  if (!m) { return iso || ''; }
  var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  var base = DAYS_SHORT[d.getUTCDay()] + ' ' + d.getUTCDate() + ' ' + MONTHS_SHORT[d.getUTCMonth()];
  if (iso === todayIso) { return 'Today · ' + base; }
  if (iso === tomorrowIso) { return 'Tomorrow · ' + base; }
  return base;
}

function calMessage(title, body) {
  setClass($('cal-list'), 'is-hidden', true);
  setClass($('cal-msg'), 'is-hidden', false);
  setText($('cal-msg-title'), title);
  setText($('cal-msg-body'), body || '');
}

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) { e.className = cls; }
  if (text) { e.textContent = text; }  // never innerHTML: titles come from the calendar
  return e;
}

function calEventRow(ev, now) {
  var row = el('div', 'cal-ev row');
  var time = el('div', 'cal-time');
  if (ev.all_day) {
    time.textContent = ev.multi_day ? 'All days' : 'All day';
  } else {
    time.textContent = fmtHM(ev.start_hm);
    time.appendChild(el('span', 'end', (ev.multi_day ? '→ ' : '') + fmtHM(ev.end_hm)));
    if (now !== null && ev.end_ms <= now) { row.className += ' past'; }
    else if (now !== null && ev.start_ms <= now) { row.className += ' now'; }
  }
  var what = el('div', 'cal-what col grow');
  what.appendChild(el('div', 'cal-ev-title', ev.title));
  if (ev.location) { what.appendChild(el('div', 'cal-ev-loc', ev.location)); }
  row.appendChild(time);
  row.appendChild(what);
  return row;
}

function renderCalendar() {
  var c = state.cal;
  var sub = $('cal-sub');
  var now = serverNow();
  var subText = '';
  var subWarn = false;

  if (!c) {
    calMessage(state.calReachable ? 'Loading calendar…' : 'Calendar unavailable',
      state.calReachable ? '' : 'Cannot reach the dashboard server. Retrying.');
    setText(sub, '');
    state.calRenderKey = '';
    return;
  }
  if (c.status === 'disabled') {
    calMessage('Calendar is turned off', 'Set calendar.enabled to true in config.json on the server.');
  } else if (c.status === 'setup') {
    calMessage('Google Calendar not connected', c.message || 'On the server run: python3 gcal.py auth');
  } else if (c.status === 'loading') {
    calMessage('Loading calendar…', 'Contacting Google.');
  } else if (!c.days) {
    calMessage('Calendar unavailable', (c.message ? c.message + ' ' : '') + 'Retrying automatically.');
    if (c.last_attempt_ms) { subText = 'Last attempt ' + formatStamp(c.last_attempt_ms); }
    subWarn = true;
  } else {
    // Rebuild the list only when the data, the minute or the 12/24h setting changed.
    var minute = now === null ? 0 : Math.floor(now / 60000);
    var key = c.updated_ms + '|' + minute + '|' + state.use24h;
    if (key !== state.calRenderKey) {
      state.calRenderKey = key;
      var list = $('cal-list');
      var zn = now === null ? null : zoned(now);
      var todayIso = zn ? isoDay(zn) : '';
      var tomorrowIso = zn ? isoDay(new Date(Date.UTC(zn.getUTCFullYear(), zn.getUTCMonth(), zn.getUTCDate() + 1))) : '';
      var frag = document.createDocumentFragment();
      for (var i = 0; i < c.days.length; i++) {
        var day = c.days[i];
        if (todayIso && day.date < todayIso) { continue; }  // cache from before midnight
        var head = el('div', 'cal-day' + (day.date === todayIso ? ' today' : ''),
          calDayLabel(day.date, todayIso, tomorrowIso));
        frag.appendChild(head);
        if (!day.events.length) {
          frag.appendChild(el('div', 'cal-empty', 'Nothing scheduled.'));
        }
        for (var j = 0; j < day.events.length; j++) {
          frag.appendChild(calEventRow(day.events[j], now));
        }
      }
      if (c.count === 0) {
        frag.appendChild(el('div', 'cal-empty', 'No events in the next ' + c.days_ahead + ' days.'));
      }
      while (list.firstChild) { list.removeChild(list.firstChild); }
      list.appendChild(frag);
    }
    setClass($('cal-msg'), 'is-hidden', true);
    setClass($('cal-list'), 'is-hidden', false);
    subText = (c.stale ? 'Stale · updated ' : 'Updated ') + formatStamp(c.updated_ms) +
      (c.truncated ? ' · showing first ' + c.count : '');
    subWarn = c.stale;
    if (c.stale && c.message) { subText += ' · ' + c.message; }
  }
  if (!state.calReachable) {
    subText = (subText ? subText + ' · ' : '') + 'server unreachable';
    subWarn = true;
  }
  setText(sub, subText);
  setClass(sub, 'warn', subWarn);
}
