"""Google Cloud Text-to-Speech (Chirp 3 HD voices) for spoken answers, with Piper as the fallback.

Signs in with a service account key (data/google-tts-sa.json, mode 600, never logged or served): a
short-lived JWT is signed with python3-cryptography (Ubuntu package) and exchanged for an access token,
which is kept in memory until shortly before it expires. Only the answer text is sent to Google.

Spending guard: characters sent are counted per calendar month (UTC) in data/google-tts-usage.json and
calls stop at tts.google_monthly_chars, well under the 1M characters Google gives free each month for
Chirp 3 HD. Past the limit, or when Google fails or the offline test switch blocks Ask, tts.py uses Piper.
"""
import base64
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

import envfile
import netsim

log = logging.getLogger("dashboard.googletts")

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
SYNTH_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"
SAMPLE_RATE = 24000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_KEY_BYTES = 16384


class TTSError(Exception):
    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class GoogleTTS:
    def __init__(self, cfg, key_path, usage_path, clock=time.time, opener=None):
        self.cfg = cfg
        self.key_path = key_path
        self.usage_path = usage_path
        self.clock = clock
        self.opener = opener or urllib.request.build_opener()
        self._lock = threading.Lock()
        self._token = None   # (access token, expires at)
        self._problem, self._problem_at = None, None

    # --- set-up ---------------------------------------------------------------------------

    def _key(self):
        data = json.loads(envfile.read_private(self.key_path, MAX_KEY_BYTES)[0])
        if data.get("type") != "service_account" or not all(
                isinstance(data.get(k), str) for k in ("client_email", "private_key", "token_uri")):
            raise ValueError("not a service account key")
        return data

    def problem(self):
        """None when Google can be tried, else a short reason (status and admin). Re-checked every 30 s."""
        now = self.clock()
        if self._problem_at is None or now - self._problem_at > 30:
            self._problem = self._check()
            self._problem_at = now
        return self._problem

    def _check(self):
        try:
            self._key()
        except (OSError, ValueError):
            return "no usable service account key at %s" % self.key_path
        try:
            import cryptography  # noqa: F401
        except ImportError:
            return "python3-cryptography is not installed"
        return None

    def _access_token(self):
        now = self.clock()
        if self._token and self._token[1] - 60 > now:
            return self._token[0]
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        sa = self._key()
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({"iss": sa["client_email"], "scope": SCOPE, "aud": sa["token_uri"],
                                  "iat": int(now), "exp": int(now) + 3600}).encode())
        key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
        signed = head + b"." + claims
        assertion = signed + b"." + _b64(key.sign(signed, padding.PKCS1v15(), hashes.SHA256()))
        form = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": assertion.decode()}).encode()
        data = self._post(sa["token_uri"], form, "application/x-www-form-urlencoded")
        token = data.get("access_token")
        if not isinstance(token, str):
            raise TTSError(502, "no access token in Google's reply")
        self._token = (token, now + int(data.get("expires_in", 3600)))
        return token

    def _post(self, url, body, ctype, token=None):
        headers = {"Content-Type": ctype, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=self.cfg["google_timeout_seconds"]) as r:
                return json.loads(r.read(MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read(65536)).get("error", {})
                reason = err.get("status", "") if isinstance(err, dict) else err
            except (ValueError, AttributeError):
                reason = ""
            raise TTSError(502, "Google answered HTTP %s %s" % (exc.code, str(reason)[:60]))
        except (OSError, ValueError) as exc:
            raise TTSError(504, "could not reach Google (%s)" % type(exc).__name__)

    # --- monthly character count ----------------------------------------------------------

    def _month(self):
        return time.strftime("%Y-%m", time.gmtime(self.clock()))

    def usage(self):
        try:
            data = json.loads(envfile.read_private(self.usage_path, 4096)[0])
            if data.get("month") == self._month() and isinstance(data.get("chars"), int):
                return data["chars"]
        except (OSError, ValueError, AttributeError):
            pass
        return 0

    def _count(self, n):
        envfile.write_private(self.usage_path, json.dumps({"month": self._month(), "chars": self.usage() + n}))

    # --- speaking -------------------------------------------------------------------------

    def synth(self, text, lang):
        """(pcm, sample rate) for text, or raise TTSError."""
        if netsim.blocked("ask"):
            raise TTSError(504, "offline test")
        with self._lock:
            if self.usage() + len(text) > self.cfg["google_monthly_chars"]:
                raise TTSError(429, "monthly character limit reached")
            token = self._access_token()
            voice = self.cfg["google_voice_" + lang]
            body = json.dumps({
                "input": {"text": text},
                "voice": {"languageCode": voice[:5], "name": voice},
                "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": SAMPLE_RATE,
                                "speakingRate": self.cfg["google_speaking_rate_" + lang]},
            }).encode()
            data = self._post(SYNTH_URL, body, "application/json", token)
            self._count(len(text))
        try:
            with wave.open(io.BytesIO(base64.b64decode(data["audioContent"]))) as w:
                return w.readframes(w.getnframes()), w.getframerate()
        except (KeyError, TypeError, ValueError, EOFError, wave.Error):
            raise TTSError(502, "Google sent audio that could not be read")

    def status(self):
        return {"chars_this_month": self.usage(), "monthly_limit": self.cfg["google_monthly_chars"]}
