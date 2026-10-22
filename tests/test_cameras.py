"""Tests for the Cameras tab backend (cameras.py) and the on/off-service buttons in home.py, against a
local mock Home Assistant. Run: python3 -m unittest discover -s tests -v. Nothing leaves this machine.
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cameras  # noqa: E402
import home  # noqa: E402
from test_home import TOKEN, FakeClock, FakeSystem  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
NOW = 1_800_000_000.0


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class MockHA(BaseHTTPRequestHandler):
    rows = {}
    posts = []
    image_gets = []

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, b"{}")
        if self.path == "/api/states":
            return self._send(200, json.dumps(list(MockHA.rows.values())).encode())
        if self.path.startswith("/api/camera_proxy/"):
            ent = self.path.rsplit("/", 1)[-1]
            MockHA.image_gets.append(ent)
            if ent not in MockHA.rows:
                return self._send(404, b"{}")
            return self._send(200, JPEG, "image/jpeg")
        self._send(404, b"{}")

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, b"{}")
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        MockHA.posts.append((self.path, body["entity_id"]))
        ent = body["entity_id"]
        if self.path.endswith("/alarm_arm_away"):
            MockHA.rows[ent]["state"] = "armed_away"
        elif self.path.endswith("/alarm_disarm"):
            MockHA.rows[ent]["state"] = "disarmed"
        self._send(200, b"[]")

    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def row(ent, state, changed=NOW - 3600, updated=None, **attrs):
    return {"entity_id": ent, "state": state, "attributes": attrs,
            "last_changed": iso(changed), "last_updated": iso(updated or changed)}


CAMS = [
    {"id": "door", "label": "Door", "entity_id": "camera.door", "motion_entity": "binary_sensor.door_motion",
     "battery_entity": "binary_sensor.door_battery", "popup": True},
    {"id": "hall", "label": "Hall", "entity_id": "camera.hall", "motion_entity": "binary_sensor.hall_motion",
     "snapshot": False},
]


class CamerasTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHA)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-test-")
        fd = os.open(os.path.join(self.tmp, ".env"), os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, ("HA_TOKEN=%s\n" % TOKEN).encode())
        os.close(fd)
        MockHA.posts[:] = []
        MockHA.image_gets[:] = []
        MockHA.rows = {r["entity_id"]: r for r in (
            row("camera.door", "idle", updated=NOW - 600, motion_detection=True),
            row("binary_sensor.door_motion", "off"),
            row("binary_sensor.door_battery", "on"),
            row("camera.hall", "idle", motion_detection=False),
            row("binary_sensor.hall_motion", "off"),
            row("alarm_control_panel.casa", "disarmed"),
        )}
        self.clock = FakeClock(NOW)
        self.sleeps = []
        hcfg = copy.deepcopy(home.DEFAULT_HOME_CONFIG)
        hcfg.update(ha_url=self.url, buttons=[
            {"id": "alarm", "label": "Casa", "kind": "ha", "on_service": "alarm_control_panel.alarm_arm_away",
             "off_service": "alarm_control_panel.alarm_disarm", "entity_id": "alarm_control_panel.casa",
             "state_entity": "alarm_control_panel.casa", "confirm": True}])
        home.validate_home_config(hcfg)
        self.home = home.Home(hcfg, self.tmp, runner=FakeSystem(), clock=self.clock)
        ccfg = copy.deepcopy(cameras.DEFAULT_CAMERAS_CONFIG)
        ccfg["list"] = copy.deepcopy(CAMS)
        cameras.validate_cameras_config(ccfg)
        self.cams = cameras.Cameras(ccfg, self.home, clock=self.clock, sleep=self.sleeps.append)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def tick(self, seconds=10):
        self.clock.t += seconds


class ValidationTests(unittest.TestCase):
    def test_camera_config(self):
        cfg = copy.deepcopy(cameras.DEFAULT_CAMERAS_CONFIG)
        cfg["list"] = copy.deepcopy(CAMS)
        cameras.validate_cameras_config(cfg)
        for bad in ({"entity_id": "switch.door"}, {"motion_entity": "sensor.x"}, {"popup": "yes"},
                    {"id": "Bad Id"}, {"label": ""}, {"snapshot_service": "nope"}):
            c = copy.deepcopy(cfg)
            c["list"][0].update(bad)
            with self.assertRaises(ValueError, msg=bad):
                cameras.validate_cameras_config(c)
        c = copy.deepcopy(cfg)
        del c["list"][0]["motion_entity"]
        with self.assertRaises(ValueError):  # popup needs a motion sensor
            cameras.validate_cameras_config(c)

    def test_on_off_buttons(self):
        ok = {"id": "a", "label": "A", "kind": "ha", "on_service": "alarm_control_panel.alarm_arm_away",
              "off_service": "alarm_control_panel.alarm_disarm", "entity_id": "alarm_control_panel.casa",
              "state_entity": "alarm_control_panel.casa"}
        cfg = copy.deepcopy(home.DEFAULT_HOME_CONFIG)
        cfg["buttons"] = [ok]
        home.validate_home_config(cfg)
        for change in ({"service": "switch.toggle"}, {"state_entity": None}, {"off_service": "x"}):
            b = dict(ok, **change)
            if b.get("state_entity") is None:
                del b["state_entity"]
            cfg["buttons"] = [b]
            with self.assertRaises(ValueError, msg=change):
                home.validate_home_config(cfg)


class AlarmButtonTests(CamerasTestCase):
    def test_arm_then_disarm(self):
        self.assertEqual(self.home.status()["buttons"][0]["state"], "disarmed")
        self.home.action("alarm")
        self.assertEqual(MockHA.posts[-1], ("/api/services/alarm_control_panel/alarm_arm_away", "alarm_control_panel.casa"))
        self.tick()
        self.assertEqual(self.home.status()["buttons"][0]["state"], "armed_away")
        self.home.action("alarm")
        self.assertEqual(MockHA.posts[-1], ("/api/services/alarm_control_panel/alarm_disarm", "alarm_control_panel.casa"))


class CameraTests(CamerasTestCase):
    def test_snapshot_fields(self):
        snap = self.cams.snapshot()
        self.assertTrue(snap["ha_ok"])
        door, hall = snap["cameras"]
        self.assertEqual((door["available"], door["motion"], door["battery_low"], door["motion_detection"]),
                         (True, False, True, True))
        self.assertEqual(door["updated_ms"], int((NOW - 600) * 1000))
        self.assertEqual((hall["battery_low"], hall["motion_detection"], hall["can_snapshot"]), (None, False, False))
        self.assertIsNone(snap["alert"])

    def test_motion_alert_window(self):
        MockHA.rows["binary_sensor.door_motion"] = row("binary_sensor.door_motion", "on", changed=NOW - 20)
        MockHA.rows["binary_sensor.hall_motion"] = row("binary_sensor.hall_motion", "on", changed=NOW - 5)
        alert = self.cams.snapshot()["alert"]
        self.assertEqual(alert["id"], "door")  # hall has motion too, but no popup
        self.assertEqual(alert["key"], "door:%d" % int((NOW - 20) * 1000))
        self.tick(60)
        self.assertIsNone(self.cams.snapshot()["alert"])  # older than popup_seconds

    def test_image_cached_by_version(self):
        self.assertEqual(self.cams.image("door", "v1"), (JPEG, "image/jpeg"))
        self.cams.image("door", "v1")
        self.assertEqual(MockHA.image_gets, ["camera.door"])
        self.cams.image("door", "v2")
        self.assertEqual(len(MockHA.image_gets), 2)
        with self.assertRaises(home.ActionError) as cm:
            self.cams.image("nope", "v1")
        self.assertEqual(cm.exception.code, 404)

    def test_image_falls_back_to_last_copy(self):
        self.cams.image("door", "v1")
        self.home.cfg["ha_url"] = "http://127.0.0.1:1"
        self.assertEqual(self.cams.image("door", "v2")[0], JPEG)

    def test_new_snapshot_rate_limited(self):
        self.assertEqual(self.cams.request_snapshot("door"), {"ok": True})
        self.assertEqual(MockHA.posts[0], ("/api/services/blink/trigger_camera", "camera.door"))
        with self.assertRaises(home.ActionError) as cm:
            self.cams.request_snapshot("door")
        self.assertEqual(cm.exception.code, 429)
        with self.assertRaises(home.ActionError) as cm:
            self.cams.request_snapshot("hall")  # snapshots turned off for this one
        self.assertEqual(cm.exception.code, 403)
        self.tick(61)
        self.cams.request_snapshot("door")

    def test_no_token(self):
        os.remove(os.path.join(self.tmp, ".env"))
        snap = self.cams.snapshot()
        self.assertFalse(snap["ha_ok"])
        self.assertFalse(snap["cameras"][0]["available"])
        with self.assertRaises(home.ActionError) as cm:
            self.cams.image("door", "v1")
        self.assertEqual(cm.exception.code, 503)


FAKE_FFMPEG = """#!%s
# Stands in for ffmpeg: "ok" writes a small JPEG every 50 ms until killed; "fail" exits like a dead camera.
import os, sys, time
mode = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mode")).read().strip()
if mode == "fail":
    sys.stderr.write("rtsp://cam: Connection refused\\n")
    sys.exit(1)
n = 0
while True:
    n += 1
    sys.stdout.buffer.write(b"\\xff\\xd8frame-%%d\\xff\\xd9" %% n)
    sys.stdout.buffer.flush()
    time.sleep(0.05)
"""


def make_fake_ffmpeg(directory, mode="ok"):
    """Writes an executable fake ffmpeg into `directory`; returns its path. Change mode with set_mode."""
    path = os.path.join(directory, "ffmpeg")
    with open(path, "w") as f:
        f.write(FAKE_FFMPEG % sys.executable)
    os.chmod(path, 0o755)
    set_mode(directory, mode)
    return path


def set_mode(directory, mode):
    with open(os.path.join(directory, "mode"), "w") as f:
        f.write(mode)


RTSP_CAM = {"id": "v380", "label": "V380", "type": "rtsp", "rtsp_url": "rtsp://192.168.1.20:554/live/ch00_1"}


class RtspTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-rtsp-")
        self.cfg = copy.deepcopy(cameras.DEFAULT_CAMERAS_CONFIG)
        self.cfg["ffmpeg_path"] = make_fake_ffmpeg(self.tmp)
        self.cfg["list"] = [dict(RTSP_CAM)]
        cameras.validate_cameras_config(self.cfg)
        self.cams = cameras.Cameras(self.cfg, None)  # no Home Assistant needed for RTSP cameras
        self.feed = self.cams._feeds["v380"]

    def tearDown(self):
        self.cams.close()
        shutil.rmtree(self.tmp)

    def wait_for(self, cond, seconds=5):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_validation(self):
        good = copy.deepcopy(self.cfg)
        good["list"][0]["live_fps"] = 15
        cameras.validate_cameras_config(good)
        for bad in ({"rtsp_url": "rtsp://user:pw@192.168.1.20/live"}, {"rtsp_url": "http://192.168.1.20/"},
                    {"rtsp_url": "rtsp://192.168.1.20/live?x=1"}, {"rtsp_url": "rtsp://cam/a b"},
                    {"live_fps": 30}, {"popup": True}, {"entity_id": "camera.x"}, {"type": "onvif"}):
            c = copy.deepcopy(self.cfg)
            c["list"][0].update(bad)
            with self.assertRaises(ValueError, msg=bad):
                cameras.validate_cameras_config(c)
        c = copy.deepcopy(self.cfg)
        c["list"] = [dict(RTSP_CAM, id="c%d" % i) for i in range(7)]
        with self.assertRaises(ValueError):
            cameras.validate_cameras_config(c)

    def test_command_never_uses_a_shell(self):
        cmd = self.feed.command()
        self.assertEqual(cmd[0], self.cfg["ffmpeg_path"])
        self.assertIn("rtsp://192.168.1.20:554/live/ch00_1", cmd)
        self.assertEqual(cmd[-3:], ["-f", "image2pipe", "-"])

    def test_snapshot_without_home_assistant(self):
        snap = self.cams.snapshot()
        self.assertNotIn("ha_ok", snap)
        item = snap["cameras"][0]
        self.assertTrue(item["live"])
        self.assertTrue(item["available"])
        self.assertFalse(item["can_snapshot"])
        self.assertIsNone(item["updated_ms"])
        self.assertEqual(snap["live_max_seconds"], 600)

    def test_still_starts_the_feed_then_stops_when_idle(self):
        data, ctype = self.cams.image("v380", "s1")
        self.assertTrue(data.startswith(b"\xff\xd8frame-") and data.endswith(b"\xff\xd9"))
        self.assertEqual(ctype, "image/jpeg")
        self.assertIsNotNone(self.cams.snapshot()["cameras"][0]["updated_ms"])
        self.cfg["live_idle_seconds"] = 0.3
        self.assertTrue(self.wait_for(lambda: self.feed._proc is None))
        self.assertIsNone(self.feed.error)  # stopping when idle is not an error

    def test_live_frames_shared_and_counted(self):
        a, b = self.cams.live("v380"), self.cams.live("v380")
        fa, fb = next(a), next(b)
        self.assertTrue(fa.startswith(b"\xff\xd8"))
        self.assertTrue(fb.startswith(b"\xff\xd8"))
        self.assertNotEqual(next(a), fa)  # a new frame each time
        self.assertEqual(self.feed._viewers, 2)
        pid = self.feed._proc.pid
        a.close()
        b.close()
        self.assertEqual(self.feed._viewers, 0)
        self.assertEqual(self.feed._proc.pid, pid)  # one ffmpeg for both viewers

    def test_live_ends_after_max_seconds(self):
        self.cfg["live_max_seconds"] = 0.3
        frames = list(self.cams.live("v380"))
        self.assertGreater(len(frames), 1)
        self.assertEqual(self.feed._viewers, 0)

    def test_viewer_limit(self):
        gens = [self.cams.live("v380") for _ in range(cameras.MAX_VIEWERS)]
        for g in gens:
            next(g)
        extra = self.cams.live("v380")
        with self.assertRaises(home.ActionError) as cm:
            next(extra)
        self.assertEqual(cm.exception.code, 429)
        for g in gens:
            g.close()

    def test_camera_down(self):
        set_mode(self.tmp, "fail")
        with self.assertRaises(home.ActionError) as cm:
            self.cams.image("v380", "s1")
        self.assertEqual(cm.exception.code, 504)
        item = self.cams.snapshot()["cameras"][0]
        self.assertFalse(item["available"])
        self.assertIn("Connection refused", item["error"])
        # No new ffmpeg for RETRY_SECONDS after a failure.
        with self.assertRaises(home.ActionError) as cm:
            next(self.cams.live("v380"))
        self.assertEqual(cm.exception.code, 502)

    def test_ha_only_calls_are_refused(self):
        with self.assertRaises(home.ActionError) as cm:
            self.cams.request_snapshot("v380")
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(home.ActionError) as cm:
            self.cams.live("nope")
        self.assertEqual(cm.exception.code, 404)



class FakeOnvif:
    """Stands in for onvif.Camera."""
    def __init__(self, url):
        self.url = url
        self.steps = []
        self.ir = "auto"
        self.down = False

    def _check(self):
        if self.down:
            raise cameras.onvif.OnvifError("camera not answering (timed out)")

    def step(self, direction, speed, seconds):
        self._check()
        self.steps.append((direction, speed, seconds))

    def mode(self):
        self._check()
        return self.ir

    def set_mode(self, mode):
        self._check()
        self.ir = mode


ONVIF_CAM = dict(RTSP_CAM, rtsp_url_hd="rtsp://192.168.1.20:554/live/ch00_0",
                 onvif_url="http://192.168.1.20:8899/onvif/device_service")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-ctl-")
        self.cfg = copy.deepcopy(cameras.DEFAULT_CAMERAS_CONFIG)
        self.cfg["ffmpeg_path"] = make_fake_ffmpeg(self.tmp)
        self.cfg["list"] = [dict(ONVIF_CAM), dict(RTSP_CAM, id="plain", label="Plain")]
        cameras.validate_cameras_config(self.cfg)
        self.cams = cameras.Cameras(self.cfg, None, onvif_factory=FakeOnvif)
        self.ptz = self.cams._onvif["v380"]

    def tearDown(self):
        self.cams.close()
        shutil.rmtree(self.tmp)

    def test_validation(self):
        for bad in ({"onvif_url": "http://user:pw@192.168.1.20:8899/onvif"}, {"onvif_url": "https://x/"},
                    {"onvif_url": "http://x/a?b=1"}, {"rtsp_url_hd": "http://x/"}, {"ptz": "yes"}):
            c = copy.deepcopy(self.cfg)
            c["list"][0].update(bad)
            with self.assertRaises(ValueError, msg=bad):
                cameras.validate_cameras_config(c)
        c = copy.deepcopy(self.cfg)
        c["list"][1]["ptz"] = True  # no onvif_url
        with self.assertRaises(ValueError):
            cameras.validate_cameras_config(c)

    def test_flags_in_snapshot(self):
        items = {c["id"]: c for c in self.cams.snapshot()["cameras"]}
        self.assertEqual((items["v380"]["hd"], items["v380"]["ptz"], items["v380"]["day_night"]), (True, True, True))
        self.assertEqual((items["plain"]["hd"], items["plain"]["ptz"], items["plain"]["day_night"]), (False, False, False))
        self.cfg["list"][0]["ptz"] = False
        self.assertFalse(self.cams.snapshot()["cameras"][0]["ptz"])

    def test_hd_uses_its_own_feed(self):
        hd = self.cams._hd_feeds["v380"]
        self.assertIn("rtsp://192.168.1.20:554/live/ch00_0", hd.command())
        g = self.cams.live("v380", hd=True)
        next(g)
        self.assertEqual(hd._viewers, 1)
        self.assertIsNone(self.cams._feeds["v380"]._proc)
        g.close()
        g = self.cams.live("plain", hd=True)  # no HD stream: the usual one
        next(g)
        self.assertEqual(self.cams._feeds["plain"]._viewers, 1)
        g.close()

    def test_move_and_mode(self):
        self.assertEqual(self.cams.move("v380", "left"), {"ok": True})
        self.assertEqual(self.ptz.steps, [("left", 0.5, 0.5)])
        self.assertEqual(self.cams.controls("v380")["mode"], "auto")
        self.assertEqual(self.cams.set_mode("v380", "night"), {"ok": True, "mode": "night"})
        self.assertEqual(self.cams.controls("v380")["mode"], "night")
        for call, code in ((lambda: self.cams.move("v380", "spin"), 400),
                           (lambda: self.cams.set_mode("v380", "dusk"), 400),
                           (lambda: self.cams.move("plain", "left"), 404),
                           (lambda: self.cams.move("nope", "left"), 404)):
            with self.assertRaises(home.ActionError) as cm:
                call()
            self.assertEqual(cm.exception.code, code)

    def test_camera_down(self):
        self.ptz.down = True
        with self.assertRaises(home.ActionError) as cm:
            self.cams.move("v380", "up")
        self.assertEqual(cm.exception.code, 502)
        ctl = self.cams.controls("v380")
        self.assertIsNone(ctl["mode"])
        self.assertIn("not answering", ctl["error"])



class RelocateTests(unittest.TestCase):
    MAC = "00:00:5e:00:53:01"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-move-")
        self.cfg = copy.deepcopy(cameras.DEFAULT_CAMERAS_CONFIG)
        self.cfg["ffmpeg_path"] = make_fake_ffmpeg(self.tmp)
        self.cfg["list"] = [dict(ONVIF_CAM, mac=self.MAC.upper())]
        cameras.validate_cameras_config(self.cfg)
        self.answers = {}
        self.searches = 0

        def discover():
            self.searches += 1
            return self.answers

        self.cams = cameras.Cameras(self.cfg, None, onvif_factory=FakeOnvif, discover=discover)
        self.moved = []
        self.cams._onvif["v380"].moved_to = self.moved.append

    def tearDown(self):
        self.cams.close()
        shutil.rmtree(self.tmp)

    def test_moves_every_url_to_the_new_address(self):
        self.answers = {"192.168.1.30": (["http://192.168.1.30:8899/x"], "aa:bb:cc:dd:ee:ff"),
                        "192.168.1.77": (["http://192.168.1.77:8899/onvif/device_service"], self.MAC)}
        self.assertEqual(self.cams.relocate("v380"), "192.168.1.77")
        cam = self.cfg["list"][0]
        self.assertEqual(cam["rtsp_url"], "rtsp://192.168.1.77:554/live/ch00_1")
        self.assertEqual(cam["rtsp_url_hd"], "rtsp://192.168.1.77:554/live/ch00_0")
        self.assertEqual(self.cams._feeds["v380"].url, "rtsp://192.168.1.77:554/live/ch00_1")
        self.assertIn("rtsp://192.168.1.77:554/live/ch00_0", self.cams._hd_feeds["v380"].command())
        self.assertEqual(self.moved, ["http://192.168.1.77:8899/onvif/device_service"])

    def test_rate_limited_and_same_address(self):
        self.answers = {"192.168.1.20": (["http://192.168.1.20:8899/x"], self.MAC)}
        self.assertIsNone(self.cams.relocate("v380"))  # still where config says
        self.assertIsNone(self.cams.relocate("v380"))
        self.assertEqual(self.searches, 1)
        self.assertEqual(self.moved, [])

    def test_failed_feed_starts_a_search(self):
        set_mode(self.tmp, "fail")
        self.answers = {"192.168.1.88": (["http://192.168.1.88:8899/onvif/device_service"], self.MAC)}
        set_mode(self.tmp, "fail")
        with self.assertRaises(home.ActionError):
            self.cams.image("v380", "s1")
        end = time.monotonic() + 5
        while self.cams._feeds["v380"].url.find("192.168.1.88") < 0 and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(self.cams._feeds["v380"].url, "rtsp://192.168.1.88:554/live/ch00_1")
        set_mode(self.tmp, "ok")
        data, _ = self.cams.image("v380", "s2")  # the retry wait was cleared: works at once
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_without_mac_nothing_searches(self):
        self.cfg["list"][0].pop("mac")
        self.assertIsNone(self.cams.relocate("v380"))
        self.assertEqual(self.searches, 0)

    def test_bad_mac_rejected(self):
        c = copy.deepcopy(self.cfg)
        c["list"][0]["mac"] = "00:00"
        with self.assertRaises(ValueError):
            cameras.validate_cameras_config(c)



class BlinkLiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cam-blink-")
        self.cfg = copy.deepcopy(cameras.DEFAULT_CAMERAS_CONFIG)
        self.cfg.update(ffmpeg_path=make_fake_ffmpeg(self.tmp), blink_proxy_url="http://127.0.0.1:8088")
        self.cfg["list"] = [dict(CAMS[0], blink_live="cámara_patio"), dict(CAMS[1])]
        cameras.validate_cameras_config(self.cfg)
        self.token = "t0ken"
        self.cams = cameras.Cameras(self.cfg, None, blink_token=lambda: self.token)
        self.feed = self.cams._blink_feeds["door"]

    def tearDown(self):
        self.cams.close()
        shutil.rmtree(self.tmp)

    def test_validation(self):
        for bad in ({"blink_live": "Camara Patio"}, {"blink_live": ""}, {"blink_live": "a/b"}, {"blink_live": "_x"}):
            c = copy.deepcopy(self.cfg)
            c["list"][0].update(bad)
            with self.assertRaises(ValueError, msg=bad):
                cameras.validate_cameras_config(c)
        for url in ("https://127.0.0.1:8088", "http://x/path", "http://user:pw@x"):
            c = copy.deepcopy(self.cfg)
            c["blink_proxy_url"] = url
            with self.assertRaises(ValueError, msg=url):
                cameras.validate_cameras_config(c)
        c = copy.deepcopy(self.cfg)
        c["blink_proxy_url"] = ""
        with self.assertRaises(ValueError):  # blink_live without a proxy
            cameras.validate_cameras_config(c)

    def test_command_keeps_the_token_out_of_the_url(self):
        cmd = self.feed.command()
        url = cmd[cmd.index("-i") + 1]
        self.assertEqual(url, "http://127.0.0.1:8088/cameras/c%C3%A1mara_patio/mpegts?seconds=660")
        self.assertNotIn("t0ken", url)
        self.assertEqual(cmd[cmd.index("-headers") + 1], "Authorization: Bearer t0ken\r\n")
        self.assertNotIn("-rtsp_transport", cmd)
        self.token = None
        self.assertNotIn("-headers", self.feed.command())

    def test_proxy_errors_in_plain_words(self):
        msg = ("http://127.0.0.1:8088/cameras/c%C3%A1mara_patio/mpegts?seconds=660: Server returned 4XX Client "
               "Error, but not one of 40{0,1,3,4}")
        self.assertIn("30 s between live views", self.feed._explain(msg))
        self.assertNotIn("127.0.0.1", self.feed._explain("http://127.0.0.1:8088/x: Something odd"))
        rtsp = self.cams._feeds.get("v380")
        self.assertIsNone(rtsp)  # only Blink feeds here; RTSP messages pass through unchanged:
        plain = cameras.LiveFeed(dict(RTSP_CAM), self.cfg)
        self.assertEqual(plain._explain("rtsp://x: Connection refused"), "rtsp://x: Connection refused")

    def test_live_on_demand(self):
        g = self.cams.live("door")
        self.assertTrue(next(g).startswith(b"\xff\xd8"))
        self.assertEqual(self.feed._viewers, 1)
        g.close()
        with self.assertRaises(home.ActionError) as cm:
            self.cams.live("hall")  # no blink_live: still no live view
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
