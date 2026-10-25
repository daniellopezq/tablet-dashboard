"""Tests for googletts.py (Google Cloud Text-to-Speech) and its use as tts.py's engine (0.25.0).

Google is a fake opener here and the service account key is generated per test run; nothing leaves this
machine. Run: python3 -m unittest discover -s tests -v
"""
import base64
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import googletts  # noqa: E402
import tts  # noqa: E402
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # optional: only the Google voice needs it (Ubuntu package python3-cryptography)
    raise unittest.SkipTest("cryptography is not installed")

TOKEN_URI = "https://oauth2.example.test/token"


def wav_bytes(frames=2400, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeGoogle:
    """Answers the token and synthesize calls the way Google does."""

    def __init__(self):
        self.tokens = 0
        self.synth = []
        self.fail = None   # HTTP code for the synthesize call

    def open(self, req, timeout=None):
        if req.full_url == TOKEN_URI:
            form = urllib.parse.parse_qs(req.data.decode())
            assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
            assert form["assertion"][0].count(".") == 2
            self.tokens += 1
            return FakeResponse(json.dumps({"access_token": "tok-%d" % self.tokens, "expires_in": 3600}).encode())
        assert req.full_url == googletts.SYNTH_URL
        assert req.headers["Authorization"].startswith("Bearer tok-")
        body = json.loads(req.data)
        self.synth.append(body)
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "err", {},
                                         io.BytesIO(b'{"error": {"status": "PERMISSION_DENIED"}}'))
        return FakeResponse(json.dumps({"audioContent": base64.b64encode(wav_bytes()).decode()}).encode())


class Clock:
    t = 1_761_350_400.0   # 2025-10-25 UTC

    def __call__(self):
        return self.t


def write_key(path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"type": "service_account", "client_email": "sa@test.iam.gserviceaccount.com",
                   "private_key": pem, "token_uri": TOKEN_URI, "project_id": "test"}, f)


class GoogleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.mkdtemp(prefix="gtts-key-")
        cls.key = os.path.join(cls.keydir, "sa.json")
        write_key(cls.key)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.keydir, True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gtts-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = json.loads(json.dumps(tts.DEFAULT_TTS_CONFIG))
        self.cfg["engine"] = "google"
        tts.validate_tts_config(self.cfg)
        self.fake = FakeGoogle()
        self.clock = Clock()
        self.usage = os.path.join(self.tmp, "usage.json")
        self.g = googletts.GoogleTTS(self.cfg, self.key, self.usage, clock=self.clock, opener=self.fake)


class GoogleTTSTests(GoogleCase):
    def test_synth_token_reuse_and_usage(self):
        pcm, rate = self.g.synth("Abanico apagado.", "es")
        self.assertEqual((len(pcm), rate), (4800, 24000))
        self.g.synth("Abanico is off.", "en")
        self.assertEqual(self.fake.tokens, 1)  # the token is reused until it nearly expires
        es, en = self.fake.synth
        self.assertEqual(es["voice"], {"languageCode": "es-US", "name": "es-US-Chirp3-HD-Achernar"})
        self.assertEqual(en["voice"]["name"], "en-US-Chirp3-HD-Achernar")
        self.assertEqual(self.g.status(), {"chars_this_month": 31, "monthly_limit": 500000})
        self.assertEqual(stat.S_IMODE(os.stat(self.usage).st_mode), 0o600)
        self.clock.t += 3600
        self.g.synth("Listo.", "es")
        self.assertEqual(self.fake.tokens, 2)

    def test_monthly_limit_and_new_month(self):
        self.cfg["google_monthly_chars"] = 20
        self.g.synth("Abanico apagado.", "es")
        with self.assertRaises(googletts.TTSError) as cm:
            self.g.synth("Abanico apagado.", "es")
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual(len(self.fake.synth), 1)  # refused before calling Google
        self.clock.t += 31 * 86400
        self.g.synth("Abanico apagado.", "es")
        self.assertEqual(self.g.usage(), 16)

    def test_errors_and_missing_key(self):
        self.fake.fail = 403
        with self.assertRaises(googletts.TTSError) as cm:
            self.g.synth("hola", "es")
        self.assertIn("PERMISSION_DENIED", str(cm.exception))
        self.assertEqual(self.g.usage(), 0)
        missing = googletts.GoogleTTS(self.cfg, os.path.join(self.tmp, "nope.json"), self.usage)
        self.assertIn("no usable service account key", missing.problem())

    def test_validation(self):
        for key, bad in (("engine", "polly"), ("google_voice_es", "Achernar"), ("google_speaking_rate_en", 3),
                         ("google_monthly_chars", 2000000)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                tts.validate_tts_config(dict(self.cfg, **{key: bad}))


FAKE_PIPER = """#!/bin/sh
cat > /dev/null
head -c 4410 /dev/zero
"""


class SpeakerGoogleTests(GoogleCase):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "piper")
        with open(self.bin, "w") as f:
            f.write(FAKE_PIPER)
        os.chmod(self.bin, stat.S_IRWXU)
        voices = {}
        for lang in ("es", "en"):
            voices[lang] = os.path.join(self.tmp, "%s.onnx" % lang)
            open(voices[lang], "wb").close()
        self.cfg.update(binary=self.bin, voices=voices, phrases_extra=[])
        self.piper_calls = []

        def runner(argv, **kw):
            self.piper_calls.append(argv)
            return subprocess.run(argv, **kw)
        self.s = tts.Speaker(self.cfg, runner=runner, google=self.g, sleep=lambda _: None,
                             bank_dir=os.path.join(self.tmp, "bank"))

    def test_google_first_piper_on_failure(self):
        self.assertEqual(self.s.status()["engine"], "google")
        status, wav = self.s.wav(self.s.prepare("La capital de Francia es París."))
        self.assertEqual((status, len(self.fake.synth), self.piper_calls), (200, 1, []))
        self.fake.fail = 500
        status, wav = self.s.wav(self.s.prepare("Otra respuesta distinta."))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.piper_calls), 1)  # fell back to Piper

    def test_bank_uses_google_names_and_never_piper(self):
        self.s.set_phrases(lambda: ["Abanico apagado.", "Abanico prendido."])
        self.assertTrue(self.s.build_bank())
        self.assertEqual((len(os.listdir(self.s.bank_dir)), len(self.fake.synth)), (2, 2))
        self.s.wav(self.s.prepare("Abanico apagado."))
        self.assertEqual(len(self.fake.synth), 2)  # from the bank
        piper_name = (self.cfg.update(engine="piper"), self.s._bank_name("Abanico apagado.", "es"))[1]
        self.cfg["engine"] = "google"
        self.assertNotIn(piper_name, os.listdir(self.s.bank_dir))
        # Google down: the build stops early, saves nothing and never uses Piper for bank clips.
        self.fake.fail = 500
        self.s.set_phrases(lambda: ["Uno.", "Dos.", "Tres.", "Cuatro.", "Cinco."])
        self.assertFalse(self.s.build_bank())
        self.assertEqual(len(self.fake.synth), 2 + tts.BANK_MAX_FAILURES)
        self.assertEqual(self.piper_calls, [])

    def test_piper_engine_ignores_google(self):
        self.cfg["engine"] = "piper"
        self.assertEqual(self.s.engine(), "piper")
        self.s.wav(self.s.prepare("Hola, esto es una prueba."))
        self.assertEqual((len(self.fake.synth), len(self.piper_calls)), (0, 1))


if __name__ == "__main__":
    unittest.main()
