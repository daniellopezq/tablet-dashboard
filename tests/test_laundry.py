"""Laundry check (laundry.py) against a fake pan/tilt camera with end stops; no camera, no ffmpeg."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import laundry  # noqa: E402
from home import ActionError  # noqa: E402
from weather import WeatherService  # noqa: E402

PAN_MAX, TILT_MAX = 20, 10   # right stop at pan 20, bottom stop at tilt 10; top stop tilt 0


class Proc:
    def __init__(self, out, code=0):
        self.stdout, self.returncode = out, code


def fake_ffmpeg(world):
    """'JPEGs' are b"P<pan>,<tilt>"; decoding paints the pose into the frame (and colour where laundry hangs)."""
    def run(cmd, input=None, capture_output=True, timeout=None):
        w, h = (int(v) for v in cmd[cmd.index("-vf") + 1][len("scale="):].split(":"))
        pan, tilt = (int(v) for v in input[1:].decode().split(","))
        if (w, h) == (laundry.SMALL_W, laundry.SMALL_H):
            px = bytes([tilt * 20, pan * 8, 0])  # every pose a different grey
        else:
            px = bytes([200, 40, 40]) if (pan, tilt) in world.laundry else bytes([120, 120, 120])
        return Proc(px * (w * h))
    return run


class FakeCameras:
    def __init__(self, pan=7, tilt=6):
        self.pan, self.tilt = pan, tilt
        self.laundry = set()
        self.last_person_move = {}
        self.moves = []
        self.on_move = None

    def move(self, cid, direction, by_person=True):
        self.moves.append(direction)
        dp, dt = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}[direction]
        self.pan = min(PAN_MAX, max(0, self.pan + dp))
        self.tilt = min(TILT_MAX, max(0, self.tilt + dt))
        if self.on_move:
            self.on_move(len(self.moves))
        return {"ok": True}

    def still(self, cid, hd=True):
        return b"P%d,%d" % (self.pan, self.tilt)


class FakeHome:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def ha_notify(self, service, title, message):
        if self.fail:
            raise ActionError("Home Assistant is unreachable", 503)
        self.sent.append((service, title, message))


class FakeSpeaker:
    def __init__(self):
        self.texts = []

    def prepare(self, text, lang=None):
        self.texts.append((text, lang))
        return "sid1"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def config(**over):
    c = dict(laundry.DEFAULT_LAUNDRY_CONFIG, enabled=True, save_frames_dir="",
             notify_services=["notify.mobile_app_phone"],
             home=["right 25", "up 12", "down 3", "left 18"],
             lines=[
                 {"id": "line2", "label": "el tendedero del patio",
                  "moves": ["right 25", "up 12", "down 4", "left 8"],
                  "boxes": [{"box": [600, 200, 800, 400], "saturation": 38}]},
                 {"id": "line1", "label": "el tendedero de la puerta",
                  "moves": ["right stop", "up stop", "left 15", "down 5"],  # detection still works
                  "boxes": [{"box": [500, 300, 900, 500], "saturation": 52}]},
             ])
    c.update(over)
    return c


def rain(prob=80, is_day=True, icon="cloud"):
    return {"current": {"icon": icon, "condition": "Overcast", "precipitation": 0, "is_day": is_day},
            "next_hours": [{"time": "2026-10-25T13:00", "precipitation_probability": 10},
                           {"time": "2026-10-25T14:00", "precipitation_probability": prob}]}


class LaundryTestBase(unittest.TestCase):
    def make(self, **over):
        self.cams = FakeCameras()
        self.home = FakeHome()
        self.speaker = FakeSpeaker()
        self.clock = Clock()
        self.mono = [1000.0]
        self.tmp = tempfile.mkdtemp()
        self.watch = laundry.LaundryWatch(config(**over), self.cams, self.home, self.tmp, self.speaker,
                                          runner=fake_ffmpeg(self.cams), clock=self.clock,
                                          monotonic=lambda: self.mono[0], sleep=lambda s: None)
        return self.watch


class RainRuleTest(unittest.TestCase):
    def test_hourly_chance_within_the_window(self):
        coming, why, why_es = laundry.rain_coming(rain(60), 50, 3)
        self.assertTrue(coming)
        self.assertEqual(why, "60% at 14:00")
        self.assertIn("a las 14:00", why_es)

    def test_below_threshold_or_beyond_the_window(self):
        self.assertFalse(laundry.rain_coming(rain(40), 50, 3)[0])
        self.assertFalse(laundry.rain_coming(rain(90), 50, 1)[0])  # only 13:00 is looked at

    def test_raining_now(self):
        coming, why, why_es = laundry.rain_coming(rain(0, icon="rain"), 50, 3)
        self.assertTrue(coming)
        self.assertEqual(why_es, "ya está lloviendo")

    def test_no_weather(self):
        self.assertFalse(laundry.rain_coming(None, 50, 3)[0])

    def test_weather_next_hours(self):
        hours = WeatherService._next_hours({"time": ["2026-10-25T12:00", "2026-10-25T13:00"],
                                            "precipitation_probability": [5, "x"], "precipitation": [0.0]})
        self.assertEqual(hours, [
            {"time": "2026-10-25T12:00", "precipitation_probability": 5, "precipitation": 0.0},
            {"time": "2026-10-25T13:00", "precipitation_probability": None, "precipitation": None}])
        self.assertEqual(WeatherService._next_hours(None), [])
        many = {"time": ["2026-10-25T%02d:00" % i for i in range(24)], "precipitation_probability": list(range(24))}
        self.assertEqual(len(WeatherService._next_hours(many)), 12)  # the modern view's 12-hour rain strip


class PixelTest(unittest.TestCase):
    def test_saturation_and_diff(self):
        red = bytes([200, 40, 40]) * (laundry.FULL_W * laundry.FULL_H)
        self.assertAlmostEqual(laundry.saturation(red, [0, 0, 10, 10]), 204, delta=1)
        grey = bytes([90, 90, 90]) * (laundry.SMALL_W * laundry.SMALL_H)
        self.assertEqual(laundry.grey_diff(grey, grey), 0)
        brighter = bytes([100, 100, 100]) * (laundry.SMALL_W * laundry.SMALL_H)
        self.assertAlmostEqual(laundry.grey_diff(grey, brighter), 10, places=3)

    def test_best_shift_finds_a_moved_square(self):
        w, h = laundry.SMALL_W, laundry.SMALL_H

        def frame(sx, sy):
            px = bytearray(bytes([30, 30, 30]) * (w * h))
            for y in range(60 + sy, 110 + sy):
                for x in range(100 + sx, 170 + sx):
                    px[(y * w + x) * 3:(y * w + x) * 3 + 3] = bytes([220, 220, 220])
            return bytes(px)
        dx, dy, d, edge = laundry.best_shift(frame(9, -5), frame(0, 0), 40)
        self.assertAlmostEqual(dx, 9, delta=1)  # sampling every 3rd pixel: +-1 (4 full-size pixels)
        self.assertAlmostEqual(dy, -5, delta=1)
        self.assertFalse(edge)
        self.assertLess(d, 1)

    def test_shift_box_scales_and_clamps(self):
        self.assertEqual(laundry.shift_box([100, 100, 200, 200], 10, -5), [140, 80, 240, 180])
        self.assertEqual(laundry.shift_box([1200, 0, 1280, 50], 40, -30), [1279, 0, 1280, 1])

    def test_decode_failure(self):
        with self.assertRaises(ActionError):
            laundry.decode(b"x", 4, 4, runner=lambda *a, **k: Proc(b"", 1))


class CheckTest(LaundryTestBase):
    def test_finds_laundry_warns_and_goes_home(self):
        w = self.make()
        self.cams.laundry = {(12, 4)}  # line2 pose: right stop 20 - 8, top 0 + 4
        w._run("80% at 14:00", "va a llover, 80% a las 14:00")
        st = w.status()
        self.assertEqual([l["id"] for l in st["lines"]], ["line2", "line1"])  # labels for the modern panel
        self.assertEqual(st["last"]["result"], "ok")
        self.assertEqual([(l["id"], l["laundry"]) for l in st["last"]["lines"]],
                         [("line2", True), ("line1", False)])
        self.assertEqual((self.cams.pan, self.cams.tilt), (2, 3))  # home
        text = st["alert"]["text"]
        self.assertEqual(text, "Hay ropa tendida en el tendedero del patio y va a llover, 80% a las 14:00.")
        self.assertEqual(st["alert"]["speech_id"], "sid1")
        self.assertEqual(self.home.sent, [("notify.mobile_app_phone", "Ropa tendida", text)])

    def test_nothing_out_no_warning(self):
        w = self.make()
        w._run("x", "y")
        self.assertIsNone(w.status()["alert"])
        self.assertEqual(self.home.sent, [])
        self.assertEqual((self.cams.pan, self.cams.tilt), (2, 3))

    def test_reference_alignment_is_recorded(self):
        w = self.make()
        with open(os.path.join(self.tmp, "ref.jpg"), "wb") as f:
            f.write(b"P12,4")
        w.cfg["lines"][0]["ref"] = "ref.jpg"
        self.cams.laundry = {(12, 4)}
        w._run("x", "")
        line = w.status()["last"]["lines"][0]
        self.assertTrue(line["laundry"])
        self.assertTrue(line["aligned"])

    def test_align_nudges_back_toward_the_reference(self):
        w = self.make()
        shifts = iter([(50, -60, 9.0, False), (2, 1, 3.0, False)])  # camera left and down of the ref
        orig = laundry.best_shift
        laundry.best_shift = lambda cur, ref, m: next(shifts)
        try:
            jpeg, dx, dy, edge = w._align(b"ref")
        finally:
            laundry.best_shift = orig
        self.assertEqual(self.cams.moves, ["right", "up"])
        self.assertEqual((dx, dy, edge), (2, 1, False))

    def test_align_does_not_nudge_on_an_edge_match(self):
        w = self.make()
        orig = laundry.best_shift
        laundry.best_shift = lambda cur, ref, m: (-64, 48, 30.0, True)
        try:
            self.assertTrue(w._align(b"ref")[3])
        finally:
            laundry.best_shift = orig
        self.assertEqual(self.cams.moves, [])

    def test_notify_failure_still_shows_the_banner(self):
        w = self.make()
        self.home.fail = True
        self.cams.laundry = {(5, 5)}  # line1
        w._run("x", "")
        self.assertIn("la puerta", w.status()["alert"]["text"])

    def test_person_moving_interrupts(self):
        w = self.make()
        self.cams.laundry = {(12, 4)}

        def person(n):
            if n == 5:
                self.mono[0] += 1
                self.cams.last_person_move["v380"] = self.mono[0]
        self.cams.on_move = person
        w._run("x", "")
        last = w.status()["last"]
        self.assertEqual(last["result"], "interrupted")
        self.assertIsNone(w.status()["alert"])
        self.assertEqual(len(self.cams.moves), 5)  # stopped at once, didn't drive home over the person

    def test_waits_for_a_person_then_skips(self):
        w = self.make()
        self.cams.last_person_move["v380"] = self.mono[0]  # sleep() doesn't advance time: always "just now"
        w._run("x", "")
        self.assertEqual(w.status()["last"]["result"], "skipped")
        self.assertEqual(self.cams.moves, [])

    def test_alert_expires(self):
        w = self.make()
        self.cams.laundry = {(12, 4)}
        w._run("x", "")
        self.clock.t += 3 * 3600 + 1
        self.assertIsNone(w.status()["alert"])

    def test_saves_frames_and_keeps_the_newest(self):
        w = self.make(save_frames_dir="checks", keep_frames=1)
        w._run("x", "")
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "checks"))), 1)


class TriggerTest(LaundryTestBase):
    def setUp(self):
        self.make()
        self.started = []
        self.watch.start = lambda why, why_es="": self.started.append(why) or True

    def test_one_check_per_rain_spell(self):
        self.watch.on_weather(rain())
        self.clock.t += 3600 * 2
        self.watch.on_weather(rain())
        self.assertEqual(len(self.started), 1)
        self.clock.t += 3600 * 5  # past repeat_hours
        self.watch.on_weather(rain())
        self.assertEqual(len(self.started), 2)

    def test_rain_gone_and_back_rechecks_after_min_gap(self):
        self.watch.on_weather(rain())
        self.clock.t += 1800
        self.watch.on_weather(rain(0))
        self.watch.on_weather(rain())
        self.assertEqual(len(self.started), 1)  # inside min_gap_minutes
        self.clock.t += 3600
        self.watch.on_weather(rain())
        self.assertEqual(len(self.started), 2)

    def test_dark_or_disabled(self):
        self.watch.on_weather(rain(is_day=False))
        self.watch.cfg["enabled"] = False
        self.watch.on_weather(rain())
        self.assertEqual(self.started, [])

    def test_bad_data_never_raises(self):
        self.watch.on_weather({"current": None, "next_hours": "junk"})


class ConfigTest(unittest.TestCase):
    def test_defaults_and_sample_are_valid(self):
        laundry.validate_laundry_config(laundry.DEFAULT_LAUNDRY_CONFIG)
        laundry.validate_laundry_config(config())

    def test_rejects_bad_values(self):
        bad = [
            {"home": ["sideways 3"]},
            {"notify_services": ["light.turn_on"]},
            {"rain_hours": 9},
            {"lines": [dict(config()["lines"][0], boxes=[{"box": [0, 0, 2000, 10], "saturation": 30}])]},
            {"lines": [config()["lines"][0], config()["lines"][0]]},
        ]
        for over in bad:
            with self.assertRaises(ValueError, msg=over):
                laundry.validate_laundry_config(config(**over))


if __name__ == "__main__":
    unittest.main()
