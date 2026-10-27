/* Tablet dashboard client: Weather tab and the clock's weather strip.
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

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
