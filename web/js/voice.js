/* Tablet dashboard client: voice input: one tap -> listen -> stop on silence -> transcribe -> ask.
 * ES5 only (Chrome 50). index.html loads the js/ files in order; they share globals. */
'use strict';

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
