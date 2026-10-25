/* Tablet dashboard client.
 * ES5 + XMLHttpRequest only (Android 4.4 WebView). No fetch, Promises, modules or Intl.
 * The clock is driven by server time: each sync anchors server ms to the local
 * Date.now(), and the display advances locally until the next sync.
 */
(function () {
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
    hcConfirmId: null,
    // cameras tab + motion pop-up
    cam: null,
    camError: null,
    camTimer: null,
    camInFlight: false,
    camDetail: null,          // id of the camera shown full size, or null for the grid
    camAlertSeen: '',         // key of the last alert already shown
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

  // ---- weather ----
  function scheduleWeather(delay) {
    if (state.weatherTimer) { clearTimeout(state.weatherTimer); }
    state.weatherTimer = setTimeout(pollWeather, delay);
  }

  function pollWeather() {
    if (state.weatherInFlight) { return; }
    state.weatherInFlight = true;
    getJSON('/api/weather', WEATHER_TIMEOUT_MS, function (err, data) {
      state.weatherInFlight = false;
      if (err || !data || typeof data.status !== 'string') {
        state.weatherReachable = false;
      } else {
        state.weatherReachable = true;
        state.weather = data;
      }
      renderWeather();
      scheduleWeather(WEATHER_POLL_MS);
    });
  }

  function fmtTemp(v) {
    return (typeof v === 'number') ? Math.round(v) + '°' : '--';
  }

  function iconSrc(name) {
    return 'icons/' + (ICONS[name] ? name : 'unknown') + '.svg';
  }

  function setImg(img, src, alt) {
    if (img.getAttribute('src') !== src) { img.setAttribute('src', src); }
    img.setAttribute('alt', alt || '');
  }

  function dayLabel(isoDate) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate || '');
    if (!m) { return '--'; }
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return DAYS_SHORT[d.getUTCDay()] + ' ' + d.getUTCDate();
  }

  function showWeatherMessage(title, body) {
    setClass($('wx-now'), 'has-msg', true);
    setText($('wx-msg-title'), title);
    setText($('wx-msg-body'), body || '');
    setClass($('wx-forecast'), 'is-hidden', true);
  }

  function renderWeather() {
    var w = state.weather;
    var meta = $('wx-meta');
    var metaText = '';
    var metaWarn = false;

    if (!w) {
      if (state.weatherReachable) {
        showWeatherMessage('Loading weather…', '');
      } else {
        showWeatherMessage('Weather unavailable', 'Cannot reach the dashboard server. Retrying.');
      }
      setText(meta, '');
      return;
    }

    setText($('wx-label'), w.label || '');

    if (w.status === 'unconfigured') {
      showWeatherMessage('Weather location not configured',
        'Set weather.latitude and weather.longitude in config.json on the server, then restart it.');
    } else if (w.status === 'loading') {
      showWeatherMessage('Loading weather…', 'Contacting Open-Meteo.');
    } else if (!w.data) {
      showWeatherMessage('Weather unavailable', (w.error ? w.error + '. ' : '') + 'Retrying automatically.');
      if (w.last_attempt_ms) { metaText = 'Last attempt ' + formatStamp(w.last_attempt_ms); }
      metaWarn = true;
    } else {
      var cur = w.data.current;
      var today = w.data.today;
      setClass($('wx-now'), 'has-msg', false);
      setImg($('wx-icon'), iconSrc(cur.icon), cur.condition);
      setText($('wx-temp'), fmtTemp(cur.temperature) + (w.unit || ''));
      setText($('wx-cond'), cur.condition || '');
      setText($('wx-hilo'), today ? 'H ' + fmtTemp(today.max) + '  ·  L ' + fmtTemp(today.min) : '');
      setText($('wx-hum'), typeof cur.humidity === 'number' ? 'Humidity ' + Math.round(cur.humidity) + '%' : '');

      var fc = w.data.forecast || [];
      for (var i = 0; i < 3; i++) {
        var card = $('fc' + i);
        var day = fc[i];
        setClass(card, 'is-hidden', !day);
        if (!day) { continue; }
        setText(card.querySelector('.fc-day'), dayLabel(day.date));
        setImg(card.querySelector('.fc-icon'), iconSrc(day.icon), day.condition);
        var temps = card.querySelector('.fc-temps');
        var html = fmtTemp(day.max) + ' <span class="lo">' + fmtTemp(day.min) + '</span>';
        if (temps.innerHTML !== html) { temps.innerHTML = html; }
      }
      setClass($('wx-forecast'), 'is-hidden', fc.length === 0);

      if (w.stale) {
        metaText = 'Stale · updated ' + formatStamp(w.updated_ms);
        metaWarn = true;
      } else {
        metaText = 'Updated ' + formatStamp(w.updated_ms);
      }
    }

    if (!state.weatherReachable) {
      metaText = (metaText ? metaText + ' · ' : '') + 'server unreachable';
      metaWarn = true;
    }
    setText(meta, metaText);
    setClass(meta, 'warn', metaWarn);
  }

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

  // ---- assistant: questions, home commands ("apaga el abanico"), camera questions, spoken answers ----
  // The server keeps the last few turns of this conversation (id below) for ~2 minutes; Clear starts a new one.
  var AI_TIMEOUT_MS = 45000;
  var VOICE_TIMEOUT_MS = 60000;  // upload + server conversion + transcription
  var AI_IDLE_TEXT = 'Answers appear here.';
  var AI_SETUP_TEXT = 'The assistant isn\'t set up yet. On the server, run scripts/set-assistant-key.sh ' +
    'and restart the dashboard (see README).';
  var ai = {
    inFlight: false, token: 0, handle: null, blockedUntil: 0, hasAnswer: false,
    configured: null, enabled: true, maxChars: 1000, minInterval: 10, left: null,
    page: 0, pages: 1, pageStep: 0,
    conv: '', speak: storageGet('dashboard.aiSpeak') !== '0', speechOk: false,
    audio: null, playing: null, confirmToken: null
  };

  function aiNewConversation() {
    ai.conv = ('c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)).slice(0, 24);
  }
  aiNewConversation();

  // Spoken answers play through Web Audio: Android Chrome only lets a page make sound after a tap, so the
  // context is created (and a silent sound played) inside the Ask/Voice tap, then reused for the answer.
  function aiUnlockAudio() {
    if (!ai.speak || !ai.speechOk) { return; }
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { return; }
    try {
      if (!ai.audio) { ai.audio = new AC(); }
      if (ai.audio.state === 'suspended' && ai.audio.resume) { ai.audio.resume(); }
      var buf = ai.audio.createBuffer(1, 1, 22050);
      var src = ai.audio.createBufferSource();
      src.buffer = buf;
      src.connect(ai.audio.destination);
      if (src.start) { src.start(0); } else { src.noteOn(0); }
    } catch (e) { ai.audio = null; }
  }

  function aiStopSpeech() {
    if (ai.playing) {
      try { if (ai.playing.stop) { ai.playing.stop(0); } else { ai.playing.noteOff(0); } } catch (e) { /* ended */ }
      ai.playing = null;
    }
  }

  function aiPlaySpeech(id) {
    if (!ai.audio || !ai.speak) { return; }
    var token = ai.token;
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/tts/' + encodeURIComponent(id) + '.wav', true);
    xhr.responseType = 'arraybuffer';
    xhr.timeout = 20000;
    xhr.onload = function () {
      if (xhr.status !== 200 || token !== ai.token || !ai.audio) { return; }
      ai.audio.decodeAudioData(xhr.response, function (buffer) {
        if (token !== ai.token) { return; }
        aiStopSpeech();
        var src = ai.audio.createBufferSource();
        src.buffer = buffer;
        src.connect(ai.audio.destination);
        ai.playing = src;
        if (src.start) { src.start(0); } else { src.noteOn(0); }
      }, function () { /* undecodable: the text is on screen anyway */ });
    };
    xhr.send();
  }

  function aiToggleSpeak() {
    ai.speak = !ai.speak;
    storageSet('dashboard.aiSpeak', ai.speak ? '1' : '0');
    if (ai.speak) { aiUnlockAudio(); } else { aiStopSpeech(); }
    aiRender();
  }

  // A home action that needs a yes (alarm, media switch): the same overlay as the Home tiles.
  function aiAskConfirm(c) {
    ai.confirmToken = c.token;
    state.hcConfirmId = null;
    setText($('hc-confirm-title'), c.title + '?');
    setText($('hc-confirm-body'), 'Asked by voice or text in Ask.');
    setText($('hc-yes'), 'Yes');
    setOverlay('hc-confirm', true);
  }

  function aiConfirmSend() {
    var tokenSent = ai.confirmToken;
    ai.confirmToken = null;
    if (!tokenSent) { return; }
    aiUnlockAudio();
    var token = ++ai.token;
    aiSetAnswer('…', 'muted');
    aiRequest('POST', '/api/assistant/confirm', { token: tokenSent, conversation: ai.conv, speak: ai.speak && ai.speechOk },
      15000, function (err, data) {
        if (token !== ai.token) { return; }
        if (!err && data && data.ok) {
          aiSetAnswer(data.answer, '');
          if (data.speech_id) { aiPlaySpeech(data.speech_id); }
        } else {
          aiSetAnswer(data && data.error ? data.error.message : 'Could not reach the server.', 'error');
        }
        pollHome(500);
        aiRender();
      });
  }

  // Like getJSON but also sends JSON and reports the HTTP status even for error responses.
  function aiRequest(method, url, bodyObj, timeoutMs, cb) {
    var xhr = new XMLHttpRequest();
    var finished = false;
    var timer = setTimeout(function () {
      if (finished) { return; }
      finished = true;
      try { xhr.abort(); } catch (e) { /* ignore */ }
      cb('timeout', null, 0);
    }, timeoutMs);
    xhr.onreadystatechange = function () {
      if (xhr.readyState !== 4 || finished) { return; }
      finished = true;
      clearTimeout(timer);
      if (!xhr.status) { cb('network', null, 0); return; }
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      cb(null, data, xhr.status);
    };
    try {
      xhr.open(method, url + (method === 'GET' ? '?_=' + Date.now() : ''), true);
      // A Blob/File (voice recording) is sent as raw bytes; anything else as JSON.
      var raw = typeof Blob !== 'undefined' && bodyObj instanceof Blob;
      if (bodyObj) {
        xhr.setRequestHeader('Content-Type', raw ? (bodyObj.type || 'application/octet-stream') : 'application/json');
      }
      xhr.send(bodyObj ? (raw ? bodyObj : JSON.stringify(bodyObj)) : null);
    } catch (e) {
      finished = true;
      clearTimeout(timer);
      cb('network', null, 0);
    }
    return {
      abort: function () {
        if (finished) { return; }
        finished = true;
        clearTimeout(timer);
        try { xhr.abort(); } catch (e) { /* ignore */ }
      }
    };
  }

  function aiSetAnswer(text, cls) {
    $('ai-answer-text').textContent = text;  // plain text only, never HTML
    $('ai-answer').className = 'ai-answer grow' + (cls ? ' ' + cls : '');
    ai.page = 0;
    aiPaginate();
  }

  // The answer is paged instead of scrolled (the touchscreen is poor at scrolling). The viewport is
  // cut to whole lines, the text is padded to a whole number of pages, and Back/Next move scrollTop
  // one page at a time. The pager only appears when the answer does not fit.
  function aiFitView() {
    var box = $('ai-answer');
    var view = $('ai-answer-view');
    var cs = window.getComputedStyle(box);
    var avail = box.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    var lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.3;
    var step = Math.max(1, Math.floor(avail / lh)) * lh;
    view.style.height = step + 'px';
    return step;
  }

  function aiPaginate() {
    var box = $('ai-answer');
    var text = $('ai-answer-text');
    var pager = $('ai-pager');
    if (!box.offsetHeight) { return; }  // tab not visible; redone by showView
    text.style.paddingBottom = '0';
    setClass(pager, 'is-hidden', true);
    var step = aiFitView();
    if (text.offsetHeight > step + 1) {
      setClass(pager, 'is-hidden', false);  // narrower now, so measure again
      step = aiFitView();
    }
    var h = text.offsetHeight;
    ai.pages = Math.max(1, Math.ceil((h - 1) / step));
    text.style.paddingBottom = (ai.pages * step - h) + 'px';
    ai.pageStep = step;
    aiShowPage(ai.page);
  }

  function aiShowPage(n) {
    ai.page = Math.max(0, Math.min(n, (ai.pages || 1) - 1));
    $('ai-answer-view').scrollTop = ai.page * (ai.pageStep || 0);
    $('ai-prev').disabled = ai.page === 0;
    $('ai-next').disabled = ai.page >= ai.pages - 1;
    setText($('ai-page-num'), (ai.page + 1) + ' / ' + ai.pages);
  }

  // Optional href: the text after `text` becomes a tappable link to it.
  function aiSetStatus(text, cls, href) {
    var el = $('ai-status');
    if (href) {
      while (el.firstChild) { el.removeChild(el.firstChild); }
      el.appendChild(document.createTextNode(text));
      var a = document.createElement('a');
      a.href = href;
      a.textContent = href.replace(/#.*$/, '');
      el.appendChild(a);
    } else {
      setText(el, text);
    }
    el.className = 'ai-status grow' + (cls ? ' ' + cls : '');
  }

  function aiIdleMessage() {
    if (!ai.enabled) { return 'The assistant is turned off on the server.'; }
    if (ai.configured === false) { return AI_SETUP_TEXT; }
    return AI_IDLE_TEXT;
  }

  function aiRender() {
    var wait = Math.ceil((ai.blockedUntil - Date.now()) / 1000);
    var unavailable = ai.configured === false || !ai.enabled;
    var label = 'Ask';
    if (ai.inFlight) {
      label = 'Asking…';
    } else if (wait > 0 && !unavailable) {
      label = wait > 90 ? 'Wait ' + Math.ceil(wait / 60) + 'm' : 'Wait ' + wait + 's';
    }
    var ask = $('ai-ask');
    ask.disabled = ai.inFlight || unavailable || wait > 0 || voice.state !== 'idle';
    setText(ask, label);
    setText($('ai-chars'), $('ai-input').value.length + '/' + ai.maxChars);
    var left = ai.left;
    setText($('ai-left'), left ? left.hour + ' left this hour · ' + left.day + ' today' : '');
    var sp = $('ai-speak');
    setClass(sp, 'is-hidden', !ai.speechOk);
    setClass(sp, 'is-off', !ai.speak);
    setText(sp, ai.speak ? 'Sound on' : 'Sound off');
    voiceRender();
  }

  function aiLoadStatus() {
    voiceLoadStatus();
    aiRequest('GET', '/api/assistant/status', null, 10000, function (err, data, status) {
      if (err || !data || status !== 200) { return; }
      ai.enabled = data.enabled !== false;
      ai.configured = !!data.configured;
      ai.maxChars = data.max_question_chars || 1000;
      ai.minInterval = data.min_interval_seconds || 0;
      ai.left = data.remaining || null;
      ai.speechOk = !!(data.speech && data.speech.available);
      if (data.blocked) {
        ai.blockedUntil = Date.now() + data.blocked_retry_after * 1000;
        if (!ai.inFlight) { aiSetStatus('Temporarily unavailable (credits or access).', 'warn'); }
      }
      if (!ai.hasAnswer && !ai.inFlight) { aiSetAnswer(aiIdleMessage(), 'muted'); }
      aiRender();
    });
  }

  function aiAsk() {
    if (ai.inFlight) { return; }
    var q = $('ai-input').value.replace(/^\s+|\s+$/g, '');
    if (!q) { aiSetStatus('Type a question first.', 'warn'); return; }
    if (q.length > ai.maxChars) { aiSetStatus('Too long: ' + ai.maxChars + ' characters at most.', 'warn'); return; }
    if (ai.blockedUntil > Date.now()) { return; }
    ai.inFlight = true;
    ai.hasAnswer = true;
    var token = ++ai.token;
    aiSetStatus('', '');
    aiSetAnswer('Thinking…', 'muted');
    aiRender();
    aiStopSpeech();
    var payload = { question: q, conversation: ai.conv, speak: ai.speak && ai.speechOk };
    ai.handle = aiRequest('POST', '/api/assistant', payload, AI_TIMEOUT_MS, function (err, data, status) {
      if (token !== ai.token) { return; }  // cleared while waiting
      ai.inFlight = false;
      ai.handle = null;
      if (err === 'timeout') {
        aiSetAnswer('No answer arrived in time. The server may still be working. Wait a few seconds and try again.', 'error');
        aiSetStatus('Timed out', 'bad');
        ai.blockedUntil = Date.now() + 8000;
      } else if (err) {
        aiSetAnswer('Cannot reach the dashboard server. Check the Wi-Fi connection and try again.', 'error');
        aiSetStatus('Server unreachable', 'bad');
      } else if (data && data.ok === true && typeof data.answer === 'string') {
        aiSetAnswer(data.answer + (data.truncated ? '\n\n(The answer was cut off at the length limit.)' : ''), '');
        // Cloud gave no response, so a small model on the server answered: say so, it can be wrong.
        if (data.source === 'local') { aiSetStatus('Local fallback (small model, may be wrong)', 'warn'); } else if (data.source === 'offline') {
          aiSetStatus('No internet for the cameras', 'warn');
        } else { aiSetStatus('', ''); }
        if (data.remaining) { ai.left = data.remaining; }
        // House commands are answered on the server without the AI, so they don't count against the wait.
        if (data.source !== 'house') { ai.blockedUntil = Date.now() + ai.minInterval * 1000; }
        if (data.actions_done && data.actions_done.length) { pollHome(500); }
        if (data.confirm && data.confirm.token) { aiAskConfirm(data.confirm); }
        if (data.speech_id) { aiPlaySpeech(data.speech_id); }
      } else {
        var e = data && data.error ? data.error : {};
        var kind = status === 429 ? 'warn' : 'bad';
        aiSetAnswer(e.message || 'The request failed (HTTP ' + status + ').', 'error');
        aiSetStatus(status === 429 ? 'Limit reached' : 'Unavailable', kind);
        if (typeof e.retry_after === 'number' && e.retry_after > 0) {
          ai.blockedUntil = Date.now() + e.retry_after * 1000;
        }
        if (e.code === 'not_configured') { ai.configured = false; }
        if (e.code === 'disabled') { ai.enabled = false; }
        aiLoadStatus();
      }
      aiRender();
    });
  }

  // ---- voice input: one tap -> listen -> stop on silence -> transcribe -> ask ----
  // In-page capture uses getUserMedia + Web Audio (works on Chrome 50) and needs a secure origin
  // (https or localhost). Elsewhere, including plain http, the button opens the system recorder.
  var VOICE_SILENCE_MS = 1300;    // stop this long after speech ends
  var VOICE_NO_SPEECH_MS = 7000;  // give up (and send nothing) if no speech is heard
  var VOICE_CALIBRATE_MS = 400;   // measure background noise before listening for speech
  var voice = {
    state: 'idle',  // idle | starting | recording | sending
    enabled: true, configured: null, autoAsk: true, askWhenReady: false,
    maxSeconds: 30, maxBytes: 8388608, forceFile: false,
    stream: null, ctx: null, source: null, proc: null, buffers: [], samples: 0, rate: 44100,
    spoke: false, silentMs: 0, noise: 0, handle: null
  };

  function voiceSecure() {
    var loc = window.location;
    return loc.protocol === 'https:' || loc.hostname === 'localhost' || loc.hostname === '127.0.0.1';
  }

  function voiceHasMicApi() {
    return !!((navigator.mediaDevices && navigator.mediaDevices.getUserMedia) ||
      navigator.webkitGetUserMedia || navigator.getUserMedia);
  }

  function voiceCanCapture() {
    return !voice.forceFile && voiceSecure() && voiceHasMicApi() &&
      !!(window.AudioContext || window.webkitAudioContext);
  }

  // Promise-based API when present (newer Chrome), callback API otherwise (Chrome 50).
  function voiceGetUserMedia(ok, fail) {
    var md = navigator.mediaDevices;
    if (md && md.getUserMedia) {
      try { md.getUserMedia({ audio: true }).then(ok, fail); } catch (e) { fail(e); }
      return;
    }
    var legacy = navigator.webkitGetUserMedia || navigator.getUserMedia;
    try { legacy.call(navigator, { audio: true }, ok, fail); } catch (e2) { fail(e2); }
  }

  function voiceStopTracks(stream) {
    if (!stream) { return; }
    var tracks = stream.getTracks ? stream.getTracks() : [];
    for (var i = 0; i < tracks.length; i++) { tracks[i].stop(); }
  }

  function voiceRender() {
    var label = 'Voice';
    if (voice.state === 'recording') {
      label = 'Stop';
    } else if (voice.state === 'starting') {
      label = 'Mic…';
    } else if (voice.state === 'sending') {
      label = 'Sending…';
    }
    var btn = $('ai-voice');
    setText($('ai-voice-label'), label);
    setClass(btn, 'recording', voice.state === 'recording');
    btn.disabled = voice.state === 'starting' || voice.state === 'sending' || ai.inFlight;
  }

  function voiceLoadStatus() {
    aiRequest('GET', '/api/voice/status', null, 10000, function (err, data, status) {
      if (err || !data || status !== 200) { return; }
      voice.enabled = data.enabled !== false;
      voice.configured = !!data.configured;
      voice.autoAsk = data.auto_ask !== false;
      voice.maxSeconds = data.max_audio_seconds || voice.maxSeconds;
      voice.maxBytes = data.max_upload_bytes || voice.maxBytes;
      setClass($('ai-voice'), 'is-hidden', !voice.enabled);
      voiceRender();
    });
  }

  function voiceClick() {
    aiUnlockAudio();  // inside the tap, so the spoken answer may play later
    if (voice.state === 'recording') { voiceFinish('tap'); return; }
    if (voice.state !== 'idle' || ai.inFlight) { return; }
    if (voice.configured === false) {
      aiSetStatus('Voice input isn\'t set up on the server (see README).', 'warn');
      return;
    }
    if (voiceCanCapture()) {
      voiceStartCapture();
      return;
    }
    if (!voiceSecure() && voiceHasMicApi() && state.httpsPort) {
      aiSetStatus('For one-tap voice open ', 'warn',
        'https://' + window.location.hostname + ':' + state.httpsPort + '/#assistant');
    }
    $('ai-voice-file').click();  // system recorder / file chooser
  }

  function voiceMicFailed(text) {
    voice.state = 'idle';
    voice.forceFile = true;  // next tap offers the recorder app instead
    aiSetStatus(text + ' Tap Voice to use the recorder app.', 'warn');
    aiRender();
  }

  function voiceStartCapture() {
    voice.state = 'starting';
    aiSetStatus('Starting the microphone…', '');
    aiRender();
    // Some browsers never answer the microphone prompt; don't leave the button stuck.
    setTimeout(function () {
      if (voice.state === 'starting') { voiceMicFailed('The microphone didn\'t respond.'); }
    }, 20000);
    voiceGetUserMedia(function (stream) {
      if (voice.state !== 'starting') { voiceStopTracks(stream); return; }  // cleared meanwhile
      try {
        voiceBeginCapture(stream);
      } catch (e) {
        voiceTeardown();
        voiceStopTracks(stream);
        voiceMicFailed('Recording isn\'t available here.');
      }
    }, function (err) {
      if (voice.state !== 'starting') { return; }
      var name = err && err.name;
      var blocked = name === 'NotAllowedError' || name === 'PermissionDeniedError' || name === 'SecurityError';
      voiceMicFailed(blocked ? 'Microphone access was blocked (allow it in Chrome\'s site settings).'
        : 'Couldn\'t start the microphone.');
    });
  }

  function voiceBeginCapture(stream) {
    var AC = window.AudioContext || window.webkitAudioContext;
    var ctx = new AC();
    voice.stream = stream;
    voice.ctx = ctx;
    if (ctx.state === 'suspended' && ctx.resume) { ctx.resume(); }
    var source = ctx.createMediaStreamSource(stream);
    var make = ctx.createScriptProcessor || ctx.createJavaScriptNode;
    var proc = make.call(ctx, 4096, 1, 1);
    voice.source = source;
    voice.proc = proc;
    voice.buffers = [];
    voice.samples = 0;
    voice.rate = ctx.sampleRate;
    voice.spoke = false;
    voice.silentMs = 0;
    voice.noise = 0;
    proc.onaudioprocess = voiceOnAudio;
    source.connect(proc);
    proc.connect(ctx.destination);  // required for the processor to run; it outputs silence
    voice.state = 'recording';
    aiSetStatus('Listening… ask your question.', '');
    aiRender();
  }

  // Simple voice-activity detection on each ~90 ms block of audio.
  function voiceOnAudio(e) {
    if (voice.state !== 'recording') { return; }
    var data = e.inputBuffer.getChannelData(0);
    var copy = new Float32Array(data.length);
    copy.set(data);
    voice.buffers.push(copy);
    voice.samples += copy.length;
    var sum = 0;
    for (var i = 0; i < data.length; i++) { sum += data[i] * data[i]; }
    var rms = Math.sqrt(sum / data.length);
    var elapsed = voice.samples / voice.rate * 1000;
    if (elapsed <= VOICE_CALIBRATE_MS) {
      voice.noise = Math.max(voice.noise, rms);
      return;
    }
    if (rms > Math.max(0.015, voice.noise * 2.5)) {
      if (!voice.spoke) { aiSetStatus('Got it… pause when you\'re done.', ''); }
      voice.spoke = true;
      voice.silentMs = 0;
    } else if (voice.spoke) {
      voice.silentMs += data.length / voice.rate * 1000;
    }
    if (voice.spoke && voice.silentMs >= VOICE_SILENCE_MS) {
      voiceFinish('silence');
    } else if (!voice.spoke && elapsed >= VOICE_NO_SPEECH_MS) {
      voiceFinish('nospeech');
    } else if (elapsed >= voice.maxSeconds * 1000) {
      voiceFinish('max');
    }
  }

  function voiceTeardown() {
    if (voice.proc) {
      voice.proc.onaudioprocess = null;
      try { voice.proc.disconnect(); } catch (e) { /* ignore */ }
    }
    if (voice.source) { try { voice.source.disconnect(); } catch (e2) { /* ignore */ } }
    if (voice.ctx && voice.ctx.close) { try { voice.ctx.close(); } catch (e3) { /* ignore */ } }
    voiceStopTracks(voice.stream);
    voice.proc = voice.source = voice.ctx = voice.stream = null;
  }

  function voiceFinish(reason) {
    if (voice.state !== 'recording') { return; }
    var buffers = voice.buffers;
    var samples = voice.samples;
    var rate = voice.rate;
    voice.buffers = [];
    voiceTeardown();
    if (reason === 'nospeech') {
      voice.state = 'idle';
      aiSetStatus('Didn\'t hear anything. Tap Voice and speak.', 'warn');
      aiRender();
      return;
    }
    voiceUpload(voiceEncodeWav(buffers, samples, rate));
  }

  // 16 kHz mono 16-bit WAV (what the server would convert to anyway; small to upload).
  function voiceEncodeWav(buffers, total, rate) {
    var outRate = 16000;
    var ratio = rate / outRate;
    var outLen = Math.max(0, Math.floor(total / ratio));
    var flat = new Float32Array(total);
    var off = 0;
    var i;
    for (i = 0; i < buffers.length; i++) {
      flat.set(buffers[i], off);
      off += buffers[i].length;
    }
    var view = new DataView(new ArrayBuffer(44 + outLen * 2));
    function str(pos, s) { for (var c = 0; c < s.length; c++) { view.setUint8(pos + c, s.charCodeAt(c)); } }
    str(0, 'RIFF');
    view.setUint32(4, 36 + outLen * 2, true);
    str(8, 'WAVE');
    str(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, outRate, true);
    view.setUint32(28, outRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    str(36, 'data');
    view.setUint32(40, outLen * 2, true);
    for (i = 0; i < outLen; i++) {
      var start = Math.floor(i * ratio);
      var end = Math.min(total, Math.floor((i + 1) * ratio));
      var acc = 0;
      var n = 0;
      for (var k = start; k < end; k++) { acc += flat[k]; n++; }
      var v = n ? acc / n : flat[Math.min(start, total - 1)] || 0;
      v = v < -1 ? -1 : (v > 1 ? 1 : v);
      view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7FFF, true);
    }
    return new Blob([view], { type: 'audio/wav' });
  }

  function voiceCancel() {
    voice.askWhenReady = false;
    if (voice.state === 'recording') { voice.buffers = []; voiceTeardown(); }
    if (voice.handle) { voice.handle.abort(); voice.handle = null; }
    voice.state = 'idle';
  }

  function voiceUpload(blob) {
    if (!blob || !blob.size) {
      voice.state = 'idle';
      aiSetStatus('Nothing was recorded. Try again.', 'warn');
      aiRender();
      return;
    }
    if (blob.size > voice.maxBytes) {
      voice.state = 'idle';
      aiSetStatus('That recording is too large. Keep it under ' + voice.maxSeconds + ' seconds.', 'warn');
      aiRender();
      return;
    }
    voice.state = 'sending';
    aiSetStatus('Transcribing…', '');
    aiRender();
    voice.handle = aiRequest('POST', '/api/transcribe', blob, VOICE_TIMEOUT_MS, function (err, data, status) {
      voice.handle = null;
      voice.state = 'idle';
      if (err === 'timeout') {
        aiSetStatus('Transcription timed out. Try again.', 'bad');
      } else if (err) {
        aiSetStatus('Cannot reach the dashboard server.', 'bad');
      } else if (data && data.ok === true) {
        if (data.no_speech || !data.text) {
          aiSetStatus('Didn\'t catch any speech. Try again a little closer.', 'warn');
        } else {
          $('ai-input').value = String(data.text).slice(0, ai.maxChars);
          var via = data.source === 'local' ? ' (local fallback)' : '';
          if (!voice.autoAsk) {
            aiSetStatus('Check the text, then tap Ask.' + via, via ? 'warn' : '');
          } else if (ai.blockedUntil > Date.now()) {
            voice.askWhenReady = true;  // asked automatically when the wait ends (see tick)
            aiSetStatus('Will ask as soon as the wait ends…' + via, via ? 'warn' : '');
          } else {
            aiRender();
            aiAsk();
            return;
          }
        }
      } else {
        var e = data && data.error ? data.error : {};
        if (e.code === 'not_configured') { voice.configured = false; }
        aiSetAnswer(e.message || 'Voice input failed (HTTP ' + status + ').', 'error');
        aiSetStatus('Voice failed', status === 429 ? 'warn' : 'bad');
      }
      aiRender();
    });
  }

  function voiceFileChosen() {
    var input = $('ai-voice-file');
    var file = input.files && input.files[0];
    input.value = '';  // allow choosing the same recording again
    if (file) { voiceUpload(file); }
  }

  function aiClear() {
    voiceCancel();
    aiStopSpeech();
    aiNewConversation();
    if (ai.handle) { ai.handle.abort(); ai.handle = null; }
    ai.token++;
    ai.inFlight = false;
    ai.hasAnswer = false;
    $('ai-input').value = '';
    aiSetAnswer(aiIdleMessage(), 'muted');
    aiSetStatus('', '');
    aiRender();
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

  // ---- home controls (Home Assistant buttons + Plex/Jellyfin switch; polled only while visible) ----
  var HC_ICONS = { media: 1, fan: 1, plug: 1, light: 1, home: 1, shield: 1, motion: 1 };
  var HC_MEDIA = { plex: 'Plex', jellyfin: 'Jellyfin' };

  function homeSync() {
    clearTimer('hcTimer');
    if (viewActive('home')) { pollHome(); }
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
    if (!viewActive('home')) { return; }
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
        clearTimer('hcTimer');
        var m = homeMedia();
        if (viewActive('home')) {
          state.hcTimer = setTimeout(function () { pollHome(); }, m && m.switching ? HOME_BUSY_POLL_MS : HOME_POLL_MS);
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
    var st = b.state;
    if (st === undefined) { return { name: b.label, word: 'RUN', cls: 'hc-off', hint: 'Tap to run' }; }
    if (st === null) { return { name: b.label, word: '?', cls: 'hc-unk', hint: 'HA not connected' }; }
    if (st === 'on') { return { name: b.label, word: 'ON', cls: 'hc-on', hint: 'Tap to turn off' }; }
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
    for (var k = 0; k < list.length; k++) { if (list[k].state === 'on') { on++; } }
    if (d.ha_ok === false) { parts.push(d.ha_error || 'Home Assistant not connected'); } else if (on) { parts.push(on + ' on'); }
    if (state.hcError) { parts.push('offline, showing last data'); }
    setText($('hc-sub'), parts.join(' · '));
    setClass($('hc-sub'), 'up-sub-bad', d.ha_ok === false);
  }

  function homePress(id) {
    var b = homeButton(id);
    if (!b || state.hcBusy[id]) { return; }
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
}());
