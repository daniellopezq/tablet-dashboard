"""Away mode (away.py) and its Home tab button, against a fake Home Assistant; no network."""
import json
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import away  # noqa: E402
import homeai  # noqa: E402
from home import ActionError, Home, DEFAULT_HOME_CONFIG, validate_home_config  # noqa: E402

LIGHTS = [
    {"entity_id": "light.main", "label": "Recámara principal", "role": "room"},
    {"entity_id": "switch.plug", "label": "Recámara plug", "role": "room"},
    {"entity_id": "light.bath1", "label": "Baño 1", "role": "short"},
    {"entity_id": "light.bath2", "label": "Baño 2", "role": "short"},
]
# 2026-10-26 18:00 local, in UTC so the tests don't depend on the machine's timezone.
EVENING = 1793037600.0


def utc(t):
    return time.gmtime(t)


def make_cfg(**kw):
    cfg = json.loads(json.dumps(away.DEFAULT_AWAY_CONFIG))
    cfg["lights"] = json.loads(json.dumps(LIGHTS))
    cfg.update(kw)
    return cfg


class FakeHome:
    def __init__(self):
        self.sun = "below_horizon"
        self.calls = []
        self.fail = False
        self.up = True

    def ha_rows(self, fresh=False):
        return {"sun.sun": {"state": self.sun}} if self.up else None

    def ha_service(self, service, entity_id):
        if self.fail:
            raise ActionError("Home Assistant is unreachable (refused)", 503)
        self.calls.append((service, entity_id))


class NoCameras:
    def ai_cameras(self):
        return []


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def lit_at(events, t):
    on = set()
    for when, e, want in events:
        if when > t:
            break
        (on.add if want == "on" else on.discard)(e)
    return on


class PlanTests(unittest.TestCase):
    def test_plan_shape(self):
        cfg = make_cfg()
        for seed in range(40):
            start, bed = EVENING + 1200, EVENING + 5.5 * 3600
            ev = away.plan_night(cfg, start, bed, random.Random(seed))
            self.assertEqual(ev, sorted(ev))
            self.assertTrue(all(start <= t <= bed for t, _, _ in ev), seed)
            per = {}
            for t, e, want in ev:
                per.setdefault(e, []).append((t, want))
            for e, rows in per.items():
                wants = [w for _, w in rows]
                self.assertEqual(wants, ["on", "off"] * (len(wants) // 2), (seed, e))   # never on twice
            self.assertEqual(lit_at(ev, bed), set(), seed)    # dark at bedtime
            spans = []
            for e in ("light.main", "switch.plug"):
                rows = per.get(e, [])
                spans += [(rows[i][0], rows[i + 1][0]) for i in range(0, len(rows), 2)]
            for on, off in spans:
                if off >= bed - 1:
                    continue      # the last one is cut short at bedtime
                self.assertGreaterEqual(off - on, 40 * 60 - 1)
                self.assertLessEqual(off - on, 60 * 60 + 1)
            self.assertTrue(any(e.startswith("light.bath") for _, e, _ in ev), seed)

    def test_rooms_take_turns(self):
        ev = away.plan_night(make_cfg(), EVENING, EVENING + 5 * 3600, random.Random(3))
        ons = [e for _, e, w in ev if w == "on" and e in ("light.main", "switch.plug")]
        self.assertTrue(all(a != b for a, b in zip(ons, ons[1:])))

    def test_rooms_overlap_sometimes(self):
        long_overlaps = 0
        for seed in range(30):
            ev = away.plan_night(make_cfg(), EVENING, EVENING + 5 * 3600, random.Random(seed))
            both = 0
            for (t, _, _), (t2, _, _) in zip(ev, ev[1:]):
                if {"light.main", "switch.plug"} <= lit_at(ev, t):
                    both += t2 - t
            long_overlaps += both >= 10 * 60
        self.assertGreater(long_overlaps, 10)
        never = away.plan_night(make_cfg(overlap_chance=0, gap_minutes=[1, 5]), EVENING, EVENING + 5 * 3600,
                                random.Random(1))
        self.assertTrue(all(len(lit_at(never, t) & {"light.main", "switch.plug"}) < 2 for t, _, _ in never))

    def test_nights_differ(self):
        a = away.plan_night(make_cfg(), EVENING, EVENING + 5 * 3600, random.Random(1))
        b = away.plan_night(make_cfg(), EVENING, EVENING + 5 * 3600, random.Random(2))
        self.assertNotEqual(a, b)

    def test_single_room_light_never_overlaps_itself(self):
        cfg = make_cfg(lights=[LIGHTS[0]], gap_minutes=[-10, -5])
        ev = away.plan_night(cfg, EVENING, EVENING + 5 * 3600, random.Random(0))
        self.assertEqual([w for _, _, w in ev], ["on", "off"] * (len(ev) // 2))


class AwayModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.home = FakeHome()
        self.clock = Clock(EVENING)

    def make(self, **kw):
        return away.AwayMode(make_cfg(**kw), self.home, self.dir, clock=self.clock, rng=random.Random(7),
                             localtime=utc)

    def run_night(self, a, until, step=20):
        while self.clock.t < until:
            a.tick()
            self.clock.t += step

    def test_off_does_nothing(self):
        a = self.make()
        self.run_night(a, EVENING + 3600)
        self.assertEqual(self.home.calls, [])
        self.assertIsNone(a.plan)

    def test_waits_for_sunset(self):
        self.home.sun = "above_horizon"
        a = self.make()
        a.set(True)
        self.run_night(a, EVENING + 1800)
        self.assertIsNone(a.plan)
        self.assertEqual(a.hint(), "Waits for sunset")
        self.home.sun = "below_horizon"
        a.tick()
        self.assertIsNotNone(a.plan)
        self.assertTrue(23 * 3600 <= a.plan["bedtime"] - (EVENING - 18 * 3600) <= 24 * 3600)

    def test_whole_night(self):
        a = self.make(bedtime_to="23:30")
        a.set(True)
        self.run_night(a, EVENING + 5.9 * 3600)   # to 23:54
        self.assertGreater(len(self.home.calls), 6)
        self.assertEqual(a.lit, [])
        services = {s for s, _ in self.home.calls}
        self.assertLessEqual(services, {"light.turn_on", "light.turn_off", "switch.turn_on", "switch.turn_off"})
        self.assertEqual(a.hint(), "Done for tonight")
        last_off = max(i for i, c in enumerate(self.home.calls) if c[0].endswith("turn_off"))
        self.assertEqual(last_off, len(self.home.calls) - 1)

    def test_turning_off_turns_its_lights_off(self):
        a = self.make()
        a.set(True)
        self.run_night(a, EVENING + 2 * 3600)
        self.assertTrue(a.lit)
        lit = list(a.lit)
        self.home.calls.clear()
        self.assertTrue(a.set(False))
        self.assertEqual(sorted(e for _, e in self.home.calls), sorted(lit))
        self.assertEqual(a.lit, [])
        self.assertFalse(a.set(False))

    def test_off_retries_when_ha_was_down(self):
        a = self.make()
        a.set(True)
        self.run_night(a, EVENING + 2 * 3600)
        self.home.fail = True
        a.set(False)
        self.assertTrue(a.lit)
        self.home.fail = False
        a.tick()
        self.assertEqual(a.lit, [])

    def test_failed_call_retried_then_given_up(self):
        a = self.make()
        a.set(True)
        a.tick()
        first = a.plan["events"][0]
        self.home.fail = True
        self.clock.t = first[0] + 30
        a.tick()
        self.assertFalse(first[3])
        self.assertIn("unreachable", a.error)
        self.clock.t = first[0] + 21 * 60
        a.tick()
        self.assertTrue(first[3])      # too late now
        self.assertEqual(a.lit, [])

    def test_state_survives_restart(self):
        a = self.make()
        a.set(True)
        self.run_night(a, EVENING + 2 * 3600)
        lit = list(a.lit)
        b = self.make()
        self.assertTrue(b.on)
        self.assertEqual(b.lit, lit)
        self.assertIsNone(b.plan)
        b.set(False)
        self.assertEqual(self.make().state(), "off")

    def test_turned_on_after_bedtime(self):
        self.clock.t = EVENING + 6 * 3600 - 60   # 23:59
        a = self.make(bedtime_from="23:00", bedtime_to="23:30")
        a.set(True)
        a.tick()
        self.assertEqual(a.plan["events"], [])
        self.assertEqual(a.hint(), "Done for tonight")

    def test_next_night_new_plan(self):
        a = self.make()
        a.set(True)
        self.run_night(a, EVENING + 3600)
        first = a.plan["night"]
        self.clock.t = EVENING + 24 * 3600 + 1800
        a.tick()
        self.assertNotEqual(a.plan["night"], first)

    def test_ha_down(self):
        self.home.up = False
        a = self.make()
        a.set(True)
        a.tick()
        self.assertIsNone(a.plan)
        self.assertEqual(a.hint(), "Home Assistant unreachable")

    def test_status(self):
        a = self.make()
        a.set(True)
        a.tick()
        st = a.status()
        self.assertTrue(st["on"])
        self.assertIn(st["tonight"]["next"]["label"], [l["label"] for l in LIGHTS])
        self.assertTrue(a.hint().startswith("Next: "))


class ValidationTests(unittest.TestCase):
    def test_default_and_house(self):
        away.validate_away_config(away.DEFAULT_AWAY_CONFIG)
        away.validate_away_config(make_cfg())

    def test_bad(self):
        for bad in ({"lights": [dict(LIGHTS[0], entity_id="script.x")]},
                    {"lights": [LIGHTS[0], LIGHTS[0]]},
                    {"lights": [LIGHTS[2]]},
                    {"lights": [dict(LIGHTS[0], role="kitchen")]},
                    {"room_minutes": [60, 40]},
                    {"room_minutes": [40]},
                    {"bedtime_from": "11:00"},
                    {"bedtime_to": "25:00"},
                    {"bedtime_from": "23:30", "bedtime_to": "23:00"},
                    {"tick_seconds": 0},
                    {"overlap_chance": 2},
                    {"overlap_minutes": [30, 10]},
                    {"enabled": "yes"}):
            with self.assertRaises(ValueError, msg=bad):
                away.validate_away_config(make_cfg(**bad))


class ButtonTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        cfg = json.loads(json.dumps(DEFAULT_HOME_CONFIG))
        cfg["buttons"] = [{"id": "away", "label": "Modo fuera", "kind": "away", "icon": "home",
                           "aliases": ["modo fuera"]}]
        cfg["min_interval_seconds"] = 0
        validate_home_config(cfg)
        self.home = Home(cfg, self.dir)
        self.fake = FakeHome()
        self.away = away.AwayMode(make_cfg(), self.fake, self.dir, clock=Clock(EVENING), localtime=utc)
        self.home.away = self.away

    def test_validation(self):
        cfg = json.loads(json.dumps(DEFAULT_HOME_CONFIG))
        cfg["buttons"] = [{"id": "a", "label": "A", "kind": "away"}, {"id": "b", "label": "B", "kind": "away"}]
        with self.assertRaises(ValueError):
            validate_home_config(cfg)

    def test_tile_and_press(self):
        item = self.home.status()["buttons"][0]
        self.assertEqual((item["state"], item["hint"]), ("off", ""))
        self.home.action("away")
        self.assertTrue(self.away.on)
        item = self.home.status()["buttons"][0]
        self.assertEqual((item["state"], item["hint"]), ("on", "Waits for sunset"))
        self.home.action("away")
        self.assertFalse(self.away.on)

    def test_set_state(self):
        self.assertEqual(self.home.set_state("away", "on"), {"ok": True, "changed": True, "state": "on"})
        self.assertEqual(self.home.set_state("away", "on")["changed"], False)
        self.assertEqual(self.home.button_state(self.home.button("away")), "on")

    def test_not_running(self):
        self.home.away = None
        with self.assertRaises(ActionError):
            self.home.action("away")
        self.assertIsNone(self.home.status()["buttons"][0]["state"])

    def test_voice(self):
        house = homeai.House(self.home, cameras=NoCameras())
        m = house.match("activa el modo fuera")
        self.assertEqual(m, {"type": "actions", "actions": [("away", "on")]})
        say, done, confirm = house.run_actions(m["actions"], "es")
        self.assertEqual(say, "Modo fuera: activado.")
        self.assertTrue(self.away.on)
        self.assertIn("Modo fuera: activado.", house.speech_phrases())
        self.assertIn("away mode", house.context_text())


if __name__ == "__main__":
    unittest.main()
