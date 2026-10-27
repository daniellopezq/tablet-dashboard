"""Voice input: uploaded audio -> ffmpeg normalisation -> Groq Whisper transcription.

The browser uploads one short recording. The server checks its type by content, converts it
to 16 kHz mono WAV with ffmpeg (trimmed to max_audio_seconds), sends only that WAV to Groq's
OpenAI-compatible transcription endpoint, and returns the text. Nothing is kept: temporary
files are deleted immediately, and audio, transcripts and the key are never logged.

Python 3 standard library plus the ffmpeg binary already on this server. Usage limits reuse
usage.UsageStore (thread lock + flock, persisted), with their own counters and log.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import assistant as am
import envfile
import fallback as fb
import netsim
from upstream import NoRedirect, UpstreamError, post_bounded
from usage import UsageStore, fmt_wait
from util import now_ms, num as _num

log = logging.getLogger("dashboard.voice")

KEY_NAMES = ("GROQ_API_KEY", "GROQ")  # GROQ_API_KEY is the standard name; GROQ is accepted as an alias
WAV_HEADER_BYTES = 44
WAV_BYTES_PER_SECOND = 16000 * 2  # 16 kHz, mono, 16-bit
MIN_AUDIO_SECONDS = 0.3

DEFAULT_VOICE_CONFIG = {
    "enabled": True,
    "auto_ask": True,  # the page asks the transcribed question immediately (fewer taps)
    "model": "whisper-large-v3-turbo",
    "base_url": "https://api.groq.com/openai/v1",
    "language": None,
    "env_file": ".env",
    "state_file": "data/voice-state.json",
    "usage_log": "data/voice-usage.jsonl",
    "tmp_dir": "data/voice-tmp",
    "ffmpeg_path": "ffmpeg",
    "max_upload_bytes": 8 * 1024 * 1024,
    "max_audio_seconds": 30,
    "max_text_chars": 1000,
    "min_interval_seconds": 5,
    "max_per_hour": 60,
    "max_per_day": 300,
    "timeout_seconds": 30,
    "ffmpeg_timeout_seconds": 20,
    "max_response_bytes": 262144,
    "no_speech_threshold": 0.6,
    "price_per_hour_usd": 0.04,
    # Whole request (conversion + cloud or local transcription) is capped server-side, leaving a
    # margin under the page's 60 s VOICE_TIMEOUT_MS.
    "total_timeout_seconds": 45,
    # Local fallback (whisper-server on this machine). Used ONLY when the cloud sends no response
    # headers within no_response_seconds; any HTTP status is a response and never falls back.
    "fallback_enabled": True,
    "fallback_base_url": "http://127.0.0.1:8092",
    "fallback_model": "whisper-base",
    "fallback_timeout_seconds": 20,
    "no_response_seconds": 5,
    "cloud_cooldown_seconds": 60,
}


def validate_voice_config(v):
    """Raise ValueError (caller wraps it) for unusable settings."""
    for key in ("enabled", "auto_ask"):
        if not isinstance(v[key], bool):
            raise ValueError("voice.%s must be true or false" % key)
    if not isinstance(v["model"], str) or not re.match(r"^[A-Za-z0-9._/:-]+$", v["model"]):
        raise ValueError("voice.model must be a model name")
    parts = urllib.parse.urlsplit(v["base_url"]) if isinstance(v["base_url"], str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("voice.base_url must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("voice.base_url must use https (http is allowed only for loopback tests)")
    if v["language"] is not None and (not isinstance(v["language"], str) or not re.match(r"^[a-z]{2,3}$", v["language"])):
        raise ValueError("voice.language must be null or an ISO-639-1 code such as 'en' or 'es'")
    for key in ("env_file", "state_file", "usage_log", "tmp_dir", "ffmpeg_path"):
        if not isinstance(v[key], str) or not v[key]:
            raise ValueError("voice.%s must be a path string" % key)
    for key, lo, hi in (("max_upload_bytes", 1024, 64 * 1024 * 1024), ("max_audio_seconds", 1, 120),
                        ("max_text_chars", 1, 4000), ("max_per_hour", 1, 10000), ("max_per_day", 1, 100000),
                        ("max_response_bytes", 1024, 1048576)):
        if not isinstance(v[key], int) or isinstance(v[key], bool) or not lo <= v[key] <= hi:
            raise ValueError("voice.%s must be an integer %d-%d" % (key, lo, hi))
    for key, lo, hi in (("min_interval_seconds", 0, 3600), ("timeout_seconds", 1, 120),
                        ("ffmpeg_timeout_seconds", 1, 120), ("no_speech_threshold", 0, 1)):
        if not _num(v[key]) or not lo <= v[key] <= hi:
            raise ValueError("voice.%s must be a number %s-%s" % (key, lo, hi))
    if v["price_per_hour_usd"] is not None and (not _num(v["price_per_hour_usd"]) or v["price_per_hour_usd"] < 0):
        raise ValueError("voice.price_per_hour_usd must be a non-negative number or null")
    if not _num(v["total_timeout_seconds"]) or not 5 <= v["total_timeout_seconds"] <= 300:
        raise ValueError("voice.total_timeout_seconds must be a number 5-300")
    am.validate_fallback_config(v, "voice")


def sniff_audio(data):
    """Return a short container label if the bytes look like supported audio, else None.

    Checking content (not the browser's label) keeps playlists, text and other formats away
    from ffmpeg's format auto-detection.
    """
    head = data[:16]
    if len(head) < 12:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[4:8] == b"ftyp":
        return "mp4"  # mp4 / m4a / 3gp family
    if head[:5] == b"#!AMR":
        return "amr"
    if head[:3] == b"ID3" or (head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"  # also covers raw AAC/ADTS frames
    return None


class AudioError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def resolve_ffmpeg(name):
    """Find ffmpeg even when /snap/bin is not on PATH (e.g. under a service manager)."""
    if os.path.isabs(name):
        return name
    found = shutil.which(name)
    if found:
        return found
    for candidate in ("/snap/bin/" + name, "/usr/bin/" + name, "/usr/local/bin/" + name):
        if os.access(candidate, os.X_OK):
            return candidate
    return name


def convert_to_wav(data, cfg, base_dir):
    """Convert uploaded audio to 16 kHz mono WAV bytes. Returns (wav_bytes, seconds)."""
    tmp_dir = os.path.join(base_dir, cfg["tmp_dir"])
    os.makedirs(tmp_dir, mode=0o700, exist_ok=True)
    in_fd, in_path = tempfile.mkstemp(dir=tmp_dir, prefix="in-", suffix=".bin")
    out_fd, out_path = tempfile.mkstemp(dir=tmp_dir, prefix="out-", suffix=".wav")
    os.close(out_fd)
    try:
        with os.fdopen(in_fd, "wb") as f:
            f.write(data)
        cmd = [resolve_ffmpeg(cfg["ffmpeg_path"]), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
               "-protocol_whitelist", "file", "-probesize", "2M", "-analyzeduration", "2M",
               "-i", in_path, "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-t", str(cfg["max_audio_seconds"]), "-ac", "1", "-ar", "16000", "-f", "wav", out_path]
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=cfg["ffmpeg_timeout_seconds"], check=False)
        except subprocess.TimeoutExpired:
            raise AudioError("bad_audio", "That recording took too long to process.")
        except OSError:
            log.error("voice: ffmpeg could not be started")
            raise AudioError("unavailable", "Audio conversion isn't available on the server.")
        if proc.returncode != 0:
            raise AudioError("bad_audio", "Couldn't read that recording. Try recording again.")
        size = os.path.getsize(out_path)
        if size <= WAV_HEADER_BYTES or size > 8 * 1024 * 1024:
            raise AudioError("bad_audio", "Couldn't read that recording. Try recording again.")
        with open(out_path, "rb") as f:
            wav = f.read()
        seconds = (len(wav) - WAV_HEADER_BYTES) / float(WAV_BYTES_PER_SECOND)
        if seconds < MIN_AUDIO_SECONDS:
            raise AudioError("too_short", "That recording was too short. Hold on a little longer.")
        return wav, seconds
    finally:
        for path in (in_path, out_path):
            try:
                os.remove(path)
            except OSError:
                pass


def encode_multipart(fields, filename, content, content_type):
    """Build a multipart/form-data body. Returns (body_bytes, content_type_header)."""
    boundary = "----tablet" + uuid.uuid4().hex
    out = []
    for name, value in fields:
        out.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                    % (boundary, name, value)).encode("utf-8"))
    out.append(('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n' % (boundary, filename, content_type)).encode("utf-8"))
    out.append(content)
    out.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out), "multipart/form-data; boundary=" + boundary


class Transcriber:
    def __init__(self, cfg, tz, base_dir, clock=now_ms):
        self.cfg = cfg
        self.base_dir = base_dir
        self.clock = clock
        self.env_path = os.path.join(base_dir, cfg["env_file"])
        self.store = UsageStore(os.path.join(base_dir, cfg["state_file"]),
                                   os.path.join(base_dir, cfg["usage_log"]), tz, clock)
        self._inflight = threading.Lock()
        self._opener = urllib.request.build_opener(NoRedirect)
        self._cloud = fb.HeaderDeadlineOpener(self._opener, "voice")
        self.gate = fb.CloudGate(cfg["cloud_cooldown_seconds"])
        self._warned = set()

    def apply_settings(self, values):
        """Live settings change from the admin page (already validated with the full config)."""
        self.cfg.update(values)
        self.gate.cooldown = self.cfg["cloud_cooldown_seconds"]

    @property
    def limits(self):
        c = self.cfg
        return {"min_interval_seconds": c["min_interval_seconds"], "max_per_hour": c["max_per_hour"],
                "max_per_day": c["max_per_day"], "noun": "recordings"}

    def _key(self):
        key, problem = envfile.load_api_key(self.env_path, KEY_NAMES)
        if problem and problem not in self._warned:
            self._warned.add(problem)
            log.warning("voice: Groq API key not usable (%s); see README", problem)
        return key, problem

    def status(self):
        key, problem = envfile.load_api_key(self.env_path, KEY_NAMES)
        out = {"enabled": self.cfg["enabled"], "auto_ask": self.cfg["auto_ask"],
               "configured": key is not None, "setup_problem": problem,
               "model": self.cfg["model"], "max_audio_seconds": self.cfg["max_audio_seconds"],
               "max_upload_bytes": self.cfg["max_upload_bytes"],
               "limits": {"per_hour": self.cfg["max_per_hour"], "per_day": self.cfg["max_per_day"]},
               "fallback": {"enabled": self.cfg["fallback_enabled"], "cloud_cooling_down": self.gate.cooling_down()}}
        try:
            out["remaining"] = self.store.snapshot(self.limits)["remaining"]
        except (OSError, ValueError, TypeError):
            out["remaining"] = None
        return out

    @staticmethod
    def _err(status, code, message, retry_after=None):
        return am.Assistant._err(status, code, message, retry_after)

    def transcribe(self, audio):
        """Return (http_status, json_body). Never raises."""
        if not self.cfg["enabled"]:
            return self._err(503, "disabled", "Voice input is turned off on the server.")
        key, _problem = self._key()
        if key is None:
            return self._err(503, "not_configured", "Voice input isn't set up yet. The Groq API key must be added "
                                                    "on the server (see README).")
        if sniff_audio(audio) is None:
            return self._err(415, "unsupported_audio", "That audio format isn't supported. "
                                                       "Use a normal voice recording (m4a, mp3, wav, ogg, webm or 3gp).")
        if not self._inflight.acquire(False):
            return self._err(429, "busy", "Another recording is still being processed. Try again in a few seconds.", 5)
        started = self.clock()
        t_start = time.monotonic()
        request_deadline = t_start + self.cfg["total_timeout_seconds"]
        try:
            try:
                wav, seconds = convert_to_wav(audio, self.cfg, self.base_dir)
            except AudioError as exc:
                status = 503 if exc.code == "unavailable" else 400
                return self._err(status, exc.code, exc.message)
            try:
                allowed, info = self.store.reserve(self.limits)
            except (OSError, ValueError, TypeError):
                return self._err(503, "unavailable", "Voice usage counters are unavailable, so voice input is paused.")
            if not allowed:
                return self._err(info["status"], info["code"], info["message"], info["retry_after"])
            remaining = info["remaining"]
            entry = {"ts_ms": started, "model": self.cfg["model"], "audio_seconds": round(seconds, 2),
                     "upload_bytes": len(audio), "source": "api",
                     "convert_ms": int((time.monotonic() - t_start) * 1000)}
            if netsim.blocked("voice"):
                entry["simulated"] = True
            price = self.cfg["price_per_hour_usd"]
            entry["estimated_cost_usd"] = (round(max(seconds, 10.0) / 3600.0 * price, 8) if price is not None else None)
            use_fallback = self.cfg["fallback_enabled"]
            if use_fallback and not self.gate.begin():
                return self._transcribe_locally(wav, entry, started, remaining, request_deadline, skipped=True)
            remaining_s = request_deadline - time.monotonic()
            if remaining_s < 1:
                entry.update({"outcome": "timeout", "duration_ms": self.clock() - started})
                if use_fallback:
                    self.gate.finish(True)  # no cloud attempt was made; don't leave a probe pending
                self._log_outcome(entry)
                return self._err(504, "timeout", "Processing the recording took too long. Try again.")
            responded = True
            t0 = time.monotonic()
            try:
                result = self._call_upstream(key, wav, min(self.cfg["timeout_seconds"], remaining_s))
            except fb.NoResponse:
                responded = False
                entry["api_wait_ms"] = int((time.monotonic() - t0) * 1000)
                return self._transcribe_locally(wav, entry, started, remaining, request_deadline, skipped=False)
            except UpstreamError as exc:
                entry.update({"outcome": exc.category, "http_status": exc.http_status,
                              "duration_ms": self.clock() - started})
                self._log_outcome(entry)
                return self._map_error(exc)
            except Exception:  # defensive: anything unexpected becomes a safe generic error
                log.error("voice: unexpected failure (details withheld)")
                entry.update({"outcome": "internal_error", "duration_ms": self.clock() - started})
                self._log_outcome(entry)
                return self._err(500, "internal_error", "Something went wrong transcribing that.")
            finally:
                if use_fallback:
                    self.gate.finish(responded)
            entry.update({"outcome": "no_speech" if result["no_speech"] else "success", "http_status": 200,
                          "duration_ms": self.clock() - started, "text_chars": len(result["text"])})
            self._log_outcome(entry)
            return 200, {"ok": True, "text": result["text"], "no_speech": result["no_speech"],
                         "seconds": round(seconds, 1), "remaining": remaining, "source": "api"}
        finally:
            self._inflight.release()

    def _transcribe_locally(self, wav, entry, started, remaining, request_deadline, skipped):
        """Transcribe with the local model after a cloud no-response (or during the cooldown)."""
        entry.update({"source": "local", "model": self.cfg["fallback_model"], "estimated_cost_usd": None,
                      "cloud": "skipped" if skipped else "no_response"})
        t0 = time.monotonic()
        deadline = min(t0 + self.cfg["fallback_timeout_seconds"], request_deadline)
        result, failure = None, None
        if not fb.acquire_local(deadline):
            failure = "local_busy"
        else:
            try:
                result = self._call_local(wav, deadline)
            except UpstreamError as exc:
                failure = exc.category
            except Exception:  # defensive, as for the cloud call
                log.error("voice: unexpected local fallback failure (details withheld)")
                failure = "internal_error"
            finally:
                fb.LOCAL_LOCK.release()
        entry.update({"local_ms": int((time.monotonic() - t0) * 1000), "duration_ms": self.clock() - started})
        if failure is not None:
            entry.update({"outcome": "local_error", "local_error": failure})
            self._log_outcome(entry)
            if skipped:
                return self._err(503, "local_unavailable",
                                 "Using the local fallback (the speech service was unreachable a moment ago), "
                                 "but it is unavailable right now. Try again shortly.")
            return self._err(503, "local_unavailable",
                             "The speech service didn't respond and the local fallback is unavailable.")
        entry.update({"outcome": "no_speech_local" if result["no_speech"] else "success_local",
                      "http_status": 200, "text_chars": len(result["text"])})
        self._log_outcome(entry)
        return 200, {"ok": True, "text": result["text"], "no_speech": result["no_speech"],
                     "seconds": round(entry["audio_seconds"], 1), "remaining": remaining, "source": "local"}

    def _log_outcome(self, entry):
        try:
            self.store.record(entry)
        except OSError:
            log.error("voice: could not write usage log")

    def _map_error(self, exc):
        cat = exc.category
        if cat == "auth_error":
            return self._err(503, "key_rejected", "The speech service rejected the server's Groq key. "
                                                  "Replace the key on the server (see README).")
        if cat in ("access_denied", "quota_exhausted"):
            return self._err(503, "unavailable", "The speech service refused the request (account or plan limit).")
        if cat == "provider_rate_limited":
            wait = exc.retry_after or 30
            return self._err(429, "provider_busy", "The speech service is busy. Try again in about %s."
                             % fmt_wait(wait), wait)
        if cat == "timeout":
            return self._err(504, "timeout", "The speech service took too long. Try again.")
        if cat == "network_error":
            return self._err(502, "network_error", "Couldn't reach the speech service from the server.")
        if exc.http_status == 413:
            return self._err(413, "too_large", "That recording is too large. Keep it shorter.")
        return self._err(502, "upstream_error", "The speech service returned an error. Try again later.")

    def _call_upstream(self, key, wav, timeout):
        fields = [("model", self.cfg["model"]), ("response_format", "verbose_json"), ("temperature", "0")]
        if self.cfg["language"]:
            fields.append(("language", self.cfg["language"]))
        body, ctype = encode_multipart(fields, "audio.wav", wav, "audio/wav")
        req = urllib.request.Request(
            self.cfg["base_url"].rstrip("/") + "/audio/transcriptions", data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": ctype, "Accept": "application/json",
                     "User-Agent": "TabletDashboard-voice"})
        no_response = self.cfg["no_response_seconds"] if self.cfg["fallback_enabled"] else None
        return self._parse(post_bounded(self._opener, self._cloud, req, timeout,
                                           self.cfg["max_response_bytes"], no_response))

    def _call_local(self, wav, deadline):
        # whisper-server's own API: multipart "file" upload to /inference.
        fields = [("response_format", "verbose_json"), ("temperature", "0"),
                  ("language", self.cfg["language"] or "auto")]
        body, ctype = encode_multipart(fields, "audio.wav", wav, "audio/wav")
        req = urllib.request.Request(
            self.cfg["fallback_base_url"].rstrip("/") + "/inference", data=body, method="POST",
            headers={"Content-Type": ctype, "Accept": "application/json", "User-Agent": "TabletDashboard-voice"})
        timeout = max(0.1, deadline - time.monotonic())
        return self._parse(post_bounded(self._opener, None, req, timeout, self.cfg["max_response_bytes"]))

    def _parse(self, raw):
        try:
            data = json.loads(raw.decode("utf-8"))
            text = data["text"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise UpstreamError("bad_response", 200)
        if not isinstance(text, str):
            raise UpstreamError("bad_response", 200)
        text = " ".join(text.split())  # collapse whitespace/newlines; this fills a one-question box
        # Pure punctuation (Whisper returns "." for a tone or noise) is not speech.
        no_speech = not re.search(r"\w", text)
        segments = data.get("segments")
        if text and isinstance(segments, list):
            probs = [s.get("no_speech_prob") for s in segments
                     if isinstance(s, dict) and _num(s.get("no_speech_prob"))]
            # Whisper invents text ("Thank you.") for silence; if every segment looks like non-speech, drop it.
            if probs and min(probs) > self.cfg["no_speech_threshold"]:
                no_speech = True
        if no_speech:
            text = ""
        return {"text": text[:self.cfg["max_text_chars"]], "no_speech": no_speech}
