"""Tests for the AI assistant (assistant.py + /api/assistant).

All upstream traffic goes to a local mock of the AI Gateway; nothing leaves this machine.
Run: python3 -m unittest discover -s tests -v
"""
import http.client
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant as am  # noqa: E402
import server  # noqa: E402
from test_server import DashboardTestCase, start_thread_server  # noqa: E402

FAKE_KEY = "FAKEKEY-abc123XYZ456"
TZ = ZoneInfo("America/Tijuana")


class MockGateway(BaseHTTPRequestHandler):
    mode = "ok"
    calls = []
    other_hits = []
    answer = "Four."

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

    def do_GET(self):
        MockGateway.other_hits.append(self.path)
        self._json(200, {"hit": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        MockGateway.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        mode = MockGateway.mode
        err = {"error": {"message": "SECRET-UPSTREAM-DETAIL", "type": "upstream_type"}}
        if mode == "401":
            return self._json(401, err)
        if mode == "402":
            return self._json(402, err)
        if mode == "403":
            return self._json(403, err)
        if mode == "429":
            return self._json(429, {"error": {"message": "SECRET-UPSTREAM-DETAIL", "type": "rate_limit_exceeded"}},
                              {"retry-after": "17"})
        if mode == "500":
            return self._json(500, err)
        if mode == "400":
            return self._json(400, err)
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/v1/elsewhere" % self.server.server_port)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode in ("slow", "slow1"):
            time.sleep(2.5 if mode == "slow" else 1.0)
        if mode == "garbage":
            raw = b"<html>nope</html>"
        elif mode == "big":
            raw = b'{"x":"' + b"a" * 200000 + b'"}'
        else:
            content = {"empty": "   ", "xss": "<script>alert(1)</script>"}.get(mode, MockGateway.answer)
            raw = json.dumps({
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "length" if mode == "length" else "stop"}],
                "usage": {"prompt_tokens": 61, "completion_tokens": 7, "total_tokens": 68},
            }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


class AssistantTestCase(DashboardTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), MockGateway)
        start_thread_server(cls.gateway)

    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown()
        super().tearDownClass()

    def setUp(self):
        MockGateway.mode = "ok"
        MockGateway.calls = []
        MockGateway.other_hits = []
        self.dir = tempfile.mkdtemp(prefix="assist-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env_path = os.path.join(self.dir, ".env")
        self.write_key()
        self.state_path = os.path.join(self.dir, "data", "state.json")
        self.usage_path = os.path.join(self.dir, "data", "usage.jsonl")
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(am.KEY_ENV, None)
        self.logs = Capture()
        root = logging.getLogger()
        self._old_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.logs)
        self.addCleanup(lambda: (root.removeHandler(self.logs), root.setLevel(self._old_level)))

    # -- helpers --------------------------------------------------------------------------
    def write_key(self, key=FAKE_KEY, mode=0o600):
        with open(self.env_path, "w") as f:
            f.write("# private\nOTHER=1\n%s=%s\n" % (am.KEY_ENV, key))
        os.chmod(self.env_path, mode)

    def acfg(self, **over):
        cfg = json.loads(json.dumps(am.DEFAULT_ASSISTANT_CONFIG))
        cfg.update(base_url="http://127.0.0.1:%d/v1" % self.gateway.server_port, env_file=self.env_path,
                   state_file=self.state_path, usage_log=self.usage_path, min_interval_seconds=0,
                   timeout_seconds=2, fallback_enabled=False)
        cfg.update(over)
        am.validate_assistant_config(cfg)
        return cfg

    def make_ai_app(self, **over):
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        cfg["web_dir"], cfg["media_dir"] = self.web, self.media
        cfg["assistant"] = self.acfg(**over)
        server.validate_config(cfg)
        return server.App(cfg)

    def serve_ai(self, **over):
        return self.serve(self.make_ai_app(**over))

    def post(self, port, payload, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        conn.request("POST", "/api/assistant", body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            return resp, json.loads(data)
        except ValueError:
            return resp, data

    def state(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {"attempts": []}

    def usage_lines(self):
        try:
            with open(self.usage_path) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def direct(self, clock, **over):
        return am.Assistant(self.acfg(**over), TZ, self.dir, clock=clock)


class SuccessAndPayloadTests(AssistantTestCase):
    def test_success_payload_and_usage_log(self):
        port = self.serve_ai()
        resp, data = self.post(port, {"question": "What is 2+2?"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(data["ok"], True)
        self.assertEqual(data["answer"], "Four.")
        self.assertFalse(data["truncated"])
        self.assertEqual(data["remaining"], {"hour": 19, "day": 99})
        self.assertIn("no-store", resp.getheader("Cache-Control"))
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

        self.assertEqual(len(MockGateway.calls), 1)
        call = MockGateway.calls[0]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual(call["auth"], "Bearer " + FAKE_KEY)
        body = call["body"]
        self.assertEqual(set(body), {"model", "messages", "max_tokens", "stream", "reasoning"})
        self.assertEqual(body["model"], "google/gemini-2.5-flash-lite")
        self.assertEqual(body["max_tokens"], 300)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertEqual(body["messages"], [{"role": "system", "content": am.SYSTEM_PROMPT},
                                            {"role": "user", "content": "What is 2+2?"}])
        self.assertIn("120 words", am.SYSTEM_PROMPT)

        lines = self.usage_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["outcome"], "success")
        self.assertEqual((lines[0]["prompt_tokens"], lines[0]["completion_tokens"], lines[0]["total_tokens"]),
                         (61, 7, 68))
        self.assertIsNone(lines[0]["estimated_cost_usd"])
        with open(self.usage_path) as f:
            raw = f.read()
        for secret in (FAKE_KEY, "What is 2+2?", "Four."):
            self.assertNotIn(secret, raw)
        self.assertEqual(oct(os.stat(self.usage_path).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(self.state_path).st_mode & 0o777), "0o600")

    def test_estimated_cost_only_with_configured_prices(self):
        port = self.serve_ai(price_per_million_input_usd=0.1, price_per_million_output_usd=0.4)
        self.post(port, {"question": "hi"})
        self.assertAlmostEqual(self.usage_lines()[0]["estimated_cost_usd"], (61 * 0.1 + 7 * 0.4) / 1e6)

    def test_thinking_option_can_be_omitted(self):
        port = self.serve_ai(disable_thinking=False)
        self.post(port, {"question": "hi"})
        self.assertNotIn("reasoning", MockGateway.calls[0]["body"])

    def test_questions_are_independent(self):
        port = self.serve_ai()
        self.post(port, {"question": "first question"})
        MockGateway.answer = "second answer"
        try:
            self.post(port, {"question": "second question"})
        finally:
            MockGateway.answer = "Four."
        second = json.dumps(MockGateway.calls[1]["body"])
        self.assertEqual(len(MockGateway.calls[1]["body"]["messages"]), 2)
        self.assertNotIn("first question", second)
        self.assertNotIn("Four.", second)

    def test_truncated_and_html_passthrough(self):
        port = self.serve_ai()
        MockGateway.mode = "length"
        self.assertTrue(self.post(port, {"question": "a"})[1]["truncated"])
        MockGateway.mode = "xss"
        resp, data = self.post(port, {"question": "b"})
        self.assertIn("application/json", resp.getheader("Content-Type"))
        self.assertEqual(data["answer"], "<script>alert(1)</script>")  # rendered with textContent client-side

    def test_exactly_1000_chars_accepted(self):
        port = self.serve_ai()
        self.assertEqual(self.post(port, {"question": "x" * 1000})[0].status, 200)


class ValidationTests(AssistantTestCase):
    def test_bad_bodies_rejected_without_quota_or_upstream(self):
        port = self.serve_ai()
        bad = [b"not json", b"[]", b"{}", b'{"question": 5}', b'{"question": "hi", "model": "x"}',
               b'{"question": "   "}', json.dumps({"question": "x" * 1001}).encode(),
               b'{"question": "a\\u0000b"}', b"\xff\xfe", b'{"question": null}']
        for payload in bad:
            with self.subTest(payload=payload[:30]):
                resp, data = self.post(port, payload)
                self.assertEqual(resp.status, 400)
                self.assertFalse(data["ok"])
        self.assertEqual(MockGateway.calls, [])
        self.assertEqual(self.state()["attempts"], [])

    def test_transport_level_rejections(self):
        port = self.serve_ai()
        resp, _ = self.post(port, {"question": "hi"}, {"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        resp, _ = self.post(port, b"x" * 9000)
        self.assertEqual(resp.status, 413)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/assistant")
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 411)
        conn.close()
        # Chunked body over a raw socket: the server answers 411 and closes without reading it.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"POST /api/assistant HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\n10\r\n{\"question\":\"hi\"}\r\n0\r\n\r\n")
            reply = sock.recv(4096)
        self.assertTrue(reply.startswith(b"HTTP/1.1 411"), reply[:40])
        self.assertEqual(MockGateway.calls, [])
        self.assertEqual(self.state()["attempts"], [])

    def test_other_methods_and_routes(self):
        port = self.serve_ai()
        for method in ("PUT", "DELETE", "OPTIONS"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(method, "/api/assistant")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 405, method)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
            conn.close()
        resp, _ = self.request(port, "/api/assistant")  # GET
        self.assertEqual(resp.status, 404)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/time", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()


class OriginTests(AssistantTestCase):
    def test_cross_origin_and_rebinding_rejected(self):
        port = self.serve_ai(allowed_hosts=["dashboard.lan"])
        q = {"question": "hi"}
        same = "http://127.0.0.1:%d" % port
        cases = [({"Origin": "http://evil.example"}, 403), ({"Origin": "null"}, 403),
                 ({"Origin": "http://127.0.0.1:1"}, 403), ({"Host": "evil.example"}, 403),
                 ({"Host": "evil.example", "Origin": "http://evil.example"}, 403),
                 ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Sec-Fetch-Site": "same-site"}, 403),
                 ({"Origin": same}, 200), ({"Sec-Fetch-Site": "same-origin", "Origin": same}, 200),
                 ({"Host": "dashboard.lan:%d" % port}, 200), ({}, 200)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                resp, data = self.post(port, q, headers)
                self.assertEqual(resp.status, expected)
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(len(MockGateway.calls), 4)  # only the accepted ones reached the gateway

    def test_lan_only_client_check(self):
        for ip, ok in (("127.0.0.1", True), ("192.168.1.60", True), ("10.1.2.3", True), ("172.17.0.2", True),
                       ("::1", True), ("::ffff:192.168.1.5", True), ("fe80::1%eth0", True),
                       ("8.8.8.8", False), ("100.64.0.1", False), ("2606:4700:4700::1111", False),
                       ("junk", False)):
            self.assertEqual(am.is_lan_client(ip), ok, ip)


class KeyHandlingTests(AssistantTestCase):
    def test_not_configured(self):
        os.remove(self.env_path)
        port = self.serve_ai()
        resp, data = self.post(port, {"question": "hi"})
        self.assertEqual((resp.status, data["error"]["code"]), (503, "not_configured"))
        self.assertEqual(MockGateway.calls, [])
        self.assertEqual(self.state()["attempts"], [])
        status = json.loads(self.request(port, "/api/assistant/status")[1])
        self.assertFalse(status["configured"])
        self.assertEqual(status["setup_problem"], "not_configured")

    def test_insecure_or_malformed_key_file_refused(self):
        port = self.serve_ai()
        self.write_key(mode=0o644)
        self.assertEqual(self.post(port, {"question": "hi"})[1]["error"]["code"], "not_configured")
        status = json.loads(self.request(port, "/api/assistant/status")[1])
        self.assertEqual(status["setup_problem"], "insecure_file")
        self.write_key(key="bad key with spaces\r\nX-Evil: 1")
        self.assertEqual(self.post(port, {"question": "hi"})[0].status, 503)
        self.assertEqual(MockGateway.calls, [])
        self.write_key()  # fixed without restarting
        self.assertEqual(self.post(port, {"question": "hi"})[0].status, 200)

    def test_symlinked_key_file_refused(self):
        real = os.path.join(self.dir, "real.env")
        os.rename(self.env_path, real)
        os.symlink(real, self.env_path)
        self.assertEqual(am.load_api_key(self.env_path), (None, "insecure_file"))

    def test_environment_variable_wins(self):
        os.remove(self.env_path)
        os.environ[am.KEY_ENV] = "ENVKEY-123456789"
        port = self.serve_ai()
        self.assertEqual(self.post(port, {"question": "hi"})[0].status, 200)
        self.assertEqual(MockGateway.calls[0]["auth"], "Bearer ENVKEY-123456789")

    def test_status_never_contains_key(self):
        port = self.serve_ai()
        raw = self.request(port, "/api/assistant/status")[1]
        status = json.loads(raw)
        self.assertTrue(status["configured"])
        self.assertEqual(status["remaining"], {"hour": 20, "day": 100})
        self.assertNotIn(FAKE_KEY.encode(), raw)


class UpstreamErrorTests(AssistantTestCase):
    def check_safe(self, resp, data):
        text = json.dumps(data)
        for leak in ("SECRET-UPSTREAM-DETAIL", FAKE_KEY, "upstream_type", "Bearer"):
            self.assertNotIn(leak, text)
        self.assertNotIn("SECRET-UPSTREAM-DETAIL", str(resp.getheaders()))

    def test_error_mapping_counts_attempts_and_hides_details(self):
        cases = [("401", 503, "key_rejected"), ("403", 503, "unavailable"), ("500", 502, "upstream_error"),
                 ("400", 502, "upstream_error"), ("garbage", 502, "upstream_error"),
                 ("big", 502, "upstream_error"), ("empty", 502, "empty_answer")]
        for mode, status, code in cases:
            with self.subTest(mode=mode):
                shutil.rmtree(os.path.join(self.dir, "data"), ignore_errors=True)
                MockGateway.mode = mode
                MockGateway.calls = []
                port = self.serve_ai()
                resp, data = self.post(port, {"question": "hi"})
                self.assertEqual((resp.status, data["error"]["code"]), (status, code))
                self.check_safe(resp, data)
                self.assertEqual(len(MockGateway.calls), 1)  # no automatic retry
                self.assertEqual(len(self.state()["attempts"]), 1)  # failed attempts still count
                self.assertNotEqual(self.usage_lines()[-1]["outcome"], "success")

    def test_provider_rate_limit_reports_retry_time(self):
        MockGateway.mode = "429"
        port = self.serve_ai()
        resp, data = self.post(port, {"question": "hi"})
        self.assertEqual((resp.status, data["error"]["code"], data["error"]["retry_after"]), (429, "provider_busy", 17))
        self.assertEqual(resp.getheader("Retry-After"), "17")
        self.check_safe(resp, data)
        self.assertEqual(len(MockGateway.calls), 1)

    def test_exhausted_credits_stop_all_requests(self):
        MockGateway.mode = "402"
        port = self.serve_ai()
        resp, data = self.post(port, {"question": "hi"})
        self.assertEqual((resp.status, data["error"]["code"]), (503, "quota_exhausted"))
        self.check_safe(resp, data)
        MockGateway.mode = "ok"  # even if the provider recovers, requests stay blocked for the block window
        for _ in range(3):
            resp, data = self.post(port, {"question": "again"})
            self.assertEqual((resp.status, data["error"]["code"]), (503, "quota_exhausted"))
        self.assertEqual(len(MockGateway.calls), 1)
        self.assertEqual(len(self.state()["attempts"]), 1)
        status = json.loads(self.request(port, "/api/assistant/status")[1])
        self.assertTrue(status["blocked"])
        # a fresh process sees the same block (persisted), and replacing the key clears it
        self.assertEqual(self.make_ai_app().assistant.ask("hi")[1]["error"]["code"], "quota_exhausted")
        self.make_ai_app().assistant.store.clear_block()
        self.assertEqual(self.post(port, {"question": "now"})[0].status, 200)

    def test_timeout(self):
        MockGateway.mode = "slow"
        port = self.serve_ai(timeout_seconds=1)
        started = time.time()
        resp, data = self.post(port, {"question": "hi"})
        self.assertEqual((resp.status, data["error"]["code"]), (504, "timeout"))
        self.assertLess(time.time() - started, 2.4)
        self.assertEqual(len(self.state()["attempts"]), 1)
        self.assertEqual(self.usage_lines()[-1]["outcome"], "timeout")

    def test_redirects_are_not_followed(self):
        MockGateway.mode = "redirect"
        port = self.serve_ai()
        resp, data = self.post(port, {"question": "hi"})
        self.assertEqual(resp.status, 502)
        self.assertEqual(MockGateway.other_hits, [])
        self.assertEqual(len(MockGateway.calls), 1)
        self.check_safe(resp, data)

    def test_unreachable_gateway(self):
        port = self.serve_ai(base_url="http://127.0.0.1:1/v1")
        resp, data = self.post(port, {"question": "hi"})
        self.assertEqual((resp.status, data["error"]["code"]), (502, "network_error"))
        self.assertEqual(len(self.state()["attempts"]), 1)


class ConcurrencyAndLimitTests(AssistantTestCase):
    def test_only_one_in_flight_request(self):
        MockGateway.mode = "slow1"
        port = self.serve_ai(timeout_seconds=5)
        results = []
        barrier = threading.Barrier(2)

        def worker(i):
            barrier.wait()
            results.append(self.post(port, {"question": "q%d" % i}))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        statuses = sorted(r[0].status for r in results)
        self.assertEqual(statuses, [200, 429])
        busy = [r[1] for r in results if r[0].status == 429][0]
        self.assertEqual(busy["error"]["code"], "busy")
        self.assertEqual(len(MockGateway.calls), 1)
        self.assertEqual(len(self.state()["attempts"]), 1)  # the rejected one consumed no quota

    def test_store_is_safe_under_many_threads(self):
        store = am.UsageStore(self.state_path, self.usage_path, TZ)
        limits = {"min_interval_seconds": 0, "max_per_hour": 5, "max_per_day": 100}
        allowed = []
        barrier = threading.Barrier(20)

        def worker():
            barrier.wait()
            allowed.append(store.reserve(limits)[0])

        threads = [threading.Thread(target=worker) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(allowed), 5)
        self.assertEqual(len(self.state()["attempts"]), 5)

    def test_min_interval_and_retry_time(self):
        clock = [1_800_000_000_000]
        a = self.direct(lambda: clock[0], min_interval_seconds=10)
        self.assertEqual(a.ask("one")[0], 200)
        clock[0] += 3000
        status, body = a.ask("two")
        self.assertEqual((status, body["error"]["code"], body["error"]["retry_after"]), (429, "rate_limited", 7))
        self.assertIn("7 seconds", body["error"]["message"])
        self.assertEqual(len(MockGateway.calls), 1)
        clock[0] += 7000
        self.assertEqual(a.ask("three")[0], 200)

    def test_hourly_limit_rolls_forward(self):
        clock = [1_800_000_000_000]
        a = self.direct(lambda: clock[0], max_per_hour=3)
        for _ in range(3):
            self.assertEqual(a.ask("q")[0], 200)
            clock[0] += 60_000
        status, body = a.ask("q")
        self.assertEqual((status, body["error"]["code"]), (429, "hourly_limit"))
        self.assertEqual(body["error"]["retry_after"], 3600 - 180)  # oldest attempt ages out first
        self.assertEqual(len(MockGateway.calls), 3)
        clock[0] += (3600 - 180) * 1000
        self.assertEqual(a.ask("q")[0], 200)

    def test_daily_limit_resets_at_tijuana_midnight(self):
        def ms(*args):
            return int(datetime(*args, tzinfo=TZ).timestamp() * 1000)

        clock = [ms(2026, 10, 15, 20, 0, 0)]
        a = self.direct(lambda: clock[0], max_per_day=2)
        self.assertEqual(a.ask("q")[0], 200)
        clock[0] = ms(2026, 10, 15, 22, 0, 0)
        self.assertEqual(a.ask("q")[0], 200)
        clock[0] = ms(2026, 10, 15, 23, 0, 0)
        status, body = a.ask("q")
        self.assertEqual((status, body["error"]["code"]), (429, "daily_limit"))
        self.assertEqual(body["error"]["retry_after"], 3600)
        self.assertIn("midnight", body["error"]["message"])
        clock[0] = ms(2026, 10, 16, 0, 0, 1)
        self.assertEqual(a.ask("q")[0], 200)
        self.assertEqual(len(MockGateway.calls), 3)

    def test_counters_survive_restart(self):
        clock = [1_800_000_000_000]
        first = self.direct(lambda: clock[0], max_per_hour=2)
        self.assertEqual(first.ask("q")[0], 200)
        clock[0] += 1000
        self.assertEqual(first.ask("q")[0], 200)
        second = self.direct(lambda: clock[0], max_per_hour=2)  # "restarted" process, same store
        status, body = second.ask("q")
        self.assertEqual((status, body["error"]["code"]), (429, "hourly_limit"))
        self.assertEqual(second.status()["remaining"], {"hour": 0, "day": 98})

    def test_corrupt_state_fails_closed(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w") as f:
            f.write("{not json")
        a = self.direct(lambda: 1_800_000_000_000)
        status, body = a.ask("q")
        self.assertEqual((status, body["error"]["code"]), (503, "unavailable"))
        self.assertEqual(MockGateway.calls, [])


class SecretHygieneTests(AssistantTestCase):
    def test_key_and_content_never_logged_or_served(self):
        port = self.serve_ai()
        secret_q = "my private question zebra-4242"
        self.post(port, {"question": secret_q})
        MockGateway.mode = "401"
        self.post(port, {"question": secret_q + " again"})
        MockGateway.mode = "ok"
        self.request(port, "/api/assistant/status")
        blob = "\n".join(self.logs.lines)
        self.assertTrue(blob)  # logging was active
        for secret in (FAKE_KEY, "zebra-4242", "Four.", "SECRET-UPSTREAM-DETAIL"):
            self.assertNotIn(secret, blob)
        for path in ("/.env", "/..%2f.env", "/data/state.json", "/assistant.py", "/server.py"):
            self.assertEqual(self.request(port, path)[0].status, 404, path)

    def test_real_web_assets_have_no_secrets_and_no_env_exposure(self):
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        app = server.App(cfg)  # real web/ and media/ directories
        port = self.serve(app)
        for path in ("/.env", "/%2e%2e/.env", "/assistant.py", "/config.json", "/data/assistant-state.json"):
            self.assertEqual(self.request(port, path)[0].status, 404, path)
        for root in (app.web_root, app.modern_root):
            for dirpath, _, names in os.walk(root):
                for name in names:
                    with open(os.path.join(dirpath, name), "rb") as f:
                        content = f.read()
                    self.assertNotIn(b"AI_GATEWAY", content, name)
                    self.assertNotIn(b"Bearer", content, name)
                    if name.endswith(".js") and root == app.modern_root:
                        self.assertNotIn(b"innerHTML", content, name)  # modern UI builds DOM nodes only
        # The tablet (Chrome 50) gets the classic page.
        tablet = {"User-Agent": "Mozilla/5.0 (Linux; Android 4.4.2) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/50.0.2661.89 Safari/537.36"}
        html = self.request(port, "/", headers=tablet)[1]
        self.assertIn(b"view-assistant", html)
        self.assertIn(b"Each question starts fresh.", html)
        with open(os.path.join(app.web_root, "app.js")) as f:
            js = f.read()
        self.assertNotIn("fetch(", js)
        self.assertNotIn("innerHTML = data", js)


class ConfigTests(unittest.TestCase):
    def test_validation(self):
        base = json.loads(json.dumps(am.DEFAULT_ASSISTANT_CONFIG))
        am.validate_assistant_config(base)
        bad = [{"base_url": "http://example.com/v1"}, {"base_url": "ftp://x"}, {"model": "no-slash"},
               {"max_per_hour": 0}, {"max_output_tokens": 5}, {"timeout_seconds": 0},
               {"min_interval_seconds": -1}, {"allowed_hosts": "dashboard"}, {"enabled": "yes"},
               {"price_per_million_input_usd": -1}]
        for over in bad:
            with self.subTest(over=over):
                cfg = dict(base, **over)
                with self.assertRaises(ValueError):
                    am.validate_assistant_config(cfg)

    def test_unknown_assistant_key_rejected_and_defaults_match_spec(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"assistant": {"nope": 1}}, f)
        try:
            with self.assertRaises(server.ConfigError):
                server.load_config(path)
        finally:
            os.remove(path)
        d = am.DEFAULT_ASSISTANT_CONFIG
        self.assertEqual((d["min_interval_seconds"], d["max_per_hour"], d["max_per_day"]), (10, 20, 100))
        self.assertEqual((d["max_output_tokens"], d["max_question_chars"], d["timeout_seconds"]), (300, 1000, 30))
        self.assertEqual(d["model"], "google/gemini-2.5-flash-lite")
        self.assertEqual(d["base_url"], "https://ai-gateway.vercel.sh/v1")
        cfg = server.load_config(os.path.join(server.BASE_DIR, "config.example.json"))
        self.assertEqual(cfg["assistant"]["model"], "google/gemini-2.5-flash-lite")


if __name__ == "__main__":
    unittest.main()
