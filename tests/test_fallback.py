"""Tests for the local-model fallback (fallback.py + its use in assistant.py and voice.py).

Policy under test: the local model is used ONLY when the cloud sends no response headers in time
(or the connection fails before them). Any HTTP status, or a failure after headers, never falls
back. Everything runs against local mock servers with dummy keys; nothing leaves this machine.
"""
import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fallback as fb  # noqa: E402
import test_assistant as ta  # noqa: E402  (module import: avoids re-collecting its tests)
import test_voice as tv  # noqa: E402
import util  # noqa: E402
from test_server import start_thread_server  # noqa: E402

NO_RESPONSE = 0.5


class BlackHole:
    """Accepts TCP connections and never sends a byte."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._conns = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._conns.append(conn)

    def close(self):
        for c in self._conns:
            c.close()
        self.sock.close()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StallAfterHeaders(BaseHTTPRequestHandler):
    """Cloud that answers with headers, then stalls mid-body."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "500")
        self.end_headers()
        self.wfile.write(b'{"choices": [')
        self.wfile.flush()
        time.sleep(4)

    def log_message(self, *args):
        pass


class MockLocal(BaseHTTPRequestHandler):
    """Stands in for llama-server (/v1/chat/completions) and whisper-server (/inference)."""
    mode = "ok"
    calls = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        MockLocal.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if MockLocal.mode == "500":
            out, status = b'{"error": "boom"}', 500
        elif MockLocal.mode == "slow":
            time.sleep(8)
            out, status = b"{}", 200
        elif MockLocal.mode == "markdown":
            out, status = json.dumps({"choices": [{"message": {"role": "assistant",
                                                               "content": "Es **Mexicali**: $30 \\times 2$ = 60."},
                                                   "finish_reason": "stop"}]}).encode(), 200
        elif self.path.endswith("/inference"):
            out, status = json.dumps({"text": " Hola, que tal.", "language": "es",
                                      "segments": [{"id": 0, "no_speech_prob": 0.01}]}).encode(), 200
        else:
            out, status = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Local answer."},
                                                   "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 20, "completion_tokens": 3,
                                                "total_tokens": 23}}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        try:
            self.wfile.write(out)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def _servers(cls):
    cls.hole = BlackHole()
    cls.local = ThreadingHTTPServer(("127.0.0.1", 0), MockLocal)
    start_thread_server(cls.local)
    cls.stall = ThreadingHTTPServer(("127.0.0.1", 0), StallAfterHeaders)
    start_thread_server(cls.stall)


def _close(cls):
    cls.hole.close()
    cls.local.shutdown()
    cls.stall.shutdown()


# --- fallback.py units ---------------------------------------------------------------------

class HeaderDeadlineOpenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _servers(cls)

    @classmethod
    def tearDownClass(cls):
        _close(cls)

    def opener(self):
        return fb.HeaderDeadlineOpener(urllib.request.build_opener())

    def req(self, port, path="/x"):
        return urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=b"{}", method="POST")

    def test_black_hole_hits_header_deadline(self):
        t0 = time.monotonic()
        with self.assertRaises(fb.NoResponse) as cm:
            self.opener().open(self.req(self.hole.port), NO_RESPONSE, 2)
        self.assertEqual(cm.exception.reason, "deadline")
        self.assertLess(time.monotonic() - t0, NO_RESPONSE + 0.4)

    def test_refused_connection_is_immediate(self):
        t0 = time.monotonic()
        with self.assertRaises(fb.NoResponse) as cm:
            self.opener().open(self.req(closed_port()), 5, 5)
        self.assertEqual(cm.exception.reason, "connect")
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_http_status_propagates_unchanged(self):
        MockLocal.mode = "500"
        self.addCleanup(setattr, MockLocal, "mode", "ok")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.opener().open(self.req(self.local.server_port), 2, 2)
        self.assertEqual(cm.exception.code, 500)
        cm.exception.close()

    def test_headers_returned_before_body(self):
        resp = self.opener().open(self.req(self.stall.server_port), 2, 6)
        self.assertEqual(resp.status, 200)
        resp.close()

    def test_one_straggler_then_immediate_no_response(self):
        o = self.opener()
        with self.assertRaises(fb.NoResponse):
            o.open(self.req(self.hole.port), 0.2, 1.5)  # worker keeps waiting up to 1.5 s
        t0 = time.monotonic()
        with self.assertRaises(fb.NoResponse) as cm:
            o.open(self.req(self.local.server_port), 2, 2)
        self.assertEqual(cm.exception.reason, "straggler")
        self.assertLess(time.monotonic() - t0, 0.1)
        time.sleep(1.6)  # straggler gone: a healthy server works again
        resp = o.open(self.req(self.local.server_port), 2, 2)
        self.assertEqual(resp.status, 200)
        resp.close()


class PlainTextTests(unittest.TestCase):
    def test_markdown_and_latex_become_plain_text(self):
        cases = {
            "La capital es **Mexicali**.": "La capital es Mexicali.",
            "*Cien años de soledad* de García Márquez": "Cien años de soledad de García Márquez",
            "Use $F = (C \\times 1.8) + 32$.": "Use F = (C × 1.8) + 32.",
            "$\\frac{9}{5}$ y $\\frac{a+b}{2}$ y 100^\\circ": "9/5 y (a+b)/2 y 100°",
            "1. **Rutina:** dormir\n* uno\n- dos": "1. Rutina: dormir\n• uno\n• dos",
            "## Título\n\n\n\nTexto `x`": "Título\n\nTexto x",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(fb.plain_text(raw), want)

    def test_ordinary_text_is_untouched(self):
        for raw in ("Cuesta $5 y $10.", "Precio: $20", "3 * 4 = 12", "2 - 1 = 1", "A spider has eight legs."):
            with self.subTest(raw=raw):
                self.assertEqual(fb.plain_text(raw), raw)


class CloudGateTests(unittest.TestCase):
    def test_cooldown_then_single_probe(self):
        now = [100.0]
        g = fb.CloudGate(60, clock=lambda: now[0])
        self.assertTrue(g.begin())
        g.finish(False)  # no response: cooldown starts
        self.assertTrue(g.cooling_down())
        self.assertFalse(g.begin())
        now[0] += 61
        self.assertTrue(g.begin())   # the one probe
        self.assertFalse(g.begin())  # concurrent callers stay local
        g.finish(False)              # probe failed: cooldown restarts
        self.assertFalse(g.begin())
        now[0] += 61
        self.assertTrue(g.begin())
        g.finish(True)               # probe succeeded: back to normal
        self.assertTrue(g.begin())
        g.finish(True)
        self.assertTrue(g.begin())

    def test_local_lock_wait_is_bounded(self):
        self.assertTrue(fb.LOCAL_LOCK.acquire(False))
        try:
            t0 = time.monotonic()
            self.assertFalse(fb.acquire_local(time.monotonic() + 0.3))
            self.assertLess(time.monotonic() - t0, 0.6)
        finally:
            fb.LOCAL_LOCK.release()


# --- assistant.py integration --------------------------------------------------------------

class AssistantFallbackTests(ta.AssistantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _servers(cls)

    @classmethod
    def tearDownClass(cls):
        _close(cls)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        MockLocal.mode = "ok"
        MockLocal.calls = []

    def ai(self, cloud_port=None, **over):
        cfg = dict(fallback_enabled=True, no_response_seconds=NO_RESPONSE, cloud_cooldown_seconds=60,
                   fallback_base_url="http://127.0.0.1:%d/v1" % self.local.server_port,
                   fallback_timeout_seconds=5)
        if cloud_port is not None:
            cfg["base_url"] = "http://127.0.0.1:%d/v1" % cloud_port
        cfg.update(over)
        return self.direct(util.now_ms, **cfg)

    def test_no_response_falls_back_to_local(self):
        ai = self.ai(self.hole.port)
        t0 = time.monotonic()
        status, body = ai.ask("Que hora es en Tokio?")
        self.assertLess(time.monotonic() - t0, NO_RESPONSE + 1.0)
        self.assertEqual(status, 200)
        self.assertEqual((body["answer"], body["source"]), ("Local answer.", "local"))
        call = MockLocal.calls[-1]
        self.assertIsNone(call["auth"], "the cloud key must never be sent to the local server")
        sent = json.loads(call["body"])
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIn("same language", sent["messages"][0]["content"])
        self.assertNotIn("no access", sent["messages"][0]["content"], "small models refuse facts because of it")
        self.assertEqual(sent["max_tokens"], 120)
        self.assertEqual(sent["temperature"], ta.am.LOCAL_TEMPERATURE)
        entry = self.usage_lines()[-1]
        self.assertEqual((entry["source"], entry["outcome"], entry["cloud"]), ("local", "success_local", "no_response"))
        self.assertIsNone(entry["estimated_cost_usd"])
        self.assertIn("api_wait_ms", entry)
        self.assertIn("local_ms", entry)

    def test_local_answer_is_plain_text(self):
        MockLocal.mode = "markdown"
        status, body = self.ai(closed_port()).ask("hi")
        self.assertEqual(body["answer"], "Es Mexicali: 30 × 2 = 60.")

    def test_refused_connection_falls_back_immediately(self):
        t0 = time.monotonic()
        status, body = self.ai(closed_port()).ask("hi")
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual((status, body["source"]), (200, "local"))

    def test_http_errors_never_fall_back(self):
        expected = {"401": "key_rejected", "402": "quota_exhausted", "403": "unavailable",
                    "429": "provider_busy", "500": "upstream_error"}
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                ta.MockGateway.mode = mode
                ai = self.ai()
                ai.store.clear_block()
                status, body = ai.ask("hi")
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"]["code"], code)
                self.assertEqual(MockLocal.calls, [])
                self.assertFalse(ai.gate.cooling_down())

    def test_failure_after_headers_never_falls_back(self):
        status, body = self.ai(self.stall.server_port, timeout_seconds=2).ask("hi")
        self.assertEqual((status, body["error"]["code"]), (504, "timeout"))
        self.assertEqual(MockLocal.calls, [])

    def test_cloud_ok_reports_api_source(self):
        status, body = self.ai().ask("hi")
        self.assertEqual((status, body["source"]), (200, "api"))
        self.assertEqual(MockLocal.calls, [])

    def test_cooldown_skips_cloud_then_probes_once(self):
        ai = self.ai(self.hole.port, cloud_cooldown_seconds=0.6, timeout_seconds=1)
        before = self.hole.accepted
        self.assertEqual(ai.ask("one")[1]["source"], "local")
        time.sleep(0.1)
        self.assertEqual(self.hole.accepted, before + 1)
        status, body = ai.ask("two")  # inside the cooldown: cloud not contacted at all
        self.assertEqual(body["source"], "local")
        self.assertEqual(self.usage_lines()[-1]["cloud"], "skipped")
        self.assertEqual(self.hole.accepted, before + 1)
        time.sleep(1.2)  # cooldown over and the abandoned worker has timed out
        self.assertEqual(ai.ask("three")[1]["source"], "local")
        time.sleep(0.1)
        self.assertEqual(self.hole.accepted, before + 2, "exactly one probe after the cooldown")

    def test_local_down_during_cooldown_has_its_own_message(self):
        ai = self.ai(self.hole.port, fallback_base_url="http://127.0.0.1:%d/v1" % closed_port())
        status, body = ai.ask("one")
        self.assertEqual((status, body["error"]["code"]), (503, "local_unavailable"))
        self.assertIn("didn't respond", body["error"]["message"])
        status, body = ai.ask("two")
        self.assertIn("unreachable a moment ago", body["error"]["message"])
        self.assertEqual(self.usage_lines()[-1]["outcome"], "local_error")

    def test_local_busy_respects_budget(self):
        ai = self.ai(closed_port(), fallback_timeout_seconds=1)
        self.assertTrue(fb.LOCAL_LOCK.acquire(False))
        try:
            t0 = time.monotonic()
            status, body = ai.ask("hi")
            self.assertLess(time.monotonic() - t0, 1.6)
        finally:
            fb.LOCAL_LOCK.release()
        self.assertEqual(body["error"]["code"], "local_unavailable")
        self.assertEqual(self.usage_lines()[-1]["local_error"], "local_busy")

    def test_fallback_url_must_be_loopback(self):
        with self.assertRaises(ValueError):
            self.ai(fallback_base_url="http://192.168.1.20:8091/v1")


# --- voice.py integration ------------------------------------------------------------------

class VoiceFallbackTests(tv.VoiceTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _servers(cls)

    @classmethod
    def tearDownClass(cls):
        _close(cls)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        MockLocal.mode = "ok"
        MockLocal.calls = []

    def port(self, cloud_port=None, **over):
        cfg = dict(fallback_enabled=True, no_response_seconds=NO_RESPONSE,
                   fallback_base_url="http://127.0.0.1:%d" % self.local.server_port)
        if cloud_port is not None:
            cfg["base_url"] = "http://127.0.0.1:%d/openai/v1" % cloud_port
        cfg.update(over)
        return self.serve_voice(**cfg)

    def test_no_response_transcribes_locally(self):
        resp, body = self.upload(self.port(self.hole.port), self.fixtures["wav"][0])
        self.assertEqual(resp.status, 200)
        self.assertEqual((body["text"], body["source"]), ("Hola, que tal.", "local"))
        call = MockLocal.calls[-1]
        self.assertEqual(call["path"], "/inference")
        self.assertIsNone(call["auth"])
        self.assertIn(b'name="language"\r\n\r\nauto', call["body"])
        self.assertIn(b"RIFF", call["body"])
        entry = self.usage()[-1]
        self.assertEqual((entry["source"], entry["outcome"]), ("local", "success_local"))
        for k in ("convert_ms", "api_wait_ms", "local_ms"):
            self.assertIn(k, entry)

    def test_http_error_never_falls_back(self):
        tv.MockGroq.mode = "500"
        resp, body = self.upload(self.port(), self.fixtures["wav"][0])
        self.assertEqual(body["error"]["code"], "upstream_error")
        self.assertEqual(MockLocal.calls, [])

    def test_cloud_ok_reports_api_source(self):
        resp, body = self.upload(self.port(), self.fixtures["wav"][0])
        self.assertEqual((resp.status, body["source"]), (200, "api"))

    def test_total_cap_bounds_slow_local(self):
        MockLocal.mode = "slow"
        t0 = time.monotonic()
        resp, body = self.upload(self.port(self.hole.port, total_timeout_seconds=5, fallback_timeout_seconds=20),
                                 self.fixtures["wav"][0])
        self.assertLess(time.monotonic() - t0, 6.5)
        self.assertEqual(body["error"]["code"], "local_unavailable")


if __name__ == "__main__":
    unittest.main()
