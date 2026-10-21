"""Tests for the Home tab backend (home.py): config validation, the Plex/Jellyfin switch (with a
fake `subprocess.run`) and the Home Assistant client (against a local mock HA).

Run: python3 -m unittest discover -s tests -v. Nothing leaves this machine; nothing is switched.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import home  # noqa: E402

TOKEN = "eyJhbGciOiJIUzI1NiJ9.test-token.sig"


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSystem:
    """Stands in for subprocess.run: systemctl is-active and sudo -n media-switch."""

    def __init__(self, current="plex", sudo_ok=True, fail_start=False):
        self.active = {"plexmediaserver": current == "plex", "jellyfin": current == "jellyfin"}
        self.sudo_ok = sudo_ok
        self.fail_start = fail_start
        self.calls = []
        self.gate = None  # threading.Event: block the switch until set

    def __call__(self, argv, capture_output=True, text=True, timeout=None):
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "is-active"]:
            out = "\n".join("active" if self.active[u] else "inactive" for u in argv[2:])
            return subprocess.CompletedProcess(argv, 0, out + "\n", "")
        if argv[:3] == ["sudo", "-n", home.SWITCH_CMD]:
            if not self.sudo_ok:
                return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")
            if self.gate:
                self.gate.wait(5)
            on = home.MEDIA[argv[3]]
            for u in self.active:
                self.active[u] = (u == on) and not self.fail_start
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError("unexpected command %r" % argv)


class MockHA(BaseHTTPRequestHandler):
    calls = []
    states = {"switch.desk_lamp": "off"}

    def _auth(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self._send(401, b'{"message":"unauthorized"}')
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/api/":
            return self._send(200, b'{"message":"API running."}')
        if self.path == "/api/states":
            rows = [{"entity_id": k, "state": v} for k, v in MockHA.states.items()]
            return self._send(200, json.dumps(rows).encode())
        ent = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/api/states/") and ent in MockHA.states:
            return self._send(200, json.dumps({"entity_id": ent, "state": MockHA.states[ent]}).encode())
        self._send(404, b'{"message":"Entity not found."}')

    def do_POST(self):
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        MockHA.calls.append((self.path, body))
        if self.path == "/api/services/switch/toggle":
            ent = body["entity_id"]
            MockHA.states[ent] = "on" if MockHA.states.get(ent) == "off" else "off"
        self._send(200, b"[]")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def make_cfg(**over):
    cfg = copy.deepcopy(home.DEFAULT_HOME_CONFIG)
    cfg.update(over)
    return cfg


class ValidationTests(unittest.TestCase):
    def test_default_is_valid(self):
        home.validate_home_config(make_cfg())

    def test_ha_button(self):
        ok = {"id": "lamp", "label": "Desk lamp", "kind": "ha", "service": "switch.toggle",
              "entity_id": "switch.desk_lamp", "state_entity": "switch.desk_lamp", "icon": "light"}
        home.validate_home_config(make_cfg(buttons=[ok]))
        for bad in ({"service": "switch/toggle"}, {"entity_id": "Desk Lamp"}, {"kind": "shell"},
                    {"id": "Has Space"}, {"label": ""}, {"confirm": "yes"}, {"icon": "../x"},
                    {"state_entity": "nope"}):
            b = dict(ok, **bad)
            with self.assertRaises(ValueError, msg=bad):
                home.validate_home_config(make_cfg(buttons=[b]))

    def test_unique_ids_and_single_switch(self):
        m = {"id": "media", "label": "Media", "kind": "media_switch"}
        with self.assertRaises(ValueError):
            home.validate_home_config(make_cfg(buttons=[m, dict(m)]))
        with self.assertRaises(ValueError):
            home.validate_home_config(make_cfg(buttons=[m, dict(m, id="media2")]))

    def test_bad_url_and_token_name(self):
        with self.assertRaises(ValueError):
            home.validate_home_config(make_cfg(ha_url="file:///etc/passwd"))
        with self.assertRaises(ValueError):
            home.validate_home_config(make_cfg(ha_token_name="ha token"))


class MediaSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="home-test-")
        self.clock = FakeClock()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make(self, sysobj, **cfg):
        return home.Home(make_cfg(**cfg), self.tmp, runner=sysobj, clock=self.clock)

    def test_status(self):
        h = self.make(FakeSystem("plex"))
        st = h.media_status()
        self.assertEqual((st["plex"], st["jellyfin"], st["current"]), ("active", "inactive", "plex"))
        self.assertEqual(h.intentionally_stopped(), {"Jellyfin"})

    def press(self, h):
        """Press the media button and wait for the background switch; return the button's status."""
        r = h.action("media")
        self.assertTrue(r["pending"])
        h._thread.join(5)
        self.clock.t += 10
        return next(b for b in h.status()["buttons"] if b["id"] == "media")["media"]

    def test_toggle_both_ways(self):
        s = FakeSystem("plex")
        h = self.make(s)
        self.assertEqual(self.press(h)["current"], "jellyfin")
        self.assertIn(["sudo", "-n", home.SWITCH_CMD, "jellyfin"], s.calls)
        self.assertEqual(self.press(h)["current"], "plex")
        self.assertIsNone(h.status()["buttons"][0]["media"]["error"])

    def test_neither_running_switches_to_plex(self):
        h = self.make(FakeSystem("none"))
        self.assertEqual(self.press(h)["current"], "plex")

    def test_not_installed(self):
        h = self.make(FakeSystem("plex", sudo_ok=False))
        with self.assertRaises(home.ActionError) as cm:
            h.media_switch()
        self.assertEqual(cm.exception.code, 503)
        self.assertIn("not installed", str(cm.exception))
        st = self.press(h)  # the background path reports the same message in the status
        self.assertEqual(st["current"], "plex")
        self.assertIn("not installed", st["error"])

    def test_start_failure_reported(self):
        h = self.make(FakeSystem("plex", fail_start=True))
        with self.assertRaises(home.ActionError) as cm:
            h.media_switch()
        self.assertIn("Jellyfin did not start", str(cm.exception))

    def test_one_switch_at_a_time(self):
        s = FakeSystem("plex")
        s.gate = threading.Event()
        h = self.make(s, min_interval_seconds=0)
        t = threading.Thread(target=h.media_switch)
        t.start()
        for _ in range(200):
            if h._switching:
                break
            threading.Event().wait(0.01)
        self.assertEqual(h.media_status()["switching"], "jellyfin")
        with self.assertRaises(home.ActionError) as cm:
            h.media_switch()
        self.assertEqual(cm.exception.code, 409)
        s.gate.set()
        t.join(5)
        self.assertIsNone(h.media_status()["switching"])

    def test_rate_limit(self):
        h = self.make(FakeSystem("plex"))
        h.action("media")
        h._thread.join(5)
        with self.assertRaises(home.ActionError) as cm:
            h.action("media")
        self.assertEqual(cm.exception.code, 429)

    def test_rate_limit_is_per_button(self):
        cfg = make_cfg(buttons=[{"id": "media", "label": "Media", "kind": "media_switch"},
                                {"id": "x", "label": "X", "kind": "ha", "service": "switch.toggle",
                                 "entity_id": "switch.x"}])
        h = home.Home(cfg, self.tmp, runner=FakeSystem("plex"), clock=self.clock)
        h.action("media")
        h._thread.join(5)
        with self.assertRaises(home.ActionError) as cm:
            h.action("x")  # not rate limited; fails only because there is no HA token here
        self.assertEqual(cm.exception.code, 503)

    def test_unknown_button_and_disabled(self):
        h = self.make(FakeSystem("plex"))
        with self.assertRaises(home.ActionError) as cm:
            h.action("nope")
        self.assertEqual(cm.exception.code, 404)
        h2 = self.make(FakeSystem("plex"), enabled=False)
        self.assertEqual(h2.status(), {"enabled": False, "buttons": []})
        with self.assertRaises(home.ActionError):
            h2.action("media")


class HomeAssistantTests(unittest.TestCase):
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
        self.tmp = tempfile.mkdtemp(prefix="home-test-")
        self.env = os.path.join(self.tmp, ".env")
        fd = os.open(self.env, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, ("HA_TOKEN=%s\n" % TOKEN).encode())
        os.close(fd)
        MockHA.calls[:] = []
        MockHA.states = {"switch.desk_lamp": "off"}
        self.clock = FakeClock()
        self.buttons = [
            {"id": "lamp", "label": "Desk lamp", "kind": "ha", "service": "switch.toggle",
             "entity_id": "switch.desk_lamp", "state_entity": "switch.desk_lamp"},
            {"id": "night", "label": "Movie night", "kind": "ha", "service": "script.turn_on",
             "entity_id": "script.movie_night", "confirm": True},
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make(self, **cfg):
        c = make_cfg(**dict({"ha_url": self.url, "buttons": self.buttons}, **cfg))
        home.validate_home_config(c)
        return home.Home(c, self.tmp, runner=FakeSystem(), clock=self.clock)

    def test_status_and_toggle(self):
        h = self.make()
        st = h.status()
        self.assertTrue(st["ha_ok"])
        self.assertEqual(st["buttons"][0]["state"], "off")
        self.assertTrue(st["buttons"][1]["confirm"])
        h.action("lamp")
        self.assertEqual(MockHA.calls[-1], ("/api/services/switch/toggle", {"entity_id": "switch.desk_lamp"}))
        self.assertEqual(h.status()["buttons"][0]["state"], "on")  # cache cleared by the call

    def test_missing_entity_and_connection_test(self):
        self.buttons[0]["state_entity"] = "switch.gone"
        h = self.make()
        self.assertEqual(h.status()["buttons"][0]["state"], "missing")
        self.assertEqual(h.test_connection(), {"ok": True, "message": "API running."})

    def test_script_call(self):
        h = self.make()
        h.action("night")
        self.assertEqual(MockHA.calls[-1], ("/api/services/script/turn_on", {"entity_id": "script.movie_night"}))

    def test_no_token(self):
        os.remove(self.env)
        h = self.make()
        self.assertFalse(h.status()["ha_ok"])
        with self.assertRaises(home.ActionError) as cm:
            h.action("lamp")
        self.assertEqual(cm.exception.code, 503)
        self.assertIn("not connected", str(cm.exception))
        self.assertIn("no Home Assistant token", h.status()["ha_error"])

    def test_bad_token(self):
        with open(self.env, "w") as f:
            f.write("HA_TOKEN=wrong-token-1234\n")
        h = self.make()
        with self.assertRaises(home.ActionError) as cm:
            h.action("lamp")
        self.assertIn("check the HA token", str(cm.exception))
        self.assertNotIn("wrong-token", str(cm.exception))

    def test_ha_down(self):
        h = self.make(ha_url="http://127.0.0.1:1")
        self.assertFalse(h.test_connection()["ok"])
        self.assertFalse(h.status()["ha_ok"])
        with self.assertRaises(home.ActionError) as cm:
            h.action("lamp")
        self.assertIn("unreachable", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
