/* Tablet dashboard client: Ask tab: questions, home commands, camera questions, spoken answers.
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

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
