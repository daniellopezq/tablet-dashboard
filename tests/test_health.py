"""Tests for health.py (checks, Uptime Kuma parsing and client) and localai.py inventory.

Run: python3 -m unittest discover -s tests -v. Uses local mock servers only.
"""
import base64
import copy
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health  # noqa: E402
import localai  # noqa: E402
from test_server import start_thread_server  # noqa: E402

METRICS = """# HELP monitor_status Monitor Status (1 = UP, 0= DOWN, 2= PENDING, 3= MAINTENANCE)
# TYPE monitor_status gauge
monitor_status{monitor_name="My site",monitor_type="http",monitor_url="https://example.org",monitor_hostname="null",monitor_port="null"} 1
monitor_status{monitor_name="NAS \\"box\\"",monitor_type="ping",monitor_url="https://",monitor_hostname="192.168.1.5",monitor_port="null"} 0
monitor_response_time{monitor_name="My site",monitor_type="http",monitor_url="https://example.org",monitor_hostname="null",monitor_port="null"} 123
monitor_response_time{monitor_name="NAS \\"box\\"",monitor_type="ping",monitor_url="https://",monitor_hostname="192.168.1.5",monitor_port="null"} -1
"""
PAGE = {"config": {"slug": "home"}, "publicGroupList": [
    {"name": "Web", "monitorList": [{"id": 1, "name": "Blog", "type": "http"}, {"id": 2, "name": "Shop", "type": "http"}]}]}
BEATS = {"heartbeatList": {"1": [{"status": 0, "time": "t0", "ping": None}, {"status": 1, "time": "t1", "ping": 88, "msg": ""}],
                           "2": [{"status": 0, "time": "t1", "ping": None, "msg": "timeout"}]},
         "uptimeList": {"1_24": 0.9987, "2_24": 0.5}}


class MockKuma(BaseHTTPRequestHandler):
    pushes = []

    def do_GET(self):
        if self.path == "/metrics":
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(b":kuma-key-123456").decode():
                return self._send(401, b"unauthorized")
            return self._send(200, METRICS.encode())
        if self.path == "/api/status-page/home":
            return self._send(200, json.dumps(PAGE).encode())
        if self.path == "/api/status-page/heartbeat/home":
            return self._send(200, json.dumps(BEATS).encode())
        if self.path.startswith("/api/push/tok"):
            MockKuma.pushes.append(self.path)
            return self._send(200, b'{"ok":true}')
        return self._send(404, b"{}")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ParserTests(unittest.TestCase):
    def test_metrics(self):
        mons = {m["name"]: m for m in health.parse_kuma_metrics(METRICS)}
        self.assertEqual(mons["My site"]["status"], "up")
        self.assertEqual(mons["My site"]["ping_ms"], 123)
        self.assertEqual(mons["My site"]["target"], "https://example.org")
        self.assertEqual(mons['NAS "box"']["status"], "down")
        self.assertIsNone(mons['NAS "box"']["ping_ms"])
        self.assertEqual(mons['NAS "box"']["target"], "192.168.1.5")

    def test_status_page(self):
        mons = {m["name"]: m for m in health.parse_kuma_status_page(PAGE, BEATS)}
        self.assertEqual((mons["Blog"]["status"], mons["Blog"]["ping_ms"], mons["Blog"]["uptime_24h"]), ("up", 88, 99.87))
        self.assertEqual((mons["Shop"]["status"], mons["Shop"]["msg"]), ("down", "timeout"))
        self.assertEqual(health.parse_kuma_status_page({"publicGroupList": "junk"}, None), [])

    def test_config_validation(self):
        good = copy.deepcopy(health.DEFAULT_HEALTH_CONFIG)
        good["targets"] = [{"name": "Plex", "type": "tcp", "host": "127.0.0.1", "port": 32400},
                           {"name": "Web", "type": "http", "url": "http://192.168.1.2/"}]
        health.validate_health_config(good)
        for bad in ({"name": "x", "type": "tcp", "host": "bad host", "port": 1},
                    {"name": "x", "type": "tcp", "host": "h", "port": 70000},
                    {"name": "x", "type": "http", "url": "ftp://h/"},
                    {"name": "x", "type": "http", "url": "http://user:pw@h/"},
                    {"name": "x", "type": "smtp"},
                    {"name": "", "type": "tcp", "host": "h", "port": 1}):
            with self.subTest(bad=bad):
                cfg = copy.deepcopy(good)
                cfg["targets"] = [bad]
                with self.assertRaises(ValueError):
                    health.validate_health_config(cfg)
        dup = copy.deepcopy(good)
        dup["targets"].append(dict(good["targets"][0]))
        with self.assertRaises(ValueError):
            health.validate_health_config(dup)
        k = dict(health.DEFAULT_KUMA_CONFIG, enabled=True)
        with self.assertRaises(ValueError):
            health.validate_kuma_config(k)  # no base_url
        health.validate_kuma_config(dict(k, base_url="http://10.0.0.2:3001", status_page_slug="home"))


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kuma = ThreadingHTTPServer(("127.0.0.1", 0), MockKuma)
        start_thread_server(cls.kuma)
        cls.tmp = tempfile.mkdtemp(prefix="health-test-")
        cls.env = os.path.join(cls.tmp, ".env")

    @classmethod
    def tearDownClass(cls):
        cls.kuma.shutdown()
        cls.kuma.server_close()
        shutil.rmtree(cls.tmp)

    def kcfg(self, **over):
        k = dict(health.DEFAULT_KUMA_CONFIG, enabled=True, base_url="http://127.0.0.1:%d" % self.kuma.server_port,
                 status_page_slug="home", env_file=self.env)
        k.update(over)
        return k

    def write_env(self, text):
        with open(self.env, "w") as f:
            f.write(text)
        os.chmod(self.env, 0o600)

    def test_tcp(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        self.assertEqual(health.tcp_check("127.0.0.1", port, 1)[0], "ok")
        srv.close()
        self.assertEqual(health.tcp_check("127.0.0.1", port, 1)[0], "fail")

    def test_http_any_status_below_500_is_up(self):
        url = "http://127.0.0.1:%d/nope" % self.kuma.server_port
        self.assertEqual(health.http_check(url, 2)[0], "ok")  # 404 still means the server answers

    def test_status_page_client(self):
        r = health.KumaClient(self.kcfg(), "/").fetch()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "status_page")
        self.assertEqual([m["name"] for m in r["monitors"]], ["Blog", "Shop"])

    def test_metrics_client_with_key(self):
        self.write_env("KUMA_API=kuma-key-123456\n")
        r = health.KumaClient(self.kcfg(use_metrics=True), "/").fetch()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "metrics")
        self.write_env("KUMA_API=wrong-key-1234567\n")
        r = health.KumaClient(self.kcfg(use_metrics=True), "/").fetch()
        self.assertFalse(r["ok"])
        self.assertIn("401", r["error"])

    def test_metrics_key_alias(self):
        self.write_env("KUMA_API_KEY=kuma-key-123456\n")  # older name
        r = health.KumaClient(self.kcfg(use_metrics=True), "/").fetch()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["source"], "metrics")

    def test_push(self):
        MockKuma.pushes = []
        self.write_env("KUMA_PUSH_URL=http://127.0.0.1:%d/api/push/tok123?status=up&msg=OK&ping=\n"
                       % self.kuma.server_port)
        self.assertIsNone(health.KumaClient(self.kcfg(), "/").push(False, "problems: Weather"))
        self.assertEqual(len(MockKuma.pushes), 1)
        self.assertIn("status=down", MockKuma.pushes[0])
        self.assertIn("tok123", MockKuma.pushes[0])

    def test_checker_runs_and_keeps_history(self):
        builtin = mock.Mock(return_value=[{"id": "x", "name": "X", "group": "Dashboard", "state": "ok", "detail": ""}])
        hcfg = dict(health.DEFAULT_HEALTH_CONFIG, targets=[
            {"name": "Kuma port", "type": "tcp", "host": "127.0.0.1", "port": self.kuma.server_port}])
        hc = health.HealthChecker(hcfg, self.kcfg(), "/", builtin)
        hc.run_once()
        builtin.return_value = [{"id": "x", "name": "X", "group": "Dashboard", "state": "fail", "detail": "down"}]
        hc.run_once()
        snap = hc.snapshot()
        by = {c["id"]: c for c in snap["checks"]}
        self.assertEqual(by["x"]["history"], "of")
        self.assertEqual(by["target:Kuma port"]["state"], "ok")
        self.assertEqual(by["kuma:Shop"]["state"], "fail")
        self.assertEqual(snap["counts"]["fail"], 2)

    def test_builtin_failure_is_reported_not_raised(self):
        hc = health.HealthChecker(dict(health.DEFAULT_HEALTH_CONFIG), dict(health.DEFAULT_KUMA_CONFIG), "/",
                                  mock.Mock(side_effect=RuntimeError("boom")))
        with self.assertLogs("dashboard.health", "ERROR"):
            results = hc.run_once()
        self.assertEqual(results[0]["state"], "fail")


class LocalAITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="localai-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        models = os.path.join(self.tmp, "models")
        os.makedirs(models)
        for name, size in (("Small-Q4.gguf", 1000), ("Huge-Q8.gguf", 3000), ("ggml-base.bin", 500), ("notes.txt", 5)):
            with open(os.path.join(models, name), "wb") as f:
                f.write(b"\0" * size)
        with open(os.path.join(models, "SHA256SUMS"), "w") as f:
            f.write("abc  Small-Q4.gguf\n")
        os.symlink("/etc/passwd", os.path.join(models, "evil.gguf"))
        for d, binary in (("llama-cpu", "llama-server"), ("whisper-vulkan", "whisper-server"), ("whisper-bad", None)):
            os.makedirs(os.path.join(self.tmp, "build", d, "bin"))
            if binary:
                p = os.path.join(self.tmp, "build", d, "bin", binary)
                open(p, "w").close()
                os.chmod(p, 0o755)
        self.cfg = dict(localai.DEFAULT_LOCAL_AI_CONFIG, dir=self.tmp, llm_model="Small-Q4.gguf", stt_backend="vulkan")
        self.la = localai.LocalAI(self.cfg, self.tmp, None, None)

    def test_inventory(self):
        m = self.la.models()
        self.assertEqual([x["name"] for x in m["llm"]], ["Huge-Q8.gguf", "Small-Q4.gguf"])  # no symlinks
        self.assertEqual([x["name"] for x in m["stt"]], ["ggml-base.bin"])
        self.assertTrue(m["llm"][1]["checksum_listed"])
        self.assertEqual(self.la.backends(), {"llm": ["cpu"], "stt": ["vulkan"]})

    def test_paths_cannot_escape(self):
        for name in ("../etc/passwd", "evil.gguf", "/etc/passwd", "sub/x.gguf"):
            with self.subTest(name=name):
                self.assertIsNone(self.la.model_path(name))

    def test_choice_checks(self):
        with mock.patch.object(localai, "meminfo", return_value={"MemAvailable": 10 ** 9}):
            self.assertIsNone(self.la.check_choice({"llm_model": "Huge-Q8.gguf"}))
            self.assertIn("not a chat model", self.la.check_choice({"llm_model": "ggml-base.bin"}))
            self.assertIn("no llama-vulkan build", self.la.check_choice({"llm_backend": "vulkan"}))
            self.assertIn("must be a model file name", self.la.check_choice({"llm_model": "../x.gguf"}))
        with mock.patch.object(localai, "meminfo", return_value={"MemAvailable": 100}):
            self.assertIn("memory", self.la.check_choice({"llm_model": "Huge-Q8.gguf"}))

    def test_env_lines(self):
        lines = dict(ln.split("=", 1) for ln in self.la.env_lines())
        self.assertEqual(lines["LOCAL_AI_LLM"], os.path.join(self.tmp, "models", "Small-Q4.gguf"))
        self.assertEqual(lines["LOCAL_AI_STT_BACKEND"], "vulkan")


if __name__ == "__main__":
    unittest.main()
