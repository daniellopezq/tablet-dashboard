"""Tests for server.py. Run: python3 -m unittest discover -s tests -v

Uses temporary web/media directories and a local mock of the Open-Meteo API;
makes no external network requests.
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

GOOD_WEATHER = {
    "current_units": {"temperature_2m": "°C"},
    "current": {"temperature_2m": 21.4, "relative_humidity_2m": 63, "weather_code": 2, "is_day": 1},
    "daily": {
        "time": ["2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"],
        "weather_code": [2, 61, 0, 95],
        "temperature_2m_max": [24.0, 20.5, 22.1, 19.0],
        "temperature_2m_min": [14.2, 13.0, 12.5, 11.9],
    },
}


class MockMeteo(BaseHTTPRequestHandler):
    mode = "ok"
    last_query = ""

    def do_GET(self):
        MockMeteo.last_query = self.path
        if self.mode == "ok":
            body = json.dumps(GOOD_WEATHER).encode()
        elif self.mode == "fail":
            body = json.dumps({"error": True, "reason": "test failure"}).encode()
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.mode == "big":
            body = b"[" + b"0," * 300000 + b"0]"
        elif self.mode == "garbage":
            body = b"<html>not json</html>"
        elif self.mode == "slow":
            time.sleep(2.5)
            body = json.dumps(GOOD_WEATHER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def start_thread_server(httpd):
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t


class DashboardTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dash-test-")
        cls.web = os.path.join(cls.tmp, "web")
        cls.media = os.path.join(cls.tmp, "media")
        os.makedirs(os.path.join(cls.web, "icons"))
        os.makedirs(cls.media)
        with open(os.path.join(cls.web, "index.html"), "w") as f:
            f.write("<!DOCTYPE html><title>t</title>")
        with open(os.path.join(cls.web, "app.js"), "w") as f:
            f.write("var x = 1;")
        with open(os.path.join(cls.web, ".hidden.js"), "w") as f:
            f.write("secret")
        with open(os.path.join(cls.web, "notes.bin"), "w") as f:
            f.write("unlisted type")
        with open(os.path.join(cls.tmp, "outside.txt"), "w") as f:
            f.write("outside")
        os.symlink(os.path.join(cls.tmp, "outside.txt"), os.path.join(cls.web, "escape.txt"))
        os.symlink(cls.tmp, os.path.join(cls.web, "escapedir"))
        cls.video = bytes(range(256)) * 40  # 10240 bytes
        with open(os.path.join(cls.media, "clip.mp4"), "wb") as f:
            f.write(cls.video)

        cls.meteo = ThreadingHTTPServer(("127.0.0.1", 0), MockMeteo)
        start_thread_server(cls.meteo)

    @classmethod
    def tearDownClass(cls):
        cls.meteo.shutdown()
        shutil.rmtree(cls.tmp)

    def make_app(self, weather=None):
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        cfg["web_dir"] = self.web
        cfg["media_dir"] = self.media
        cfg["weather"]["api_url"] = "http://127.0.0.1:%d/v1/forecast" % self.meteo.server_port
        cfg["weather"]["timeout_seconds"] = 1
        cfg["weather"].update(weather or {})
        server.validate_config(cfg)
        return server.App(cfg)

    def serve(self, app):
        httpd = server.DashboardServer(("127.0.0.1", 0), app)
        start_thread_server(httpd)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_port

    def request(self, port, path, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest(method, path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body


class StaticAndApiTests(DashboardTestCase):
    def setUp(self):
        self.port = self.serve(self.make_app())

    def test_index(self):
        resp, body = self.request(self.port, "/")
        self.assertEqual(resp.status, 200)
        self.assertIn("text/html", resp.getheader("Content-Type"))
        self.assertIn("default-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertIn(b"<title>", body)

    def test_head(self):
        resp, body = self.request(self.port, "/app.js", method="HEAD")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Length"), "10")
        self.assertEqual(body, b"")

    def test_restricted_paths(self):
        for path in ["/../server.py", "/%2e%2e/outside.txt", "/..%2f..%2fetc%2fpasswd",
                     "/icons/../../outside.txt", "/escape.txt", "/escapedir/outside.txt",
                     "/.hidden.js", "/%2ehidden.js", "/icons/", "/icons", "/notes.bin",
                     "/app.js%00.html", "/..\\outside.txt", "/config.json", "/media/",
                     "/media/../web/index.html", "/media/%2e%2e/outside.txt", "/media/missing.mp4",
                     "//etc/passwd"]:
            with self.subTest(path=path):
                resp, body = self.request(self.port, path)
                self.assertIn(resp.status, (400, 404), path)
                self.assertNotIn(b"outside", body)
                self.assertNotIn(b"<a href", body)

    def test_absolute_uri_form_is_confined(self):
        resp, _ = self.request(self.port, "http://evil.example/../outside.txt")
        self.assertIn(resp.status, (400, 404))

    def test_methods(self):
        resp, _ = self.request(self.port, "/", method="POST")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.getheader("Allow"), "GET, HEAD")

    def test_api_time(self):
        before = int(time.time() * 1000)
        resp, body = self.request(self.port, "/api/time")
        after = int(time.time() * 1000)
        self.assertEqual(resp.status, 200)
        self.assertIn("no-store", resp.getheader("Cache-Control"))
        data = json.loads(body)
        self.assertTrue(before <= data["now_ms"] <= after)
        self.assertEqual(data["timezone"], "UTC")
        expected = datetime.now(ZoneInfo("UTC")).utcoffset().total_seconds() / 60
        self.assertEqual(data["utc_offset_minutes"], expected)
        self.assertRegex(data["utc_offset"], r"^[+-]\d\d:\d\d$")

    def test_api_config_and_unknown(self):
        resp, body = self.request(self.port, "/api/config")
        self.assertEqual(json.loads(body)["clock_default_24h"], True)
        resp, _ = self.request(self.port, "/api/nope")
        self.assertEqual(resp.status, 404)

    def test_api_uptime(self):
        resp, body = self.request(self.port, "/api/uptime")
        self.assertEqual(resp.status, 200)
        data = json.loads(body)
        for key in ("enabled", "checks", "counts", "last_run_ms", "kuma_ok"):
            self.assertIn(key, data)

class MediaTests(DashboardTestCase):
    def setUp(self):
        self.port = self.serve(self.make_app())

    def test_full(self):
        resp, body = self.request(self.port, "/media/clip.mp4")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "video/mp4")
        self.assertEqual(resp.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(body, self.video)

    def test_ranges(self):
        size = len(self.video)
        cases = [("bytes=0-99", 0, 99), ("bytes=100-", 100, size - 1), ("bytes=-50", size - 50, size - 1),
                 ("bytes=10000-999999", 10000, size - 1)]
        for header, start, end in cases:
            with self.subTest(range=header):
                resp, body = self.request(self.port, "/media/clip.mp4", headers={"Range": header})
                self.assertEqual(resp.status, 206)
                self.assertEqual(resp.getheader("Content-Range"), "bytes %d-%d/%d" % (start, end, size))
                self.assertEqual(body, self.video[start:end + 1])

    def test_unsatisfiable_and_ignored(self):
        resp, _ = self.request(self.port, "/media/clip.mp4", headers={"Range": "bytes=999999-"})
        self.assertEqual(resp.status, 416)
        self.assertEqual(resp.getheader("Content-Range"), "bytes */%d" % len(self.video))
        resp, body = self.request(self.port, "/media/clip.mp4", headers={"Range": "bytes=0-1,5-6"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(len(body), len(self.video))


class WeatherTests(DashboardTestCase):
    LOC = {"latitude": 32.5149, "longitude": -117.0382, "label": "Test City"}

    def test_unconfigured(self):
        app = self.make_app()
        self.assertFalse(app.weather.fetch_once())
        port = self.serve(app)
        resp, body = self.request(port, "/api/weather")
        data = json.loads(body)
        self.assertIn("no-store", resp.getheader("Cache-Control"))
        self.assertEqual(data["status"], "unconfigured")
        self.assertIsNone(data["data"])

    def test_ok_then_stale_after_failure(self):
        MockMeteo.mode = "ok"
        app = self.make_app(self.LOC)
        port = self.serve(app)
        self.assertEqual(json.loads(self.request(port, "/api/weather")[1])["status"], "loading")
        self.assertTrue(app.weather.fetch_once())
        self.assertIn("latitude=32.5149", MockMeteo.last_query)
        self.assertIn("forecast_days=4", MockMeteo.last_query)
        data = json.loads(self.request(port, "/api/weather")[1])
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["stale"])
        self.assertEqual(data["unit"], "C")
        self.assertEqual(data["data"]["current"]["condition"], "Partly cloudy")
        self.assertEqual(data["data"]["current"]["humidity"], 63)
        self.assertEqual(data["data"]["today"]["max"], 24.0)
        self.assertEqual([d["icon"] for d in data["data"]["forecast"]], ["rain", "clear", "storm"])
        updated = data["updated_ms"]

        MockMeteo.mode = "fail"
        self.assertFalse(app.weather.fetch_once())
        data = json.loads(self.request(port, "/api/weather")[1])
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["stale"])
        self.assertEqual(data["error"], "HTTP 503")
        self.assertEqual(data["updated_ms"], updated)
        self.assertEqual(data["data"]["current"]["temperature"], 21.4)
        self.assertEqual(app.weather.next_delay(), 60)
        app.weather.fetch_once()
        self.assertEqual(app.weather.next_delay(), 120)

        MockMeteo.mode = "ok"
        self.assertTrue(app.weather.fetch_once())
        self.assertEqual(app.weather.next_delay(), 15 * 60)
        self.assertFalse(app.weather.snapshot()["stale"])

    def test_age_based_stale(self):
        MockMeteo.mode = "ok"
        app = self.make_app(self.LOC)
        app.weather.fetch_once()
        app.weather._updated_ms -= 36 * 60000
        self.assertTrue(app.weather.snapshot()["stale"])

    def test_error_without_data(self):
        for mode, expected in [("fail", "HTTP 503"), ("big", "response too large"),
                               ("garbage", "invalid response"), ("slow", "timeout")]:
            with self.subTest(mode=mode):
                MockMeteo.mode = mode
                app = self.make_app(self.LOC)
                self.assertFalse(app.weather.fetch_once())
                snap = app.weather.snapshot()
                self.assertEqual(snap["status"], "error")
                self.assertEqual(snap["error"], expected)
                self.assertIsNone(snap["data"])
        MockMeteo.mode = "ok"

    def test_fahrenheit_and_night_icon(self):
        MockMeteo.mode = "ok"
        app = self.make_app(dict(self.LOC, temperature_unit="fahrenheit"))
        night = json.loads(json.dumps(GOOD_WEATHER))
        night["current"]["weather_code"] = 0
        night["current"]["is_day"] = 0
        norm = app.weather.normalize(night)
        self.assertEqual(norm["current"]["icon"], "clear-night")
        self.assertIn("temperature_unit=fahrenheit", app.weather.build_url())
        self.assertEqual(app.weather.snapshot()["unit"], "F")


class ConfigTests(unittest.TestCase):
    def write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        self.addCleanup(os.remove, path)
        return path

    def test_project_config_loads(self):
        cfg = server.load_config(os.path.join(server.BASE_DIR, "config.example.json"))
        self.assertEqual(cfg["port"], 8099)

    def test_rejects_bad_values(self):
        bad = [{"timezone": "Mars/Base"}, {"port": 0}, {"bogus": 1},
               {"weather": {"latitude": 10}}, {"weather": {"latitude": 91, "longitude": 0}},
               {"weather": {"temperature_unit": "kelvin"}}, {"weather": {"refresh_minutes": 1}},
               {"weather": {"nope": 1}}]
        for obj in bad:
            with self.subTest(obj=obj):
                with self.assertRaises(server.ConfigError):
                    server.load_config(self.write(obj))

    def test_parse_range_edges(self):
        self.assertIsNone(server.parse_range(None, 10))
        self.assertIsNone(server.parse_range("bytes=5-2", 10))
        self.assertEqual(server.parse_range("bytes=-0", 10), "unsatisfiable")
        self.assertEqual(server.parse_range("bytes=0-", 0), "unsatisfiable")
        self.assertEqual(server.parse_range("bytes=-100", 10), (0, 9))


if __name__ == "__main__":
    unittest.main()
