/* Dashboard admin page. Plain JS, no libraries. All text is set with textContent (no innerHTML). */
(function () {
  'use strict';

  var view = document.getElementById('view');
  var timers = [];

  // ---- helpers ------------------------------------------------------------------------------
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === undefined || v === null || v === false) { return; }
        if (k === 'text') { el.textContent = v; }
        else if (k === 'cls') { el.className = v; }
        else if (k.slice(0, 2) === 'on') { el.addEventListener(k.slice(2), v); }
        else if (k === 'value') { el.value = v; }
        else if (k === 'checked') { el.checked = !!v; }
        else { el.setAttribute(k, v === true ? '' : v); }
      });
    }
    for (var i = 2; i < arguments.length; i++) { add(el, arguments[i]); }
    return el;
  }
  function add(el) {  // add(parent, child, child, ...); children may be arrays, text or null
    for (var i = 1; i < arguments.length; i++) {
      var child = arguments[i];
      if (child === null || child === undefined || child === false) { continue; }
      if (Array.isArray(child)) { child.forEach(function (c) { add(el, c); }); continue; }
      el.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
    }
  }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function setMsg(el, text, ok) { el.textContent = text || ''; el.className = 'msg ' + (ok ? 'ok' : text ? 'bad' : ''); }

  function api(path, body) {
    var opts = { credentials: 'same-origin', cache: 'no-store' };
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers = { 'Content-Type': 'application/json', 'X-Admin': '1' };
      opts.body = JSON.stringify(body);
    }
    return fetch('/api/admin/' + path, opts).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; }).then(function (d) {
        if (r.status === 401 && path !== 'login') { showLogin(); }
        if (!r.ok || d.ok === false) { var e = new Error(d.error || 'HTTP ' + r.status); e.data = d; throw e; }
        return d;
      });
    });
  }

  // Start a background job and resolve with its result once it finishes.
  function runJob(path, body, msgEl) {
    if (msgEl) { setMsg(msgEl, 'Working…', true); msgEl.className = 'msg'; }
    return api(path, body).then(function (d) {
      if (!d.job) { return d; }
      var id = d.job.id;
      return new Promise(function (resolve, reject) {
        (function poll() {
          api('job').then(function (j) {
            if (!j.job || j.job.id !== id) { reject(new Error('job lost')); return; }
            if (j.job.state === 'running') { setTimeout(poll, 1000); return; }
            resolve(j.job.result || {});
          }, reject);
        })();
      });
    });
  }

  function fmtBytes(n) {
    if (n === null || n === undefined) { return '—'; }
    var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + ' ' + u[i];
  }
  function fmtAgo(ms) {
    if (!ms) { return 'never'; }
    var s = (Date.now() - ms) / 1000;
    if (s < 0) { s = 0; }
    if (s < 90) { return Math.round(s) + ' s ago'; }
    if (s < 5400) { return Math.round(s / 60) + ' min ago'; }
    if (s < 172800) { return (s / 3600).toFixed(1) + ' h ago'; }
    return Math.round(s / 86400) + ' days ago';
  }
  function fmtDur(s) {
    var d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return (d ? d + 'd ' : '') + hh + 'h ' + m + 'm';
  }
  function fmtTime(ms) { return ms ? new Date(ms).toLocaleString() : '—'; }
  function pill(state, text) { return h('span', { cls: 'pill ' + state, text: text || state }); }
  function dot(state) { return h('span', { cls: 'dot ' + state }); }
  function every(fn, ms) { fn(); timers.push(setInterval(fn, ms)); }
  function field(label, input) { return [h('label', { text: label }), input]; }
  function card(title, extra) {
    var c = h('div', { cls: 'card' + (extra ? ' ' + extra : '') });
    if (title) { add(c, h('h2', { text: title })); }
    return c;
  }
  function errorCard(err) { return h('div', { cls: 'card' }, h('p', { cls: 'msg bad', text: err.message })); }

  // ---- shell: login, navigation, banners --------------------------------------------------
  function showLogin(note) {
    document.getElementById('app').classList.add('hidden');
    document.getElementById('login').classList.remove('hidden');
    if (note) { document.getElementById('login-note').textContent = note; }
    document.getElementById('login-password').focus();
  }
  function showApp() {
    document.getElementById('login').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
    route();
  }
  document.getElementById('login-form').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var pw = document.getElementById('login-password'), msg = document.getElementById('login-msg');
    api('login', { password: pw.value }).then(function () {
      pw.value = ''; setMsg(msg, ''); showApp();
    }, function (e) { setMsg(msg, e.message); pw.select(); });
  });
  document.getElementById('logout').addEventListener('click', function () {
    api('logout', {}).then(function () { showLogin('Signed out.'); }, function () { showLogin(); });
  });
  document.getElementById('restart-now').addEventListener('click', restartServer);

  function offlineBanner(st) {
    var b = document.getElementById('offline-banner');
    if (st && st.active) {
      b.textContent = 'OFFLINE TEST ON (' + st.mode + '): ' + st.services.join(', ') + ' act as if the internet is down. '
        + 'Switches off in ' + Math.floor(st.remaining_seconds / 60) + ' min ' + (st.remaining_seconds % 60) + ' s.';
      b.classList.remove('hidden');
    } else { b.classList.add('hidden'); }
  }
  function needRestart(on) { document.getElementById('restart-banner').classList.toggle('hidden', !on); }

  function restartServer() {
    if (!confirm('Restart the dashboard now? The tablet reconnects by itself.')) { return; }
    api('maint', { action: 'restart' }).then(function () {
      needRestart(false);
      clear(view);
      add(view, h('div', { cls: 'card' }, h('p', { text: 'Restarting…' })));
      var tries = 0;
      setTimeout(function wait() {
        fetch('/api/admin/session', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
          if (d.logged_in) { route(); } else { showLogin('The dashboard restarted. Sign in again.'); }
        }, function () { if (++tries < 60) { setTimeout(wait, 1000); } });
      }, 2500);
    }, function (e) { alert(e.message); });
  }

  var views = {};
  function route() {
    timers.forEach(clearInterval);
    timers = [];
    var name = (location.hash || '#overview').slice(1);
    if (!views[name]) { name = 'overview'; }
    var links = document.querySelectorAll('.side a[data-view]');
    for (var i = 0; i < links.length; i++) { links[i].classList.toggle('active', links[i].getAttribute('data-view') === name); }
    clear(view);
    views[name](view);
  }
  window.addEventListener('hashchange', function () {
    if (!document.getElementById('app').classList.contains('hidden')) { route(); }
  });

  // ---- Overview ------------------------------------------------------------------------------
  views.overview = function (root) {
    add(root, h('h1', { text: 'Overview' }));
    var box = h('div');
    add(root, box);
    every(function () {
      api('overview').then(function (d) {
        document.getElementById('version').textContent = 'v' + d.version;
        offlineBanner(d.offline_test);
        clear(box);
        var hc = d.health || {};
        var cards = h('div', { cls: 'cards' },
          h('div', { cls: 'card' }, h('div', { cls: 'muted', text: 'Server' }),
            h('div', { cls: 'big', text: 'v' + d.version }),
            h('div', { text: 'PID ' + d.pid + ' · up ' + fmtDur(d.uptime_seconds) }),
            h('div', { cls: 'muted', text: 'http :' + d.ports.http + (d.ports.https ? ' · https :' + d.ports.https : '') })),
          h('a', { cls: 'card', href: '#health' }, h('div', { cls: 'muted', text: 'Health checks' }),
            h('div', { cls: 'big' }, pill('ok', (hc.ok || 0) + ' ok'), ' ', pill('warn', (hc.warn || 0) + ' warn'), ' ',
              pill('fail', (hc.fail || 0) + ' fail'))),
          h('div', { cls: 'card' }, h('div', { cls: 'muted', text: 'Disk (root filesystem)' }),
            h('div', { cls: 'big', text: fmtBytes(d.disk.free) + ' free' }), h('div', { cls: 'muted', text: 'of ' + fmtBytes(d.disk.total) })),
          h('div', { cls: 'card' }, h('div', { cls: 'muted', text: 'Memory' }),
            h('div', { cls: 'big', text: fmtBytes(d.mem.MemAvailable) + ' available' }),
            h('div', { cls: 'muted', text: 'of ' + fmtBytes(d.mem.MemTotal) + (d.load ? ' · load ' + d.load.map(function (x) { return x.toFixed(2); }).join(' ') : '') })),
          h('div', { cls: 'card' }, h('div', { cls: 'muted', text: 'https certificate' }),
            h('div', { cls: 'big', text: d.cert ? Math.round((d.cert.expires * 1000 - Date.now()) / 86400000) + ' days left' : '—' }),
            h('div', { cls: 'muted', text: d.cert ? 'expires ' + d.cert.text : 'not found' })));
        add(box, cards);
        var s = d.services;
        function svcRow(name, state, detail, href) {
          return h('tr', null, h('td', null, dot(state), href ? h('a', { href: href, text: name }) : name), h('td', { text: detail }));
        }
        function aiDetail(a) {
          if (!a.enabled) { return 'turned off'; }
          var t = a.configured ? 'cloud: ' + a.model : 'API key: ' + a.problem;
          if (a.remaining) { t += ' · left this hour ' + a.remaining.hour + ', today ' + a.remaining.day; }
          t += ' · local fallback ' + (a.fallback.enabled ? 'on' : 'off');
          if (a.fallback.cloud_cooling_down) { t += ' (cloud cooling down)'; }
          return t;
        }
        add(box, h('div', { cls: 'card' }, h('h2', { text: 'Services' }), h('table', null, h('tbody', null,
          svcRow('Weather', s.weather.status === 'ok' ? (s.weather.stale ? 'warn' : 'ok') : 'fail',
            s.weather.status + (s.weather.error ? ' · ' + s.weather.error : '') + ' · updated ' + fmtAgo(s.weather.updated_ms)),
          svcRow('Maps', s.maps.status === 'ok' ? (s.maps.error ? 'warn' : 'ok') : 'fail',
            s.maps.status + ' · ' + s.maps.layers + ' layers · ' + s.maps.calls + ' tile calls since start' + (s.maps.error ? ' · ' + s.maps.error : '')),
          svcRow('Calendar', s.calendar.status === 'ok' ? (s.calendar.stale ? 'warn' : 'ok') : 'fail',
            s.calendar.status + (s.calendar.message ? ' · ' + s.calendar.message : '') + ' · updated ' + fmtAgo(s.calendar.updated_ms), '#calendar'),
          svcRow('Ask', s.assistant.enabled && s.assistant.configured ? 'ok' : 'warn', aiDetail(s.assistant), '#ai'),
          svcRow('Voice', s.voice.enabled && s.voice.configured ? 'ok' : 'warn', aiDetail(s.voice), '#ai')))));
      }, function (e) { clear(box); add(box, errorCard(e)); });
    }, 15000);
  };

  // ---- API keys ------------------------------------------------------------------------------
  views.keys = function (root) {
    add(root, h('h1', { text: 'API keys and providers' }),
      h('p', { cls: 'muted', text: 'Keys are stored in the server\'s private .env file (mode 600). They are write-only here: ' +
        'you see whether one is set and its last characters, never the value. Changes apply without a restart.' }));
    var box = h('div');
    add(root, box);
    function load() {
      api('keys').then(function (d) {
        clear(box);
        if (d.file_problem) { add(box, h('div', { cls: 'banner bad', text: d.file_problem })); }
        d.keys.forEach(function (k) {
          var input = h('input', { type: k.secret ? 'password' : 'text', autocomplete: 'off', spellcheck: 'false',
            placeholder: k.set ? 'Paste a new value to replace it' : 'Paste the ' + (k.kind === 'url' ? 'URL' : 'value') });
          var msg = h('p', { cls: 'msg' });
          var status = k.set
            ? [pill('ok', 'set'), ' ', h('code', { text: k.preview }), k.stored_as !== k.name ? h('span', { cls: 'muted', text: ' as ' + k.stored_as }) : null,
              k.in_environment ? h('span', { cls: 'muted', text: ' (from the server environment; that copy wins)' }) : null]
            : pill('warn', 'not set');
          add(box, h('div', { cls: 'card stack' },
            h('div', { cls: 'row' }, h('h2', { text: k.label }), h('span', { cls: 'muted', text: '· ' + k.used_by })),
            h('div', null, h('code', { cls: 'muted', text: k.name }), '  ', status),
            h('div', { cls: 'row' }, input,
              h('button', { cls: 'primary', text: 'Save', onclick: function () {
                if (!input.value.trim()) { setMsg(msg, 'Paste a value first.'); return; }
                api('keys/set', { name: k.name, value: input.value }).then(function (r) { input.value = ''; setMsg(msg, r.message, true); load(); },
                  function (e) { setMsg(msg, e.message); });
              } }),
              k.testable ? h('button', { text: 'Test', disabled: !k.set, onclick: function () {
                runJob('keys/test', { name: k.name }, msg).then(function (r) {
                  setMsg(msg, r.ok ? '✓ ' + (r.message || r.text || 'OK') + (r.ms ? ' (' + r.ms + ' ms)' : '') +
                    (r.cost_usd ? ' · $' + r.cost_usd : '') : '✗ ' + (r.error || 'failed'), r.ok);
                }, function (e) { setMsg(msg, e.message); });
              } }) : null,
              h('button', { cls: 'danger', text: 'Remove', disabled: !k.set, onclick: function () {
                if (!confirm('Remove ' + k.name + ' from the .env file?')) { return; }
                api('keys/remove', { name: k.name }).then(function (r) { setMsg(msg, r.message, true); load(); },
                  function (e) { setMsg(msg, e.message); });
              } })),
            msg));
        });
      }, function (e) { clear(box); add(box, errorCard(e)); });
    }
    load();
  };

  // ---- AI models -------------------------------------------------------------------------------
  views.ai = function (root) {
    add(root, h('h1', { text: 'AI models' }),
      h('p', { cls: 'muted', text: 'Each service tries the cloud model first. The local model answers only when the cloud gives no ' +
        'response at all (no headers within the deadline, or no connection); HTTP errors never fall back.' }));
    var box = h('div');
    add(root, box);
    function serviceCard(sid, title, d, local) {
      var s = d.settings, c = card(title + ' (' + (sid === 'assistant' ? 'Ask' : 'Voice') + ')', 'stack');
      var localModel = sid === 'assistant' ? local.servers.llm : local.servers.stt;
      add(c, h('div', { cls: 'chain' },
        h('span', { cls: 'step' }, dot(d.configured ? 'ok' : 'fail'), 'Cloud: ', h('b', { text: s.model })),
        '→',
        h('span', { cls: 'step' }, dot(!s.fallback_enabled ? 'unknown' : localModel.healthy ? 'ok' : 'fail'),
          'Local: ', h('b', { text: s.fallback_enabled ? (localModel.model || d.fallback_model) : 'off' })),
        d.cooling_down ? pill('warn', 'cloud skipped (cooldown)') : null));
      if (d.last) {
        add(c, h('div', { cls: 'muted', text: 'Last request: ' + fmtAgo(d.last.ts_ms) + ' via ' + d.last.source +
          ' (' + d.last.model + ') → ' + d.last.outcome + (d.last.duration_ms ? ', ' + d.last.duration_ms + ' ms' : '') +
          (d.last.cloud ? ', cloud ' + d.last.cloud : '') + (d.last.simulated ? ', simulated offline' : '') }));
      }
      if (!d.configured) { add(c, h('div', { cls: 'msg bad', text: 'Cloud API key: ' + d.problem + ' (see API keys)' })); }

      var dl = h('datalist', { id: 'models-' + sid });
      (d.cloud_models || []).forEach(function (m) { add(dl, h('option', { value: m })); });
      var model = h('input', { type: 'text', value: s.model, list: 'models-' + sid, spellcheck: 'false' });
      var msg = h('p', { cls: 'msg' });
      var inputs = {};
      function num(key, step) { inputs[key] = h('input', { type: 'number', step: step || 'any', value: s[key] }); return inputs[key]; }
      inputs.fallback_enabled = h('input', { type: 'checkbox', checked: s.fallback_enabled });
      var form = h('div', { cls: 'form' },
        field('Cloud model', h('div', { cls: 'row' }, model, dl,
          h('button', { text: 'Load list', onclick: function () {
            setMsg(msg, 'Loading the provider\'s model list…', true);
            api('ai/models', { service: sid, force: true }).then(function (r) {
              clear(dl);
              r.models.forEach(function (m) { add(dl, h('option', { value: m })); });
              setMsg(msg, r.models.length + ' models available; pick one in the Cloud model box.', true);
            }, function (e) { setMsg(msg, e.message); });
          } }))),
        sid === 'assistant' ? field('Max answer tokens (cloud)', num('max_output_tokens', 1)) : null,
        field('Local fallback', h('label', { cls: 'check' }, inputs.fallback_enabled, 'enabled')),
        field('No-response deadline (s)', num('no_response_seconds')),
        field('Cloud cooldown after no response (s)', num('cloud_cooldown_seconds')),
        field('Local time limit (s)', num('fallback_timeout_seconds')),
        sid === 'assistant' ? field('Max answer tokens (local)', num('fallback_max_output_tokens', 1)) : null);
      add(c, form, h('div', { cls: 'row' },
        h('button', { cls: 'primary', text: 'Save (applies now)', onclick: function () {
          var values = { model: model.value.trim(), fallback_enabled: inputs.fallback_enabled.checked };
          Object.keys(inputs).forEach(function (k) { if (k !== 'fallback_enabled') { values[k] = inputs[k].value; } });
          api('ai/save', { service: sid, values: values }).then(function (r) { setMsg(msg, r.message, true); reload(); },
            function (e) { setMsg(msg, e.message); });
        } }),
        h('button', { text: 'Test cloud', onclick: function () {
          runJob('ai/test', { service: sid, target: 'cloud' }, msg).then(function (r) {
            setMsg(msg, r.ok ? '✓ cloud answered in ' + r.ms + ' ms: ' + r.text + (r.cost_usd ? ' · $' + r.cost_usd : '') : '✗ ' + r.error, r.ok);
          }, function (e) { setMsg(msg, e.message); });
        } }),
        h('button', { text: 'Test local', onclick: function () {
          runJob('ai/test', { service: sid, target: 'local' }, msg).then(function (r) {
            setMsg(msg, r.ok ? '✓ local answered in ' + r.ms + ' ms: ' + (r.answer || r.text) : '✗ ' + r.error, r.ok);
          }, function (e) { setMsg(msg, e.message); });
        } })), msg);
      return c;
    }

    function localCard(L) {
      var c = card('Local models (this server)', 'stack');
      var t = h('tbody');
      [['llm', 'Chat (llama-server)'], ['stt', 'Speech (whisper-server)']].forEach(function (p) {
        var s = L.servers[p[0]];
        add(t, h('tr', null, h('td', null, dot(s.healthy ? 'ok' : s.running ? 'warn' : 'fail'), p[1]),
          h('td', { text: s.running ? 'PID ' + s.pid : 'not running' }), h('td', { text: s.model || '—' }),
          h('td', { text: s.backend || '—' }), h('td', { cls: 'num', text: fmtBytes(s.rss_bytes) }), h('td', { text: s.health })));
      });
      add(c, h('table', null, h('thead', null, h('tr', null, h('th', { text: 'Server' }), h('th', { text: 'Process' }),
        h('th', { text: 'Model' }), h('th', { text: 'Build' }), h('th', { text: 'Memory' }), h('th', { text: 'Health' }))), t),
        h('div', { cls: 'muted', text: 'Memory available: ' + fmtBytes(L.mem_available_bytes) + ' of ' + fmtBytes(L.mem_total_bytes) }));
      function sel(list, current, label) {
        var s = h('select');
        list.forEach(function (m) {
          var name = typeof m === 'string' ? m : m.name;
          add(s, h('option', { value: name, text: typeof m === 'string' ? m : name + ' (' + fmtBytes(m.bytes) + (m.checksum_listed ? '' : ', no checksum') + ')' }));
        });
        s.value = current;
        return s;
      }
      var cfg = L.config;
      var f = {
        llm_model: sel(L.models.llm, cfg.llm_model), llm_backend: sel(L.backends.llm, cfg.llm_backend),
        stt_model: sel(L.models.stt, cfg.stt_model), stt_backend: sel(L.backends.stt, cfg.stt_backend),
        threads: h('input', { type: 'number', min: 1, max: 16, step: 1, value: cfg.threads })
      };
      var msg = h('p', { cls: 'msg' });
      function values(restart) {
        return { llm_model: f.llm_model.value, llm_backend: f.llm_backend.value, stt_model: f.stt_model.value,
          stt_backend: f.stt_backend.value, threads: f.threads.value, restart: restart };
      }
      function after(r) {
        setMsg(msg, (r.ok ? '✓ ' : '✗ ') + (r.message || '') + (r.output ? '\n' + r.output : ''), r.ok);
        reload();
      }
      add(c, h('div', { cls: 'form' },
        field('Chat model', f.llm_model), field('Chat build', f.llm_backend),
        field('Speech model', f.stt_model), field('Speech build', f.stt_backend), field('CPU threads', f.threads)),
        h('p', { cls: 'muted', text: L.note + ' Files: ' + cfg.dir + '/models' }),
        h('div', { cls: 'row' },
          h('button', { cls: 'primary', text: 'Save and restart models', onclick: function () {
            runJob('ai/local/save', values(true), msg).then(after, function (e) { setMsg(msg, e.message); });
          } }),
          h('button', { text: 'Save only', onclick: function () {
            api('ai/local/save', values(false)).then(after, function (e) { setMsg(msg, e.message); });
          } }),
          ['start', 'stop', 'restart'].map(function (a) {
            return h('button', { text: a[0].toUpperCase() + a.slice(1), cls: a === 'stop' ? 'danger' : '', onclick: function () {
              runJob('ai/local/action', { action: a }, msg).then(after, function (e) { setMsg(msg, e.message); });
            } });
          })), msg);
      return c;
    }

    function reload() {
      api('ai').then(function (d) {
        offlineBanner(d.offline_test);
        clear(box);
        add(box, serviceCard('assistant', 'Chat', d.assistant, d.local), serviceCard('voice', 'Speech-to-text', d.voice, d.local),
          localCard(d.local));
      }, function (e) { clear(box); add(box, errorCard(e)); });
    }
    reload();
  };

  // ---- Offline test ----------------------------------------------------------------------------
  views.offline = function (root) {
    add(root, h('h1', { text: 'Offline test switch' }),
      h('p', { cls: 'muted', text: 'Makes the dashboard act as if the internet were down, so you can watch the local fallbacks ' +
        'and the stale-data display. Only the dashboard process is affected; Plex, Docker and everything else on the server ' +
        'keep their real network. It always switches itself off when the time runs out. The tablet shows a red OFFLINE TEST tag.' }));
    var box = h('div');
    add(root, box);
    var built = false, status, gates, form = {}, msg, results;

    function build(d) {
      built = true;
      status = h('div', { cls: 'big' });
      gates = h('div', { cls: 'muted' });
      msg = h('p', { cls: 'msg' });
      results = h('pre', { cls: 'out hidden' });
      var svcBoxes = d.services.map(function (s) {
        form[s] = h('input', { type: 'checkbox', checked: s === 'ask' || s === 'voice' });
        return h('label', { cls: 'check' }, form[s], s);
      });
      form.mode = h('select', null, h('option', { value: 'realistic', text: 'Realistic: requests hang until the no-response deadline (as in a real outage)' }),
        h('option', { value: 'instant', text: 'Instant: requests fail immediately (quick checks)' }));
      form.minutes = h('input', { type: 'number', min: 1, max: d.max_minutes, step: 1, value: 15 });
      add(box, h('div', { cls: 'card stack' }, status, gates,
        h('div', { cls: 'form' }, field('Act offline for', h('div', null, svcBoxes)), field('Mode', form.mode),
          field('Switch off after (minutes)', form.minutes)),
        h('div', { cls: 'row' },
          h('button', { cls: 'primary big-switch', text: 'Turn on', onclick: function () {
            var services = d.services.filter(function (s) { return form[s].checked; });
            api('offline', { services: services, mode: form.mode.value, minutes: form.minutes.value })
              .then(function () { setMsg(msg, 'Offline test is on.', true); refresh(); }, function (e) { setMsg(msg, e.message); });
          } }),
          h('button', { cls: 'danger big-switch', text: 'Turn off', onclick: function () {
            api('offline/off', {}).then(function () { setMsg(msg, 'Back online. Cloud cooldowns were reset.', true); refresh(); },
              function (e) { setMsg(msg, e.message); });
          } })), msg),
        h('div', { cls: 'card stack' }, h('h2', { text: 'Fallback test' }),
          h('p', { cls: 'muted', text: 'Sends one real question through Ask and one sample recording through Voice, the same way the ' +
            'tablet does, and shows which side answered. They count toward the usage limits. With the switch on (realistic), ' +
            'expect about ' + d.no_response_seconds.ask + ' s of waiting before the local model answers.' }),
          h('div', { cls: 'row' }, h('button', { text: 'Run fallback test', onclick: function () {
            results.classList.remove('hidden');
            results.textContent = 'Running… (up to a minute)';
            runJob('offline/fallback-test', { services: ['ask', 'voice'] }).then(function (r) {
              var lines = [];
              ['ask', 'voice'].forEach(function (k) {
                var x = r[k];
                if (!x) { return; }
                lines.push(k.toUpperCase() + ': ' + (x.ok ? 'answered by ' + x.source + ' in ' + x.ms + ' ms' : 'failed (' +
                  (x.error && x.error.message || x.status) + ')'));
                if (x.answer || x.text) { lines.push('   ' + (x.answer || x.text)); }
              });
              results.textContent = lines.join('\n');
            }, function (e) { results.textContent = e.message; });
          } })), results));
    }

    function refresh() {
      api('offline').then(function (d) {
        if (!built) { build(d); }
        var st = d.state;
        offlineBanner(st);
        clear(status);
        add(status, st.active ? [dot('fail'), 'ON (' + st.mode + '): ' + st.services.join(', ') + ' · ' +
          Math.floor(st.remaining_seconds / 60) + ' min ' + (st.remaining_seconds % 60) + ' s left'] : [dot('ok'), 'Off: normal network']);
        gates.textContent = 'Cloud cooldown: Ask ' + (d.gates.ask ? 'active (going straight to local)' : 'none') +
          ' · Voice ' + (d.gates.voice ? 'active' : 'none') +
          ' · Local fallback: Ask ' + (d.fallback_enabled.ask ? 'on' : 'OFF') + ', Voice ' + (d.fallback_enabled.voice ? 'on' : 'OFF');
      }, function (e) { clear(box); add(box, errorCard(e)); });
    }
    every(refresh, 2000);
  };

  // ---- Google Calendar -------------------------------------------------------------------------
  views.calendar = function (root) {
    add(root, h('h1', { text: 'Google Calendar' }));
    var box = h('div');
    add(root, box);
    var hint = h('input', { type: 'text', placeholder: 'Google account email (optional hint)', autocomplete: 'off' });
    var paste = h('input', { type: 'text', placeholder: 'Paste the address of the page that failed to load (http://127.0.0.1:…)', spellcheck: 'false' });
    var msg = h('p', { cls: 'msg' });
    var pendingBox = h('div', { cls: 'card stack hidden' });

    function load() {
      return api('calendar').then(function (d) {
        clear(box);
        var state = d.status === 'ok' ? (d.problem ? 'warn' : 'ok') : 'fail';
        var exp = d.token_expires ? Math.round((d.token_expires * 1000 - Date.now()) / 86400000 * 10) / 10 : null;
        add(box, h('div', { cls: 'card stack' },
          h('div', { cls: 'big' }, dot(state), d.status === 'ok' ? 'Connected' : d.status === 'setup' ? 'Not connected' : d.status),
          h('table', null, h('tbody', null,
            h('tr', null, h('td', { text: 'OAuth client (API keys page)' }), h('td', null, d.client_problem ? pill('fail', d.client_problem) : pill('ok', 'set'))),
            h('tr', null, h('td', { text: 'Sign-in token' }), h('td', null, d.token_problem ? pill('fail', d.token_problem) :
              [pill('ok', 'saved'), ' ', h('span', { cls: 'muted', text: 'created ' + (d.token_created || '?') })])),
            exp !== null ? h('tr', null, h('td', { text: 'Token lifetime' }), h('td', null, pill(exp <= 0 ? 'fail' : exp < 2 ? 'warn' : 'ok',
              exp <= 0 ? 'expired' : exp + ' days left'), h('span', { cls: 'muted', text: ' (Google expires sign-ins after ' + d.token_max_age_days +
              ' days while the app is in Testing; publish it and clear this in Settings → Health)' }))) : null,
            h('tr', null, h('td', { text: 'Last update' }), h('td', { text: fmtAgo(d.updated_ms) + ' · ' + d.count + ' events · ' + d.calendar_ids.join(', ') })),
            d.message ? h('tr', null, h('td', { text: 'Problem' }), h('td', { cls: 'msg bad', text: d.message })) : null)),
          h('div', { cls: 'row' }, hint,
            h('button', { cls: 'primary', text: d.token_problem ? 'Connect' : 'Reconnect', onclick: start }),
            d.token_problem ? null : h('button', { cls: 'danger', text: 'Revoke', onclick: function () {
              if (!confirm('Revoke the calendar sign-in at Google and delete it here?')) { return; }
              api('calendar/revoke', {}).then(function (r) { setMsg(msg, r.message, true); load(); }, function (e) { setMsg(msg, e.message); });
            } })), msg));
        add(box, pendingBox);
        showPending(d.pending);
        return d;
      }, function (e) { clear(box); add(box, errorCard(e)); });
    }

    function showPending(p) {
      clear(pendingBox);
      if (!p || p.result === 'done' || p.result === 'error') {
        pendingBox.classList.add('hidden');
        if (p && p.result === 'done') { setMsg(msg, p.message, true); }
        if (p && p.result === 'error') { setMsg(msg, p.message); }
        return;
      }
      pendingBox.classList.remove('hidden');
      add(pendingBox, h('h2', { text: 'Finish signing in' }),
        h('ol', null,
          h('li', null, h('a', { href: p.auth_url, target: '_blank', rel: 'noopener noreferrer', text: 'Open Google sign-in in a new tab' }),
            ' and sign in with the calendar\'s account.'),
          h('li', { text: 'If Google warns the app is unverified: Advanced → Go to … (it is your own app). Tick the calendar permission.' }),
          h('li', { text: 'Google then sends the browser to ' + p.redirect_uri + '. On this server it finishes by itself; on another ' +
            'computer that page fails to load. Copy its full address from the address bar and paste it here:' })),
        h('div', { cls: 'row' }, paste, h('button', { cls: 'primary', text: 'Finish', onclick: function () {
          api('calendar/finish', { url: paste.value }).then(function (r) { paste.value = ''; setMsg(msg, r.message, true); load(); },
            function (e) { setMsg(msg, e.message); });
        } }), h('button', { text: 'Cancel', onclick: function () { api('calendar/cancel', {}).then(load); } })),
        p.message ? h('p', { cls: 'msg bad', text: p.message }) : null);
    }

    function start() {
      api('calendar/start', { login_hint: hint.value.trim() }).then(function (r) {
        setMsg(msg, '');
        showPending(r.pending);
        window.open(r.pending.auth_url, '_blank', 'noopener');
      }, function (e) { setMsg(msg, e.message); });
    }

    load();
    timers.push(setInterval(function () {  // picks up a sign-in caught by the loopback listener
      if (!pendingBox.classList.contains('hidden')) {
        api('calendar').then(function (d) { if (!d.pending || d.pending.result === 'done') { load(); } });
      }
    }, 3000));
  };

  // ---- Health ------------------------------------------------------------------------------------
  views.health = function (root) {
    add(root, h('div', { cls: 'row' }, h('h1', { cls: 'grow', text: 'Health & uptime' }),
      h('button', { text: 'Check now', onclick: function () { runJob('health/run', {}).then(load); } })));
    var box = h('div');
    add(root, box);
    function load() {
      api('health').then(function (d) {
        clear(box);
        if (!d.enabled) { add(box, h('div', { cls: 'banner warn', text: 'Health checks are turned off (Settings → Health checks).' })); }
        add(box, h('p', { cls: 'muted' }, 'Last run ' + fmtAgo(d.last_run_ms) + ', every ' + d.interval_seconds + ' s. ',
          pill('ok', d.counts.ok + ' ok'), ' ', pill('warn', d.counts.warn + ' warn'), ' ', pill('fail', d.counts.fail + ' fail'),
          d.kuma.enabled ? ' · Uptime Kuma via ' + (d.kuma.source || '—') + (d.kuma.error ? ' (' + d.kuma.error + ')' : '') : ' · Uptime Kuma not configured (Settings → Uptime Kuma)',
          d.kuma.push_enabled ? ' · push ' + (d.kuma.push_error ? 'failed: ' + d.kuma.push_error : 'ok') : ''));
        var groups = {};
        d.checks.forEach(function (c) { (groups[c.group] = groups[c.group] || []).push(c); });
        Object.keys(groups).forEach(function (g) {
          var t = h('tbody');
          groups[g].forEach(function (c) {
            var hist = h('div', { cls: 'hist', title: 'last ' + c.history.length + ' checks' });
            c.history.slice(-60).split('').forEach(function (ch) { add(hist, h('span', { cls: ch === 'u' ? '' : ch })); });
            add(t, h('tr', null, h('td', null, dot(c.state), c.name), h('td', { text: c.detail || '' }),
              h('td', { cls: 'muted', text: 'since ' + fmtAgo(c.since_ms) }), h('td', null, hist)));
          });
          add(box, h('div', { cls: 'card' }, h('h2', { text: g }), h('table', null, t)));
        });
      }, function (e) { clear(box); add(box, errorCard(e)); });
    }
    every(load, 15000);
  };

  // ---- Settings ----------------------------------------------------------------------------------
  views.settings = function (root) {
    add(root, h('h1', { text: 'Settings' }),
      h('p', { cls: 'muted', text: 'Saved to config.json (the previous file is copied to backups/ first) and checked exactly as the ' +
        'server checks it at start-up. Fields marked ↻ take effect after a restart; the rest apply immediately. ' +
        'AI models and local models are on the AI models page.' }));
    var box = h('div');
    add(root, box);
    api('settings').then(function (d) {
      d.sections.forEach(function (sec) {
        var inputs = {}, msg = h('p', { cls: 'msg' });
        var form = h('div', { cls: 'form' });
        sec.fields.forEach(function (f) {
          var el, v = f.value;
          if (f.type === 'bool') { el = h('input', { type: 'checkbox', checked: v }); }
          else if (f.type.indexOf('enum:') === 0) {
            el = h('select');
            f.type.slice(5).split('|').forEach(function (o) { add(el, h('option', { value: o, text: o })); });
            el.value = v;
          } else if (f.type === 'list') { el = h('textarea', { value: (v || []).join('\n') }); }
          else if (f.type === 'json') { el = h('textarea', { cls: 'mono', value: JSON.stringify(v, null, 2) }); el.rows = 8; }
          else { el = h('input', { type: f.type === 'int' || f.type.indexOf('num') === 0 ? 'number' : 'text', step: 'any',
            value: v === null || v === undefined ? '' : v }); }
          inputs[f.key] = { el: el, type: f.type };
          add(form, h('label', { text: f.label + (f.restart ? ' ↻' : '') }), f.type === 'json' ? h('div', { cls: 'full' }, el) : el);
        });
        add(box, h('div', { cls: 'card stack' }, h('h2', { text: sec.title }), form,
          h('div', { cls: 'row' }, h('button', { cls: 'primary', text: 'Save ' + sec.title, onclick: function () {
            var values = {};
            Object.keys(inputs).forEach(function (k) {
              var x = inputs[k];
              values[k] = x.type === 'bool' ? x.el.checked : x.el.value;
            });
            api('settings', { section: sec.id, values: values }).then(function (r) {
              setMsg(msg, r.message, true);
              if (r.restart_required) { needRestart(true); }
            }, function (e) { setMsg(msg, e.message); });
          } })), msg));
      });
    }, function (e) { add(box, errorCard(e)); });
  };

  // ---- Logs & usage ------------------------------------------------------------------------------
  views.logs = function (root) {
    add(root, h('h1', { text: 'Usage' }));
    var usage = h('div', { cls: 'cards' });
    add(root, usage);
    api('usage').then(function (d) {
      [['assistant', 'Ask'], ['voice', 'Voice']].forEach(function (p) {
        var u = d[p[0]], t = h('tbody');
        ['today', 'month'].forEach(function (per) {
          var s = u.totals[per];
          add(t, h('tr', null, h('td', { text: per === 'today' ? 'Today' : 'This month' }), h('td', { cls: 'num', text: s.count }),
            h('td', { cls: 'num', text: s.ok }), h('td', { cls: 'num', text: s.local }), h('td', { cls: 'num', text: s.errors }),
            h('td', { cls: 'num', text: s.simulated }), h('td', { cls: 'num', text: '$' + s.cost_usd.toFixed(5) }),
            h('td', { cls: 'num', text: p[0] === 'voice' ? s.audio_seconds + ' s' : s.tokens })));
        });
        var rec = h('tbody');
        u.recent.slice(0, 12).forEach(function (e) {
          add(rec, h('tr', null, h('td', { text: fmtTime(e.ts_ms) }), h('td', { text: e.source + (e.simulated ? ' (sim)' : '') + (e.admin_test ? ' (test)' : '') }),
            h('td', { text: e.model }), h('td', { text: e.outcome }), h('td', { cls: 'num', text: e.duration_ms ? e.duration_ms + ' ms' : '' })));
        });
        add(usage, h('div', { cls: 'card wide' }, h('h2', { text: p[1] }),
          h('table', null, h('thead', null, h('tr', null, ['', 'Requests', 'OK', 'Local', 'Errors', 'Simulated', 'Cost', p[0] === 'voice' ? 'Audio' : 'Tokens']
            .map(function (x) { return h('th', { text: x }); }))), t),
          h('h3', { text: 'Recent' }), h('table', null, rec)));
      });
    }, function (e) { add(usage, errorCard(e)); });

    add(root, h('h1', { text: 'Logs' }));
    var file = h('select', null, h('option', { value: 'dashboard', text: 'Dashboard' }), h('option', { value: 'llama', text: 'llama-server' }),
      h('option', { value: 'whisper', text: 'whisper-server' }));
    var level = h('select', null, h('option', { value: '', text: 'All levels' }), h('option', { value: 'WARNING', text: 'Warnings + errors' }),
      h('option', { value: 'ERROR', text: 'Errors only' }));
    var q = h('input', { type: 'text', placeholder: 'Filter text' });
    var pre = h('pre', { cls: 'out' });
    function loadLog() {
      api('logs?file=' + file.value + '&level=' + level.value + '&lines=400&q=' + encodeURIComponent(q.value)).then(function (d) {
        pre.textContent = d.lines.join('\n') || '(no lines)';
        pre.scrollTop = pre.scrollHeight;
      }, function (e) { pre.textContent = e.message; });
    }
    [file, level].forEach(function (el) { el.addEventListener('change', loadLog); });
    q.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { loadLog(); } });
    add(root, h('div', { cls: 'card' }, h('div', { cls: 'row' }, file, level, q, h('button', { text: 'Refresh', onclick: loadLog })), pre));
    loadLog();
  };

  // ---- Maintenance -------------------------------------------------------------------------------
  views.maint = function (root) {
    add(root, h('h1', { text: 'Maintenance' }));
    var msg = h('p', { cls: 'msg' });
    function act(action, confirmText) {
      return function () {
        if (confirmText && !confirm(confirmText)) { return; }
        runJob('maint', { action: action }, msg).then(function (r) { setMsg(msg, r.message || (r.ok ? 'Done.' : r.error), r.ok !== false); },
          function (e) { setMsg(msg, e.message); });
      };
    }
    add(root, h('div', { cls: 'card stack' },
      h('div', { cls: 'row' }, h('button', { text: 'Refresh weather, maps and calendar now', onclick: act('refresh') })),
      h('div', { cls: 'row' }, h('button', { text: 'Rebuild weather maps', onclick: act('clear_maps', 'Delete the rendered maps and rebuild them now? The tablet shows no map until the rebuild finishes (about 2 minutes).') })),
      h('div', { cls: 'row' }, h('button', { text: 'Clear the assistant\'s quota pause', onclick: act('clear_ai_block') })),
      h('div', { cls: 'row' }, h('button', { text: 'Back up code and settings', onclick: act('backup') }),
        h('span', { cls: 'muted', text: 'to backups/ (without .env and data/)' })),
      h('div', { cls: 'row' }, h('button', { cls: 'danger', text: 'Restart dashboard', onclick: restartServer })),
      h('div', { cls: 'row' }, h('a', { href: '/dashboard-ca.crt', text: 'Download the https CA certificate' })), msg));
  };

  // ---- start ----------------------------------------------------------------------------------
  fetch('/api/admin/session', { credentials: 'same-origin', cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (d) {
    document.getElementById('version').textContent = d.version ? 'v' + d.version : '';
    if (!d.password_set) {
      showLogin('No admin password is set yet. On the server run: scripts/set-admin-password.sh');
      document.getElementById('login-password').disabled = true;
      return;
    }
    if (d.logged_in) { showApp(); } else { showLogin(); }
  }, function () { showLogin('Cannot reach the server.'); });
})();
