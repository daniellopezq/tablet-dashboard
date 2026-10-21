"""Tests for voice input (voice.py + /api/transcribe).

Uses the real ffmpeg on this server to create and convert test tones, and a local mock of
Groq's transcription endpoint; nothing leaves this machine.
Run: python3 -m unittest discover -s tests -v
"""
import http.client
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
import voice as vm  # noqa: E402
from test_server import DashboardTestCase, start_thread_server  # noqa: E402

FAKE_KEY = "gsk_FAKEKEY1234567890"
SPOKEN = "How tall is Mount Whitney?"
FFMPEG = vm.resolve_ffmpeg("ffmpeg")


class MockGroq(BaseHTTPRequestHandler):
    mode = "ok"
    calls = []

    def _json(self, status, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        MockGroq.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                               "ctype": self.headers.get("Content-Type"), "body": body})
        mode = MockGroq.mode
        err = {"error": {"message": "SECRET-UPSTREAM-DETAIL", "type": "x"}}
        if mode in ("401", "500"):
            return self._json(int(mode), err)
        if mode == "429":
            return self._json(429, err, {"retry-after": "12"})
        if mode in ("slow", "slow1"):
            time.sleep(4 if mode == "slow" else 1.0)
        if mode == "garbage":
            return self._json(200, ["not", "an", "object"])
        if mode == "silence":
            return self._json(200, {"text": " Thank you.", "segments": [{"id": 0, "no_speech_prob": 0.97}]})
        if mode == "empty":
            return self._json(200, {"text": "  "})
        if mode == "dot":  # what the live API returned for a 2-second tone
            return self._json(200, {"text": " .", "segments": [{"id": 0, "no_speech_prob": 0.3}]})
        if mode == "textonly":
            return self._json(200, {"text": "  plain\n text  "})
        return self._json(200, {"text": " " + SPOKEN + "\n", "language": "en",
                                "segments": [{"id": 0, "no_speech_prob": 0.02, "avg_logprob": -0.2}]})

    def log_message(self, *args):
        pass


def make_tone(path, seconds, extra):
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", "sine=frequency=440:duration=%s" % seconds] + extra + [path]
    subprocess.run(cmd, check=True, stdin=subprocess.DEVNULL, timeout=60)
    with open(path, "rb") as f:
        return f.read()


class VoiceTestCase(DashboardTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.groq = ThreadingHTTPServer(("127.0.0.1", 0), MockGroq)
        start_thread_server(cls.groq)
        # snap ffmpeg can only see non-hidden paths under $HOME, so fixtures live in data/
        os.makedirs(os.path.join(server.BASE_DIR, "data"), mode=0o700, exist_ok=True)
        cls.fixdir = tempfile.mkdtemp(prefix="test-voice-fixtures-", dir=os.path.join(server.BASE_DIR, "data"))
        f = lambda name: os.path.join(cls.fixdir, name)  # noqa: E731
        cls.fixtures = {
            "wav": (make_tone(f("t.wav"), 2, ["-ar", "44100", "-ac", "2"]), "audio/wav"),
            "m4a": (make_tone(f("t.m4a"), 2, ["-c:a", "aac"]), "audio/mp4"),
            "ogg": (make_tone(f("t.ogg"), 2, ["-c:a", "libopus"]), "audio/ogg"),
            "webm": (make_tone(f("t.webm"), 2, ["-c:a", "libopus"]), "audio/webm;codecs=opus"),
            "mp3": (make_tone(f("t.mp3"), 2, ["-c:a", "libmp3lame"]), "audio/mpeg"),
            "3gp": (make_tone(f("t.3gp"), 2, ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb",
                                               "-b:a", "12.2k"]), "video/3gpp"),
            "amr": (make_tone(f("t.amr"), 2, ["-ar", "8000", "-ac", "1", "-c:a", "libopencore_amrnb",
                                               "-b:a", "12.2k"]), "audio/amr"),
            "unlabeled": (make_tone(f("u.m4a"), 2, ["-c:a", "aac"]), "application/octet-stream"),
        }
        cls.long_wav = make_tone(f("long.wav"), 40, ["-ar", "8000", "-ac", "1"])
        cls.short_wav = make_tone(f("short.wav"), 0.1, ["-ar", "16000", "-ac", "1"])

    @classmethod
    def tearDownClass(cls):
        cls.groq.shutdown()
        shutil.rmtree(cls.fixdir, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        MockGroq.mode = "ok"
        MockGroq.calls = []
        self.dir = tempfile.mkdtemp(prefix="voice-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.tmp_dir = tempfile.mkdtemp(prefix="test-voice-tmp-", dir=os.path.join(server.BASE_DIR, "data"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, True)
        self.env_path = os.path.join(self.dir, ".env")
        self.write_env("GROQ_API_KEY=%s\n" % FAKE_KEY)
        self.state_path = os.path.join(self.dir, "voice-state.json")
        self.usage_path = os.path.join(self.dir, "voice-usage.jsonl")
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in vm.KEY_NAMES:
            os.environ.pop(name, None)
        self.logs = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = lambda record: self.logs.append(record.getMessage())
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(old)))

    def write_env(self, text, mode=0o600):
        with open(self.env_path, "w") as f:
            f.write(text)
        os.chmod(self.env_path, mode)

    def vcfg(self, **over):
        cfg = json.loads(json.dumps(vm.DEFAULT_VOICE_CONFIG))
        cfg.update(base_url="http://127.0.0.1:%d/openai/v1" % self.groq.server_port, env_file=self.env_path,
                   state_file=self.state_path, usage_log=self.usage_path, tmp_dir=self.tmp_dir,
                   min_interval_seconds=0, timeout_seconds=3, fallback_enabled=False)
        cfg.update(over)
        vm.validate_voice_config(cfg)
        return cfg

    def serve_voice(self, **over):
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        cfg["web_dir"], cfg["media_dir"] = self.web, self.media
        cfg["voice"] = self.vcfg(**over)
        server.validate_config(cfg)
        return self.serve(server.App(cfg))

    def upload(self, port, data, ctype="audio/wav", headers=None):
        h = {"Content-Type": ctype}
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("POST", "/api/transcribe", body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp, json.loads(raw)
        except ValueError:
            return resp, raw

    def attempts(self):
        try:
            with open(self.state_path) as f:
                return len(json.load(f)["attempts"])
        except FileNotFoundError:
            return 0

    def usage(self):
        try:
            with open(self.usage_path) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    @staticmethod
    def sent_wav(call):
        body = call["body"]
        start = body.index(b"RIFF")
        header = body[start:start + 44]
        channels, rate = struct.unpack("<HI", header[22:28])
        return channels, rate


class FormatTests(VoiceTestCase):
    def test_common_phone_and_browser_formats(self):
        port = self.serve_voice()
        for name, (data, ctype) in self.fixtures.items():
            with self.subTest(fmt=name):
                MockGroq.calls = []
                resp, body = self.upload(port, data, ctype)
                self.assertEqual(resp.status, 200, body)
                self.assertEqual(body["text"], SPOKEN)
                self.assertFalse(body["no_speech"])
                self.assertAlmostEqual(body["seconds"], 2.0, delta=0.3)
                self.assertEqual(self.sent_wav(MockGroq.calls[0]), (1, 16000))
        self.assertEqual(os.listdir(self.tmp_dir), [])  # temporary files removed

    def test_request_to_groq(self):
        port = self.serve_voice()
        self.upload(port, self.fixtures["wav"][0])
        call = MockGroq.calls[0]
        self.assertEqual(call["path"], "/openai/v1/audio/transcriptions")
        self.assertEqual(call["auth"], "Bearer " + FAKE_KEY)
        self.assertTrue(call["ctype"].startswith("multipart/form-data; boundary="))
        body = call["body"]
        for part in (b'name="model"\r\n\r\nwhisper-large-v3-turbo\r\n', b'name="response_format"\r\n\r\nverbose_json\r\n',
                     b'name="temperature"\r\n\r\n0\r\n', b'filename="audio.wav"\r\nContent-Type: audio/wav\r\n'):
            self.assertIn(part, body)
        self.assertNotIn(b'name="language"', body)

    def test_language_hint_and_trim_to_30s(self):
        port = self.serve_voice(language="es")
        resp, body = self.upload(port, self.long_wav)
        self.assertEqual(resp.status, 200)
        self.assertIn(b'name="language"\r\n\r\nes\r\n', MockGroq.calls[0]["body"])
        self.assertAlmostEqual(body["seconds"], 30.0, delta=0.1)
        self.assertAlmostEqual(self.usage()[0]["audio_seconds"], 30.0, delta=0.1)

    def test_rejected_input_uses_no_quota(self):
        port = self.serve_voice(max_upload_bytes=200000)
        cases = [(self.short_wav, "audio/wav", 400, "too_short"),
                 (b"#EXTM3U\n#EXTINF:1,\nfile:/etc/passwd\n", "audio/x-mpegurl", 415, "unsupported_audio"),
                 (b"hello this is plain text pretending to be audio", "audio/wav", 415, "unsupported_audio"),
                 (b"RIFF\x00\x00\x00\x00WAVEjunkjunkjunkjunk" * 3, "audio/wav", 400, "bad_audio"),
                 (self.long_wav, "audio/wav", 413, "too_large"),
                 (self.fixtures["wav"][0], "text/plain", 415, "unsupported_media_type")]
        for data, ctype, status, code in cases:
            with self.subTest(code=code, ctype=ctype):
                resp, body = self.upload(port, data, ctype)
                self.assertEqual((resp.status, body["error"]["code"]), (status, code))
        self.assertEqual(MockGroq.calls, [])
        self.assertEqual(self.attempts(), 0)
        self.assertEqual(os.listdir(self.tmp_dir), [])

    def test_missing_length_and_chunked(self):
        port = self.serve_voice()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/transcribe")
        conn.putheader("Content-Type", "audio/wav")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()


class SpeechResultTests(VoiceTestCase):
    def test_silence_hallucination_is_dropped(self):
        MockGroq.mode = "silence"
        port = self.serve_voice()
        resp, body = self.upload(port, self.fixtures["wav"][0])
        self.assertEqual(resp.status, 200)
        self.assertEqual((body["text"], body["no_speech"]), ("", True))
        self.assertEqual(self.usage()[-1]["outcome"], "no_speech")

    def test_empty_and_text_only_responses(self):
        port = self.serve_voice()
        MockGroq.mode = "empty"
        self.assertTrue(self.upload(port, self.fixtures["wav"][0])[1]["no_speech"])
        MockGroq.mode = "dot"
        body = self.upload(port, self.fixtures["wav"][0])[1]
        self.assertEqual((body["text"], body["no_speech"]), ("", True))
        MockGroq.mode = "textonly"
        body = self.upload(port, self.fixtures["wav"][0])[1]
        self.assertEqual((body["text"], body["no_speech"]), ("plain text", False))


class KeyTests(VoiceTestCase):
    def test_alias_and_priority(self):
        self.write_env("GROQ=gsk_ALIASKEY123456\n")
        port = self.serve_voice()
        self.assertEqual(self.upload(port, self.fixtures["wav"][0])[0].status, 200)
        self.assertEqual(MockGroq.calls[-1]["auth"], "Bearer gsk_ALIASKEY123456")
        self.write_env("GROQ=gsk_ALIASKEY123456\nGROQ_API_KEY=%s\n" % FAKE_KEY)
        self.upload(port, self.fixtures["wav"][0])
        self.assertEqual(MockGroq.calls[-1]["auth"], "Bearer " + FAKE_KEY)

    def test_not_configured_and_insecure(self):
        os.remove(self.env_path)
        port = self.serve_voice()
        resp, body = self.upload(port, self.fixtures["wav"][0])
        self.assertEqual((resp.status, body["error"]["code"]), (503, "not_configured"))
        status = json.loads(self.request(port, "/api/voice/status")[1])
        self.assertFalse(status["configured"])
        self.write_env("GROQ_API_KEY=%s\n" % FAKE_KEY, mode=0o640)
        self.assertEqual(self.upload(port, self.fixtures["wav"][0])[1]["error"]["code"], "not_configured")
        self.assertEqual(MockGroq.calls, [])
        self.assertEqual(self.attempts(), 0)

    def test_status_has_no_key(self):
        port = self.serve_voice()
        raw = self.request(port, "/api/voice/status")[1]
        status = json.loads(raw)
        self.assertTrue(status["configured"])
        self.assertEqual(status["remaining"], {"hour": 60, "day": 300})
        self.assertNotIn(FAKE_KEY.encode(), raw)


class SecurityTests(VoiceTestCase):
    def test_cross_origin_rejected(self):
        port = self.serve_voice()
        data = self.fixtures["wav"][0]
        for headers in ({"Origin": "http://evil.example"}, {"Host": "evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                resp, body = self.upload(port, data, headers=headers)
                self.assertEqual(resp.status, 403)
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(MockGroq.calls, [])

    def test_no_secrets_or_content_in_logs_and_usage(self):
        port = self.serve_voice()
        self.upload(port, self.fixtures["wav"][0])
        MockGroq.mode = "401"
        self.upload(port, self.fixtures["wav"][0])
        blob = "\n".join(self.logs)
        for secret in (FAKE_KEY, SPOKEN, "SECRET-UPSTREAM-DETAIL"):
            self.assertNotIn(secret, blob)
        with open(self.usage_path) as f:
            usage = f.read()
        self.assertNotIn(SPOKEN, usage)
        self.assertNotIn(FAKE_KEY, usage)
        self.assertIn('"text_chars"', usage)
        self.assertEqual(oct(os.stat(self.usage_path).st_mode & 0o777), "0o600")


class UpstreamAndLimitTests(VoiceTestCase):
    def test_upstream_errors_count_and_stay_generic(self):
        data = self.fixtures["wav"][0]
        cases = [("401", 503, "key_rejected"), ("500", 502, "upstream_error"),
                 ("garbage", 502, "upstream_error"), ("429", 429, "provider_busy")]
        for mode, status, code in cases:
            with self.subTest(mode=mode):
                MockGroq.mode = mode
                before = self.attempts()
                port = self.serve_voice()
                resp, body = self.upload(port, data)
                self.assertEqual((resp.status, body["error"]["code"]), (status, code))
                self.assertNotIn("SECRET-UPSTREAM-DETAIL", json.dumps(body))
                self.assertEqual(self.attempts(), before + 1)
                if mode == "429":
                    self.assertEqual(resp.getheader("Retry-After"), "12")

    def test_timeout(self):
        MockGroq.mode = "slow"
        port = self.serve_voice(timeout_seconds=1)
        resp, body = self.upload(port, self.fixtures["wav"][0])
        self.assertEqual((resp.status, body["error"]["code"]), (504, "timeout"))
        self.assertEqual(self.attempts(), 1)

    def test_one_at_a_time(self):
        MockGroq.mode = "slow1"
        port = self.serve_voice()
        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(self.upload(port, self.fixtures["wav"][0]))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(r[0].status for r in results), [200, 429])
        self.assertEqual(len(MockGroq.calls), 1)
        self.assertEqual(self.attempts(), 1)

    def test_interval_and_hourly_limits(self):
        port = self.serve_voice(min_interval_seconds=5)
        self.assertEqual(self.upload(port, self.fixtures["wav"][0])[0].status, 200)
        resp, body = self.upload(port, self.fixtures["wav"][0])
        self.assertEqual((resp.status, body["error"]["code"]), (429, "rate_limited"))
        self.assertTrue(1 <= body["error"]["retry_after"] <= 5)
        port = self.serve_voice(max_per_hour=1)
        resp, body = self.upload(port, self.fixtures["wav"][0])
        self.assertEqual((resp.status, body["error"]["code"]), (429, "hourly_limit"))
        self.assertIn("recordings", body["error"]["message"])
        self.assertEqual(len(MockGroq.calls), 1)

    def test_estimated_cost_uses_10s_minimum(self):
        port = self.serve_voice()
        self.upload(port, self.fixtures["wav"][0])
        self.assertAlmostEqual(self.usage()[0]["estimated_cost_usd"], 10 / 3600 * 0.04, places=8)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        d = json.loads(json.dumps(vm.DEFAULT_VOICE_CONFIG))
        vm.validate_voice_config(d)
        self.assertEqual((d["model"], d["base_url"]), ("whisper-large-v3-turbo", "https://api.groq.com/openai/v1"))
        for over in ({"base_url": "http://api.groq.com/v1"}, {"language": "english"}, {"max_audio_seconds": 0},
                     {"no_speech_threshold": 2}, {"enabled": 1}, {"price_per_hour_usd": -1}):
            with self.subTest(over=over):
                with self.assertRaises(ValueError):
                    vm.validate_voice_config(dict(d, **over))

    def test_sniffing(self):
        self.assertEqual(vm.sniff_audio(b"RIFF\x00\x00\x00\x00WAVEfmt "), "wav")
        self.assertEqual(vm.sniff_audio(b"\x00\x00\x00\x18ftypM4A \x00\x00"), "mp4")
        self.assertEqual(vm.sniff_audio(b"#!AMR\n" + b"\x00" * 10), "amr")
        self.assertIsNone(vm.sniff_audio(b"#EXTM3U\nhttp://x/y\n"))
        self.assertIsNone(vm.sniff_audio(b"short"))


if __name__ == "__main__":
    unittest.main()
