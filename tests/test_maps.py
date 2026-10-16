"""Tests for maps.py using a local mock tile server (no network, no real key)."""
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import maps  # noqa: E402
from PIL import Image  # noqa: E402

HITS = []
GOOD_KEY = "a" * 32


def tile_png(color):
    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), color).save(buf, "PNG")
    return buf.getvalue()


class Mock(BaseHTTPRequestHandler):
    def do_GET(self):
        HITS.append(self.path)
        if "appid=" in self.path and GOOD_KEY not in self.path:
            self.send_response(401)
            self.end_headers()
            return
        color = (0, 80, 255, 160) if "appid=" in self.path else (200, 200, 200, 255)
        body = tile_png(color)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class MapTests(unittest.TestCase):
    def setUp(self):
        HITS.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self._saved = (maps.OWM_TILE_URL, maps.OSM_TILE_URL)
        maps.OWM_TILE_URL = base + "/owm/%s/%d/%d/%d.png?appid=%s"
        maps.OSM_TILE_URL = base + "/osm/%d/%d/%d.png"
        self.dir = tempfile.mkdtemp()
        self.cfg = dict(maps.DEFAULT_MAPS_CONFIG, layers=["clouds_new", "wind_new"],
                        max_calls_per_minute=60, env_file=".env")
        os.environ["OPENWEATHER_API_KEY"] = GOOD_KEY

    def tearDown(self):
        maps.OWM_TILE_URL, maps.OSM_TILE_URL = self._saved
        self.srv.shutdown()
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("OPENWEATHER_API_KEY", None)

    def service(self):
        svc = maps.MapService(self.cfg, 32.5149, -117.0382, "Test", self.dir, "test-agent")
        svc._limiter = maps.RateLimiter(60, sleep=lambda s: None)
        return svc

    def test_refresh_writes_pngs_and_caches_base(self):
        svc = self.service()
        self.assertTrue(svc.refresh_once())
        snap = svc.snapshot()
        self.assertEqual(snap["status"], "ok")
        self.assertEqual([l["name"] for l in snap["layers"]], ["clouds_new", "wind_new"])
        path = svc.path_for("clouds_new.png")
        img = Image.open(path)
        self.assertEqual(img.size, (1024, 512))
        self.assertEqual(Image.open(svc.path_for("clouds_new_small.png")).size, (512, 256))
        self.assertIn("small_url", snap["layers"][0])
        self.assertIsNone(svc.path_for("../server.py"))
        self.assertIsNone(svc.path_for("temp_new.png"))
        osm1 = len([h for h in HITS if h.startswith("/osm/")])
        n_tiles = len(HITS) - osm1
        self.assertEqual(osm1 * 2, n_tiles)  # base tiles fetched once, overlay tiles per layer
        self.assertTrue(svc.refresh_once())
        self.assertEqual(len([h for h in HITS if h.startswith("/osm/")]), osm1)

    def test_bad_key_stops_cycle_and_keeps_old_images(self):
        svc = self.service()
        self.assertTrue(svc.refresh_once())
        before = svc.snapshot()["layers"][0]["updated_ms"]
        os.environ["OPENWEATHER_API_KEY"] = "b" * 32
        HITS.clear()
        self.assertFalse(svc.refresh_once())
        snap = svc.snapshot()
        self.assertIn("rejected", snap["error"])
        self.assertEqual(snap["layers"][0]["updated_ms"], before)
        self.assertEqual(len([h for h in HITS if "appid=" in h]), 1)  # aborted after first 401
        self.assertEqual(svc.next_delay(), 300)
        self.assertNotIn("b" * 32, str(snap))

    def test_missing_key(self):
        os.environ.pop("OPENWEATHER_API_KEY")
        svc = self.service()
        self.assertFalse(svc.refresh_once())
        self.assertEqual(HITS, [])
        self.assertIn("not_configured", svc.snapshot()["error"])

    def test_rate_limiter_gap(self):
        t = [0.0]
        slept = []
        rl = maps.RateLimiter(30, clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
        for _ in range(3):
            rl.wait()
        self.assertEqual(len(slept), 2)
        self.assertAlmostEqual(slept[0], 2.0)

    def test_config_validation(self):
        bad = dict(maps.DEFAULT_MAPS_CONFIG, layers=["nope"])
        with self.assertRaises(ValueError):
            maps.validate_maps_config(bad)
        maps.validate_maps_config(dict(maps.DEFAULT_MAPS_CONFIG))


if __name__ == "__main__":
    unittest.main()
