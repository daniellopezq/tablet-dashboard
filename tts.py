"""Spoken answers: Piper (local text-to-speech) turns an Ask answer into a short WAV.

The Ask reply carries a speech_id; the page fetches GET /api/tts/<id>.wav, which synthesizes on the
first fetch and keeps the WAV in a small in-memory cache. Text shows at once; audio follows ~1 s later.
Answers are not written to disk (only the phrase bank below is) and text is never logged.

Engine (0.25.0): tts.engine "google" speaks with Google Cloud Text-to-Speech (googletts.py, Chirp 3 HD
voice picked by ear on 2026-10-25) and falls back to Piper when Google fails, is over the monthly
character limit or the offline test blocks Ask; "piper" (the default) uses Piper only.

Piper is a self-contained binary outside the project (scripts/install-piper.sh), run once per answer at
low CPU priority, one synthesis at a time. The voice (Mexican Spanish or US English) is picked from the
answer text by guess_lang(), each with its own speed.

Phrase bank (0.24.0): the fixed replies to home commands (homeai.House.speech_phrases(), e.g. "Abanico
apagado.") plus tts.phrases_extra are rendered once in the background and kept as WAVs in data/tts-phrases/,
so those answers play at once. Only these template phrases are written to disk, never an AI answer. Each
file name is a hash of the engine, voice, speed and text, so changing a voice or speed re-renders them.
"""
import hashlib
import io
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import unicodedata
import wave
from collections import OrderedDict

import fallback as fb
import googletts

log = logging.getLogger("dashboard.tts")

# default: a "local-ai" directory next to this project (sibling folder), as scripts/install-piper.sh uses
_LOCAL_AI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local-ai")

DEFAULT_TTS_CONFIG = {
    "enabled": True,
    "binary": os.path.join(_LOCAL_AI, "piper", "piper", "piper"),
    "voices": {
        "es": os.path.join(_LOCAL_AI, "models", "piper", "es_MX-claude-high.onnx"),
        "en": os.path.join(_LOCAL_AI, "models", "piper", "en_US-lessac-medium.onnx"),
    },
    # Speaking speed per language: below 1 is faster (0.85 = about 15% faster). Picked by ear, 2026-10-23.
    "length_scale_es": 0.85,
    "length_scale_en": 1.0,
    "sentence_silence": 0.2,   # seconds of pause after each sentence
    "max_chars": 600,
    "timeout_seconds": 15,
    "cache_items": 8,
    "max_wav_bytes": 4 * 1024 * 1024,
    "phrase_bank": True,   # keep the fixed home replies as ready-made WAVs in data/tts-phrases/
    "phrases_extra": ["Listo.", "Ok.", "Apagado.", "Prendido.", "Done.", "Off.", "On."],
    "engine": "piper",     # "google": Google Cloud TTS first, Piper as the fallback
    "google_key_file": "data/google-tts-sa.json",   # service account key, relative to the project
    "google_voice_es": "es-US-Chirp3-HD-Achernar",
    "google_voice_en": "en-US-Chirp3-HD-Achernar",
    "google_speaking_rate_es": 1.0,
    "google_speaking_rate_en": 1.0,
    "google_monthly_chars": 500000,   # Google's free tier is 1M characters a month for Chirp 3 HD
    "google_timeout_seconds": 5,
}
GOOGLE_VOICE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}-[A-Za-z0-9-]{1,60}$")

BANK_RECHECK_SECONDS = 6 * 3600
BANK_PAUSE_SECONDS = 0.2   # between clips, so a live answer never waits long for the Piper lock
BANK_RETRY_SECONDS = 600   # after a build stopped early
BANK_MAX_FAILURES = 3      # stop a build early (e.g. Google unreachable); the next check tries again
BANK_FILE_RE = re.compile(r"^[0-9a-f]{32}\.wav$")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")

ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,32}$")

_ES_WORDS = {"el", "la", "los", "las", "de", "del", "que", "y", "en", "es", "un", "una", "por", "para",
             "con", "no", "si", "sí", "está", "esta", "están", "hay", "qué", "como", "cómo", "pero",
             "ya", "muy", "lo", "le", "se", "al", "su", "mi", "tu", "ahora", "apagado", "encendido",
             "prendido", "presionado", "listo", "hola", "gracias"}
_EN_WORDS = {"the", "a", "an", "of", "and", "is", "are", "to", "in", "it", "that", "this", "on", "off",
             "for", "with", "you", "i", "be", "was", "not", "there", "what", "now", "done", "yes", "no",
             "hello", "thanks", "can", "do", "does", "your", "my"}


def validate_tts_config(t):
    """Raise ValueError (caller wraps it) for unusable settings."""
    if not isinstance(t["enabled"], bool):
        raise ValueError("tts.enabled must be true or false")
    if not isinstance(t["binary"], str) or not t["binary"]:
        raise ValueError("tts.binary must be a path")
    v = t["voices"]
    if not isinstance(v, dict) or set(v) != {"es", "en"} or not all(isinstance(p, str) and p for p in v.values()):
        raise ValueError("tts.voices must be {\"es\": path, \"en\": path}")
    for key, lo, hi in (("length_scale_es", 0.5, 1.5), ("length_scale_en", 0.5, 1.5), ("sentence_silence", 0.0, 1.0)):
        val = t[key]
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not lo <= val <= hi:
            raise ValueError("tts.%s must be a number %s-%s" % (key, lo, hi))
    for key, lo, hi in (("max_chars", 20, 4000), ("timeout_seconds", 1, 60), ("cache_items", 1, 64),
                        ("max_wav_bytes", 65536, 16 * 1024 * 1024)):
        val = t[key]
        if not isinstance(val, int) or isinstance(val, bool) or not lo <= val <= hi:
            raise ValueError("tts.%s must be an integer %d-%d" % (key, lo, hi))
    if not isinstance(t["phrase_bank"], bool):
        raise ValueError("tts.phrase_bank must be true or false")
    extra = t["phrases_extra"]
    if (not isinstance(extra, list) or len(extra) > 200
            or not all(isinstance(p, str) and p.strip() and len(p) <= 200 for p in extra)):
        raise ValueError("tts.phrases_extra must be a list of up to 200 phrases (200 characters each)")
    if t["engine"] not in ("piper", "google"):
        raise ValueError("tts.engine must be \"piper\" or \"google\"")
    if not isinstance(t["google_key_file"], str) or not t["google_key_file"]:
        raise ValueError("tts.google_key_file must be a path")
    for lang in ("es", "en"):
        if not isinstance(t["google_voice_" + lang], str) or not GOOGLE_VOICE_RE.match(t["google_voice_" + lang]):
            raise ValueError("tts.google_voice_%s must be a voice name like es-US-Chirp3-HD-Achernar" % lang)
        val = t["google_speaking_rate_" + lang]
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not 0.5 <= val <= 2.0:
            raise ValueError("tts.google_speaking_rate_%s must be a number 0.5-2.0" % lang)
    for key, lo, hi in (("google_monthly_chars", 0, 1000000), ("google_timeout_seconds", 1, 30)):
        val = t[key]
        if not isinstance(val, int) or isinstance(val, bool) or not lo <= val <= hi:
            raise ValueError("tts.%s must be an integer %d-%d" % (key, lo, hi))


def _fold(text):
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def guess_lang(text):
    """'es' or 'en' for a short text: Spanish-only characters first, then common words."""
    if not isinstance(text, str):
        return "en"
    if re.search(r"[ñÑ¿¡áéíóúÁÉÍÓÚ]", text):
        return "es"
    words = re.findall(r"[a-zA-Z']+", text.lower())
    es = sum(1 for w in words if w in _ES_WORDS)
    en = sum(1 for w in words if w in _EN_WORDS)
    return "es" if es > en else "en"


def speakable(text, max_chars):
    """Plain text for the voice: no markdown/symbols, cut at a sentence end before max_chars."""
    text = fb.plain_text(text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#*_`~|<>\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = cut[:end + 1] if end > max_chars // 3 else cut
    return text


def split_banked(text, known):
    """Cut text into runs of whole sentences for which known(run) is true, longest runs first
    ("¿Apagar X? Confírmalo en la pantalla." is one bank phrase). None when some part is not known."""
    parts = [p for p in SENTENCE_SPLIT_RE.split(text) if p]
    out, i = [], 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            run = " ".join(parts[i:j])
            if known(run):
                out.append(run)
                i = j
                break
        else:
            return None
    return out or None


def _wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Speaker:
    def __init__(self, cfg, runner=subprocess.run, clock=time.monotonic, bank_dir=None, sleep=time.sleep,
                 google=None):
        self.cfg = cfg
        self.google = google                 # googletts.GoogleTTS, used when cfg["engine"] is "google"
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.bank_dir = bank_dir             # None: no phrase bank
        self._lock = threading.Lock()        # one synthesis at a time
        self._cache_lock = threading.Lock()
        self._items = OrderedDict()          # id -> {"text", "lang", "wav"}
        self._rates = {}
        self._phrases = None                 # callable -> list of texts (homeai.House.speech_phrases)
        self._wake = threading.Event()
        self._bank_counts = {"ready": 0, "total": 0}

    def apply_settings(self, values):
        self.cfg.update(values)
        self._wake.set()   # a new voice or speed means new bank files

    def _google_on(self):
        return self.cfg["engine"] == "google" and self.google is not None and self.google.problem() is None

    def engine(self):
        """The engine answers are spoken with right now: "google", "piper" or None."""
        if not self.cfg["enabled"]:
            return None
        if self._google_on():
            return "google"
        return "piper" if self._piper_problem() is None else None

    def problem(self):
        """None when some engine can speak, else a short reason (for the admin Health page and status)."""
        if not self.cfg["enabled"]:
            return "turned off"
        if self._google_on():
            return None
        return self._piper_problem()

    def _piper_problem(self):
        if not os.access(self.cfg["binary"], os.X_OK):
            return "Piper is not installed (scripts/install-piper.sh)"
        for lang, path in self.cfg["voices"].items():
            if not os.path.isfile(path):
                return "voice file for %s is missing" % lang
        return None

    def available(self):
        return self.problem() is None

    def status(self):
        out = {"enabled": self.cfg["enabled"], "available": self.available(), "engine": self.engine(),
               "phrases": dict(self._bank_counts)}
        if self.cfg["engine"] == "google" and self.google is not None:
            out["google"] = self.google.status()
        return out

    def prepare(self, text, lang=None):
        """Register an answer for speaking; returns a speech_id, or None when speech is off/unavailable."""
        if not self.available():
            return None
        clean = speakable(text, self.cfg["max_chars"])
        if not clean:
            return None
        sid = secrets.token_urlsafe(12)
        with self._cache_lock:
            self._items[sid] = {"text": clean, "lang": lang or guess_lang(clean), "wav": None}
            while len(self._items) > self.cfg["cache_items"]:
                self._items.popitem(last=False)
        return sid

    def _rate(self, model):
        if model not in self._rates:
            rate = 22050
            try:
                with open(model + ".json", encoding="utf-8") as f:
                    rate = int(json.load(f)["audio"]["sample_rate"])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            self._rates[model] = rate
        return self._rates[model]

    def wav(self, sid):
        """(status, bytes or None). Phrase bank first; otherwise synthesizes on first use and caches."""
        if not isinstance(sid, str) or not ID_RE.match(sid):
            return 404, None
        with self._cache_lock:
            item = self._items.get(sid)
        if item is None:
            return 404, None
        if item["wav"] is not None:
            return 200, item["wav"]
        banked = self._from_bank(item["text"])
        if banked is not None:
            item["wav"] = banked
            return 200, banked
        with self._lock:
            if item["wav"] is not None:
                return 200, item["wav"]
            started = self.clock()
            status, pcm, rate, used = self._synth(item["text"], item["lang"])
            if status != 200:
                return status, None
            item["wav"] = _wav(pcm, rate)
            log.info("tts: %s %s, %d chars in %.2f s", used, item["lang"], len(item["text"]), self.clock() - started)
            return 200, item["wav"]

    def _synth(self, text, lang, fallback=True):
        """(status, pcm, sample rate, engine used): Google when it is the engine, else (or on failure, with fallback) Piper.
        The caller holds self._lock."""
        if self._google_on():
            try:
                pcm, rate = self.google.synth(text, lang)
                return 200, pcm, rate, "google"
            except googletts.TTSError as exc:
                log.warning("tts: Google: %s%s", exc, "; using Piper" if fallback else "")
                if not fallback:
                    return exc.status, None, None, None
            except Exception:
                log.exception("tts: Google failed")
                if not fallback:
                    return 502, None, None, None
        if self._piper_problem() is not None:
            return 503, None, None, None
        model = self.cfg["voices"][lang]
        try:
            proc = self.runner(["nice", "-n", "10", self.cfg["binary"], "--model", model,
                                "--length_scale", str(self.cfg["length_scale_" + lang]),
                                "--sentence_silence", str(self.cfg["sentence_silence"]), "--output_raw"],
                               input=text.encode("utf-8"), stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=self.cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            log.warning("tts: Piper took longer than %ss", self.cfg["timeout_seconds"])
            return 504, None, None, None
        except OSError:
            log.error("tts: could not run Piper")
            return 503, None, None, None
        pcm = proc.stdout or b""
        if proc.returncode != 0 or len(pcm) < 2:
            log.warning("tts: Piper failed (exit %s)", proc.returncode)
            return 502, None, None, None
        if len(pcm) > self.cfg["max_wav_bytes"]:
            pcm = pcm[:self.cfg["max_wav_bytes"] & ~1]
        return 200, pcm, self._rate(model), "piper"

    # --- phrase bank ----------------------------------------------------------------------

    def set_phrases(self, fn):
        self._phrases = fn

    def _bank_name(self, text, lang):
        if self.engine() == "google":
            voice = ["google", self.cfg["google_voice_" + lang], self.cfg["google_speaking_rate_" + lang]]
        else:
            voice = ["piper", os.path.basename(self.cfg["voices"][lang]), self.cfg["length_scale_" + lang],
                     self.cfg["sentence_silence"]]
        key = json.dumps(voice + [lang, text], ensure_ascii=False)
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".wav"

    def _bank_on(self):
        return bool(self.bank_dir) and self.cfg["phrase_bank"] and self.available()

    def _bank_path(self, text):
        return os.path.join(self.bank_dir, self._bank_name(text, guess_lang(text)))

    def _from_bank(self, text):
        """The WAV for text when it is made of banked phrases; else None (never writes to disk)."""
        if not self._bank_on():
            return None
        runs = split_banked(text, lambda run: os.path.isfile(self._bank_path(run)))
        if runs is None:
            return None
        pcm, rate = [], None
        for run in runs:
            try:
                with wave.open(self._bank_path(run), "rb") as w:
                    if rate not in (None, w.getframerate()):
                        return None
                    rate = w.getframerate()
                    pcm.append(w.readframes(w.getnframes()))
            except (OSError, EOFError, wave.Error):
                return None
        return _wav(b"".join(pcm), rate)

    def _wanted(self):
        texts = list(self.cfg["phrases_extra"])
        if self._phrases is not None:
            try:
                texts += self._phrases()
            except Exception:
                log.error("tts: could not list the home phrases")
        want = {}
        for t in texts:
            clean = speakable(t, self.cfg["max_chars"])
            if clean:
                lang = guess_lang(clean)
                want[self._bank_name(clean, lang)] = (clean, lang)
        return want

    def build_bank(self):
        """Render missing bank phrases one at a time and remove ones no longer wanted.
        False when the build stopped early and should be retried soon."""
        if not self._bank_on():
            self._bank_counts = {"ready": 0, "total": 0}
            return
        want = self._wanted()
        os.makedirs(self.bank_dir, mode=0o700, exist_ok=True)
        have = set(os.listdir(self.bank_dir))
        ready = len(want.keys() & have)
        self._bank_counts = {"ready": ready, "total": len(want)}
        made = failed = in_a_row = 0
        complete = True
        for name, (text, lang) in want.items():
            if name in have:
                continue
            if self._wake.is_set() or not self._bank_on():
                return   # settings changed; the loop starts over
            if in_a_row >= BANK_MAX_FAILURES:
                log.warning("tts: phrase bank paused after %d failures in a row; retrying later", in_a_row)
                complete = False
                break
            with self._lock:
                status, pcm, rate, _ = self._synth(text, lang, fallback=False)  # never a Piper clip under a Google name
            if status == 200 and self._write_bank(name, _wav(pcm, rate)):
                made += 1
                in_a_row = 0
                self._bank_counts = {"ready": ready + made, "total": len(want)}
            else:
                failed += 1
                in_a_row += 1
            self.sleep(BANK_PAUSE_SECONDS)
        removed = 0
        for name in have - want.keys():
            if BANK_FILE_RE.match(name):
                try:
                    os.remove(os.path.join(self.bank_dir, name))
                    removed += 1
                except OSError:
                    pass
        if made or failed or removed:
            log.info("tts: phrase bank %d/%d ready (%d new, %d failed, %d removed)",
                     ready + made, len(want), made, failed, removed)
        return complete

    def _write_bank(self, name, data):
        path = os.path.join(self.bank_dir, name)
        tmp = path + ".tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            return True
        except OSError:
            log.warning("tts: could not save a phrase bank file")
            return False

    def start_bank(self):
        threading.Thread(target=self._bank_loop, name="tts-bank", daemon=True).start()

    def _bank_loop(self):
        while True:
            self._wake.clear()
            complete = False
            try:
                complete = self.build_bank() is not False
            except Exception:
                log.exception("tts: phrase bank build failed")
            self._wake.wait(BANK_RECHECK_SECONDS if complete else BANK_RETRY_SECONDS)
