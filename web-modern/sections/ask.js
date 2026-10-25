// Ask: questions, home commands ("apaga el abanico") and camera questions, typed or spoken, with spoken
// answers (Piper on the server). The server remembers the last few turns of this conversation for ~2 minutes;
// Clear starts a new one. Home actions that need a yes (alarm, media switch) are confirmed in a dialog.
// Voice: tap once, speak, and it stops by itself after a pause (same rules as the tablet), then the
// recording is transcribed and asked. MediaRecorder needs a secure page (https or localhost);
// elsewhere the button opens the phone's recorder through a file input.
import { h, icon, replace, card, confirmDialog } from '../lib/dom.js';
import { getJSON, postJSON, request } from '../lib/api.js';
import { refreshSoon } from '../lib/store.js';
import { onTick } from '../lib/clock.js';

const ASK_TIMEOUT_MS = 45000;
const VOICE_TIMEOUT_MS = 60000;  // upload + conversion + transcription
const SILENCE_MS = 1300;         // stop this long after speech ends
const NO_SPEECH_MS = 7000;       // give up (send nothing) if no speech is heard
const CALIBRATE_MS = 400;        // measure background noise first
const MIME_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];

// Module state, shared by the full view and the Overview card (only one is mounted at a time).
const s = {
  question: '',
  answer: '',
  answerCls: 'muted',
  status: '',
  statusCls: '',
  inFlight: false,
  token: 0,
  blockedUntil: 0,
  askWhenReady: false,
  ai: { enabled: true, configured: null, maxChars: 1000, minInterval: 10, left: null },
  voice: { enabled: true, configured: null, autoAsk: true, maxSeconds: 30, maxBytes: 8388608 },
  httpsPort: null,
  vstate: 'idle',  // idle | starting | recording | sending
  rec: null,       // { recorder, stream, ctx, timer, chunks, spoke, silentMs, noise, started }
  conv: newConversation(),
  speak: readSpeak(),
  speechOk: false,
  audio: null,     // AudioContext, created inside a tap so the answer may play later
  playing: null,
};

function newConversation() {
  return ('c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10)).slice(0, 24);
}
function readSpeak() {
  try { return localStorage.getItem('dashboard.aiSpeak') !== '0'; } catch { return true; }
}

// ---- spoken answers ----
function unlockAudio() {
  if (!s.speak || !s.speechOk) return;
  try {
    s.audio ??= new (window.AudioContext || window.webkitAudioContext)();
    if (s.audio.state === 'suspended') s.audio.resume();
  } catch { s.audio = null; }
}
function stopSpeech() {
  try { s.playing?.stop(); } catch { /* already ended */ }
  s.playing = null;
}
async function playSpeech(id) {
  if (!s.audio || !s.speak) return;
  const token = s.token;
  try {
    const resp = await fetch(`/api/tts/${encodeURIComponent(id)}.wav`, { cache: 'no-store' });
    if (!resp.ok || token !== s.token) return;
    const buffer = await s.audio.decodeAudioData(await resp.arrayBuffer());
    if (token !== s.token) return;
    stopSpeech();
    const src = s.audio.createBufferSource();
    src.buffer = buffer;
    src.connect(s.audio.destination);
    src.start();
    s.playing = src;
  } catch { /* the text is on screen anyway */ }
}
function toggleSpeak() {
  s.speak = !s.speak;
  try { localStorage.setItem('dashboard.aiSpeak', s.speak ? '1' : '0'); } catch { /* private mode */ }
  if (s.speak) unlockAudio(); else stopSpeech();
  update({});
}

async function confirmAction(c) {
  const yes = await confirmDialog({ title: `${c.title}?`, body: 'Asked in Ask.', yes: 'Yes' });
  if (!yes) return;
  unlockAudio();
  const token = ++s.token;
  try {
    const data = await postJSON('/api/assistant/confirm', { token: c.token, conversation: s.conv, speak: s.speak && s.speechOk },
      { timeout: 15000 });
    if (token !== s.token) return;
    update({ answer: data.answer, answerCls: '' });
    if (data.speech_id) playSpeech(data.speech_id);
  } catch (err) {
    update({ answer: err.body?.error?.message || 'Couldn’t reach the server.', answerCls: 'error' });
  }
  refreshSoon('home', 500);
}
const views = new Set();  // render functions of mounted views

function update(patch) {
  Object.assign(s, patch);
  for (const fn of views) fn();
}
const setStatus = (status, statusCls = '') => update({ status, statusCls });
const setAnswer = (answer, answerCls = '') => update({ answer, answerCls });

function idleText() {
  if (!s.ai.enabled) return 'The assistant is turned off on the server.';
  if (s.ai.configured === false) return 'The assistant isn’t set up yet. Add the API key on the admin page.';
  return 'Answers appear here. Try “apaga el abanico” or “what do you see on the street?”.';
}

async function loadStatus() {
  try {
    const [a, v] = await Promise.all([getJSON('/api/assistant/status'), getJSON('/api/voice/status').catch(() => null)]);
    s.ai = { enabled: a.enabled !== false, configured: !!a.configured, maxChars: a.max_question_chars || 1000,
      minInterval: a.min_interval_seconds || 0, left: a.remaining ?? null };
    s.speechOk = !!a.speech?.available;
    if (a.blocked) {
      s.blockedUntil = Date.now() + a.blocked_retry_after * 1000;
      if (!s.inFlight) Object.assign(s, { status: 'Temporarily unavailable (credits or access).', statusCls: 'warn' });
    }
    if (v) {
      s.voice = { enabled: v.enabled !== false, configured: !!v.configured, autoAsk: v.auto_ask !== false,
        maxSeconds: v.max_audio_seconds || 30, maxBytes: v.max_upload_bytes || 8388608 };
    }
  } catch (err) {
    if (err.status === 403) Object.assign(s, { status: 'Ask is only available on the home network.', statusCls: 'warn' });
  }
  if (!s.answer) Object.assign(s, { answer: idleText(), answerCls: 'muted' });
  update({});
}

// ---- asking ----
async function ask() {
  if (s.inFlight) return;
  const q = s.question.trim();
  if (!q) return setStatus('Type a question first.', 'warn');
  if (q.length > s.ai.maxChars) return setStatus(`Too long: ${s.ai.maxChars} characters at most.`, 'warn');
  if (s.blockedUntil > Date.now()) return;
  const token = ++s.token;
  stopSpeech();
  update({ inFlight: true, status: '', statusCls: '', answer: 'Thinking…', answerCls: 'muted' });
  try {
    const data = await postJSON('/api/assistant', { question: q, conversation: s.conv, speak: s.speak && s.speechOk },
      { timeout: ASK_TIMEOUT_MS });
    if (token !== s.token) return;  // cleared while waiting
    s.ai.left = data.remaining ?? s.ai.left;
    // House commands are answered on the server without the AI, so they don't count against the wait.
    if (data.source !== 'house') s.blockedUntil = Date.now() + s.ai.minInterval * 1000;
    const note = { local: 'Local fallback (small model, may be wrong)', offline: 'No internet for the cameras' }[data.source] ?? '';
    update({
      inFlight: false,
      answer: data.answer + (data.truncated ? '\n\n(The answer was cut off at the length limit.)' : ''),
      answerCls: '',
      // The cloud didn't answer, so a small model on the server did: say so, it can be wrong.
      status: note,
      statusCls: note ? 'warn' : '',
    });
    if (data.actions_done?.length) refreshSoon('home', 500);
    if (data.speech_id) playSpeech(data.speech_id);
    if (data.confirm?.token) confirmAction(data.confirm);
  } catch (err) {
    if (token !== s.token) return;
    const e = err.body?.error ?? {};
    if (!err.status) {
      const timedOut = err.message === 'timed out';
      if (timedOut) s.blockedUntil = Date.now() + 8000;
      update({ inFlight: false, answerCls: 'error', statusCls: 'bad',
        answer: timedOut ? 'No answer arrived in time. The server may still be working. Wait a few seconds and try again.'
          : 'Can’t reach the dashboard server. Check the Wi-Fi connection and try again.',
        status: timedOut ? 'Timed out' : 'Server unreachable' });
      return;
    }
    if (typeof e.retry_after === 'number' && e.retry_after > 0) s.blockedUntil = Date.now() + e.retry_after * 1000;
    if (e.code === 'not_configured') s.ai.configured = false;
    if (e.code === 'disabled') s.ai.enabled = false;
    update({ inFlight: false, answer: e.message || `The request failed (HTTP ${err.status}).`, answerCls: 'error',
      status: err.status === 429 ? 'Limit reached' : 'Unavailable', statusCls: err.status === 429 ? 'warn' : 'bad' });
    loadStatus();
  }
}

function clearAll() {
  cancelVoice();
  stopSpeech();
  s.conv = newConversation();
  s.token += 1;
  update({ question: '', inFlight: false, answer: idleText(), answerCls: 'muted', status: '', statusCls: '' });
}

// ---- voice ----
const secure = () => window.isSecureContext;
const canRecord = () => secure() && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined';

function httpsLink() {
  if (secure() || !s.httpsPort) return null;
  return `https://${location.hostname}:${s.httpsPort}/#assistant`;
}

async function startVoice(fileInput) {
  unlockAudio();  // inside the tap, so the spoken answer may play later
  if (s.vstate === 'recording') return finishVoice('tap');
  if (s.vstate !== 'idle' || s.inFlight) return;
  if (s.voice.configured === false) return setStatus('Voice input isn’t set up on the server.', 'warn');
  if (!canRecord()) {
    // Browsers only allow the microphone on https: go to the secure address (Voice works there with one tap).
    // The phone's recorder app is the fallback only when there is no https address.
    const link = httpsLink();
    if (link) { location.href = link; return; }
    fileInput.click();  // phone recorder or file chooser
    return;
  }
  update({ vstate: 'starting', status: 'Starting the microphone…', statusCls: '' });
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (err) {
    const blocked = ['NotAllowedError', 'SecurityError'].includes(err?.name);
    update({ vstate: 'idle', statusCls: 'warn',
      status: blocked ? 'Microphone access was blocked. Allow it in the browser’s site settings.' : 'Couldn’t start the microphone.' });
    return;
  }
  if (s.vstate !== 'starting') { stream.getTracks().forEach((t) => t.stop()); return; }  // cancelled meanwhile
  try {
    beginRecording(stream);
  } catch (err) {
    console.error(err);
    stream.getTracks().forEach((t) => t.stop());
    update({ vstate: 'idle', status: 'Recording isn’t available in this browser.', statusCls: 'warn' });
  }
}

function beginRecording(stream) {
  const mimeType = MIME_TYPES.find((t) => MediaRecorder.isTypeSupported?.(t));
  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  const r = { recorder, stream, ctx, chunks: [], spoke: false, silentMs: 0, noise: 0, started: performance.now(), last: performance.now(), reason: null };
  s.rec = r;
  recorder.ondataavailable = (e) => { if (e.data?.size) r.chunks.push(e.data); };
  recorder.onstop = () => onRecorded(r);
  recorder.start(250);
  // Simple voice-activity detection every 100 ms (same thresholds as the tablet).
  r.timer = setInterval(() => {
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) sum += v * v;
    const rms = Math.sqrt(sum / buf.length);
    const t = performance.now();
    const elapsed = t - r.started;
    const step = t - r.last;
    r.last = t;
    if (elapsed <= CALIBRATE_MS) { r.noise = Math.max(r.noise, rms); return; }
    if (rms > Math.max(0.015, r.noise * 2.5)) {
      if (!r.spoke) setStatus('Got it… pause when you’re done.');
      r.spoke = true;
      r.silentMs = 0;
    } else if (r.spoke) {
      r.silentMs += step;
    }
    if (r.spoke && r.silentMs >= SILENCE_MS) finishVoice('silence');
    else if (!r.spoke && elapsed >= NO_SPEECH_MS) finishVoice('nospeech');
    else if (elapsed >= s.voice.maxSeconds * 1000) finishVoice('max');
  }, 100);
  update({ vstate: 'recording', status: 'Listening… ask your question.', statusCls: '' });
}

function teardown(r) {
  clearInterval(r.timer);
  r.stream.getTracks().forEach((t) => t.stop());
  r.ctx.close?.().catch(() => {});
}

function finishVoice(reason) {
  const r = s.rec;
  if (!r || r.reason) return;
  r.reason = reason;
  clearInterval(r.timer);
  if (r.recorder.state !== 'inactive') r.recorder.stop();  // -> onRecorded
  else onRecorded(r);
}

function onRecorded(r) {
  teardown(r);
  if (s.rec !== r) return;  // cancelled
  s.rec = null;
  if (r.reason === 'cancel') return;
  if (r.reason === 'nospeech') return update({ vstate: 'idle', status: 'Didn’t hear anything. Tap Voice and speak.', statusCls: 'warn' });
  const type = (r.chunks[0]?.type || r.recorder.mimeType || 'audio/webm').split(';')[0];
  upload(new Blob(r.chunks, { type }));
}

function cancelVoice() {
  s.askWhenReady = false;
  const r = s.rec;
  if (r) {
    r.reason = 'cancel';
    s.rec = null;
    if (r.recorder.state !== 'inactive') r.recorder.stop();
    teardown(r);
  }
  if (s.vstate === 'recording' || s.vstate === 'starting') s.vstate = 'idle';
}

async function upload(blob) {
  if (!blob?.size) return update({ vstate: 'idle', status: 'Nothing was recorded. Try again.', statusCls: 'warn' });
  if (blob.size > s.voice.maxBytes) {
    return update({ vstate: 'idle', status: `That recording is too large. Keep it under ${s.voice.maxSeconds} seconds.`, statusCls: 'warn' });
  }
  update({ vstate: 'sending', status: 'Transcribing…', statusCls: '' });
  try {
    const data = await request('POST', '/api/transcribe', { body: blob, timeout: VOICE_TIMEOUT_MS });
    s.vstate = 'idle';
    if (data.no_speech || !data.text) return update({ status: 'Didn’t catch any speech. Try again a little closer.', statusCls: 'warn' });
    s.question = String(data.text).slice(0, s.ai.maxChars);
    const via = data.source === 'local' ? ' (local fallback)' : '';
    if (!s.voice.autoAsk) return update({ status: `Check the text, then tap Ask.${via}`, statusCls: via ? 'warn' : '' });
    if (s.blockedUntil > Date.now()) {
      s.askWhenReady = true;  // asked when the wait ends (see the tick in mount)
      return update({ status: `Will ask as soon as the wait ends…${via}`, statusCls: via ? 'warn' : '' });
    }
    update({});
    ask();
  } catch (err) {
    s.vstate = 'idle';
    if (!err.status) return update({ status: err.message === 'timed out' ? 'Transcription timed out. Try again.' : 'Can’t reach the dashboard server.', statusCls: 'bad' });
    const e = err.body?.error ?? {};
    if (e.code === 'not_configured') s.voice.configured = false;
    update({ answer: e.message || `Voice input failed (HTTP ${err.status}).`, answerCls: 'error',
      status: 'Voice failed', statusCls: err.status === 429 ? 'warn' : 'bad' });
  }
}

// ---- view ----
function build(compact) {
  const input = h(compact ? 'input' : 'textarea', {
    class: 'ai-input', placeholder: 'Ask anything…', 'aria-label': 'Question', maxlength: '2000',
    rows: compact ? null : '3', type: compact ? 'text' : null, enterkeyhint: 'send',
  });
  input.addEventListener('input', () => { s.question = input.value; renderButtons(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(); }
  });
  const fileInput = h('input', { type: 'file', accept: 'audio/*', capture: '', hidden: true });
  fileInput.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    fileInput.value = '';  // allow choosing the same recording again
    if (file) upload(file);
  });
  const voiceBtn = h('button', { type: 'button', class: 'ai-voice', onclick: () => startVoice(fileInput) },
    icon('mic', 'ai-mic'), h('span', null, 'Voice'));
  const askBtn = h('button', { type: 'button', class: 'btn ai-ask', onclick: () => { unlockAudio(); ask(); } }, 'Ask');
  const clearBtn = h('button', { type: 'button', class: 'btn btn-quiet', onclick: clearAll }, 'Clear');
  const speakBtn = h('button', { type: 'button', class: 'btn btn-quiet', onclick: toggleSpeak }, 'Sound on');
  const answer = h('div', { class: 'ai-answer', 'aria-live': 'polite' });
  const status = h('p', { class: 'ai-status' });
  const meta = h('p', { class: 'meta ai-meta' });

  function renderButtons() {
    const wait = Math.ceil((s.blockedUntil - Date.now()) / 1000);
    const unavailable = s.ai.configured === false || !s.ai.enabled;
    askBtn.textContent = s.inFlight ? 'Asking…' : wait > 0 && !unavailable ? (wait > 90 ? `Wait ${Math.ceil(wait / 60)}m` : `Wait ${wait}s`) : 'Ask';
    askBtn.disabled = s.inFlight || unavailable || wait > 0 || s.vstate !== 'idle';
    voiceBtn.hidden = !s.voice.enabled;
    voiceBtn.classList.toggle('recording', s.vstate === 'recording');
    voiceBtn.lastChild.textContent = { recording: 'Stop', starting: 'Mic…', sending: 'Sending…' }[s.vstate] ?? 'Voice';
    voiceBtn.disabled = s.vstate === 'starting' || s.vstate === 'sending' || s.inFlight;
    speakBtn.hidden = !s.speechOk;
    speakBtn.textContent = s.speak ? 'Sound on' : 'Sound off';
    speakBtn.setAttribute('aria-pressed', String(s.speak));
    const left = s.ai.left;
    meta.textContent = [`${s.question.length}/${s.ai.maxChars}`, left && `${left.hour} left this hour · ${left.day} today`]
      .filter(Boolean).join(' · ');
  }

  function render() {
    if (input.value !== s.question) input.value = s.question;
    answer.textContent = s.answer;  // plain text only
    answer.className = `ai-answer ${s.answerCls}`;
    const link = httpsLink();
    replace(status, s.status,
      // On plain http the mic can't be used in the page; point to the https address.
      !s.status && link && s.voice.enabled && s.vstate === 'idle'
        ? ['For one-tap voice open ', h('a', { href: link }, link.replace(/#.*$/, ''))] : null);
    status.className = `ai-status ${s.statusCls}`;
    renderButtons();
  }

  const controls = h('div', { class: 'ai-controls' }, voiceBtn, h('div', { class: 'ai-field' }, input,
    h('div', { class: 'ai-buttons' }, askBtn, !compact && clearBtn, !compact && speakBtn)), fileInput);
  const el = compact
    ? h('div', { class: 'ai-card' }, controls, status, answer)
    : h('div', { class: 'view-ask' }, h('h1', { class: 'view-title' }, 'Ask'), controls, status, answer, meta);
  return { el, render };
}

function mountWith(root, compact, wrap) {
  const v = build(compact);
  replace(root, wrap(v.el));
  views.add(v.render);
  v.render();
  loadStatus();
  // Countdown on the Ask button, and the queued voice question when the wait ends.
  const stopTick = onTick(() => {
    if (s.askWhenReady && s.blockedUntil <= Date.now() && !s.inFlight) { s.askWhenReady = false; ask(); }
    if (s.blockedUntil > Date.now() - 1000) v.render();
  });
  return () => {
    views.delete(v.render);
    stopTick();
    if (!views.size) cancelVoice();  // never leave the microphone on after leaving
  };
}

export function setHttpsPort(port) { s.httpsPort = port; }

export default {
  id: 'assistant',
  label: 'Ask',
  icon: 'assistant',
  mount(root) {
    return mountWith(root, false, (el) => el);
  },
  card(root) {
    return mountWith(root, true, (el) => card({ title: 'Ask', iconName: 'assistant', href: '#assistant', cls: 'card-ask' }, el));
  },
};
