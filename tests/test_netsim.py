"""Tests for the offline test switch (netsim.py) and its hooks in fallback.py / assistant.py.

Run: python3 -m unittest discover -s tests -v. Nothing leaves this machine.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fallback as fb  # noqa: E402
import netsim  # noqa: E402
import upstream  # noqa: E402


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class CountingOpener:
    def __init__(self):
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        raise OSError("connection refused (test)")


class SimTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="netsim-test-")
        self.path = os.path.join(self.tmp, "offline-test.json")
        self.clock = FakeClock()
        self.sim = netsim.install(self.path, self.clock)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(setattr, netsim, "SIM", None)


class SwitchTests(SimTestCase):
    def test_off_by_default(self):
        self.assertFalse(self.sim.state()["active"])
        self.assertIsNone(netsim.blocked("ask"))

    def test_on_then_off(self):
        st = self.sim.turn_on(["voice", "ask"], "instant", 10)
        self.assertTrue(st["active"])
        self.assertEqual(st["services"], ["ask", "voice"])  # canonical order
        self.assertEqual(st["remaining_seconds"], 600)
        self.assertEqual(netsim.blocked("ask"), "instant")
        self.assertIsNone(netsim.blocked("weather"))
        self.assertFalse(self.sim.turn_off()["active"])
        self.assertFalse(os.path.exists(self.path))

    def test_expires_and_removes_file(self):
        fired = []
        self.sim.on_change(fired.append)
        self.sim.turn_on("ask,maps", "realistic", 1)
        self.assertEqual(fired, [True])
        self.clock.t += 61
        self.assertIsNone(netsim.blocked("ask"))
        self.assertEqual(fired, [True, False])
        self.assertFalse(os.path.exists(self.path))

    def test_external_file_change_is_seen(self):
        # scripts/offline-test.sh writes the same file from another process
        with open(self.path, "w") as f:
            json.dump({"services": ["calendar"], "mode": "instant", "until": self.clock.t + 30}, f)
        self.assertEqual(netsim.blocked("calendar"), "instant")

    def test_bad_file_is_off(self):
        for text in ("not json", json.dumps({"services": ["internet"], "mode": "instant", "until": 9e12}),
                     json.dumps({"services": ["ask"], "mode": "chaos", "until": 9e12}),
                     json.dumps({"services": ["ask"], "mode": "instant", "until": "soon"})):
            with self.subTest(text=text):
                with open(self.path, "w") as f:
                    f.write(text + " " * len(text))  # size changes too, so the cache can't hide it
                self.assertIsNone(netsim.blocked("ask"))

    def test_validation(self):
        for args in (([], "instant", 5), (["ask"], "instant", 0), (["ask"], "instant", 121),
                     (["dns"], "instant", 5), (["ask"], "loud", 5), (["ask"], "instant", True)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.sim.turn_on(*args)


class HookTests(SimTestCase):
    def req(self):
        return urllib.request.Request("http://192.0.2.1/test")

    def test_instant_fails_without_network(self):
        inner = CountingOpener()
        cloud = fb.HeaderDeadlineOpener(inner, "ask")
        self.sim.turn_on(["ask"], "instant", 5)
        with self.assertRaises(fb.NoResponse) as cm:
            cloud.open(self.req(), 2, 5)
        self.assertEqual(cm.exception.reason, "simulated")
        self.assertEqual(inner.calls, 0)

    def test_realistic_waits_for_the_deadline(self):
        inner = CountingOpener()
        cloud = fb.HeaderDeadlineOpener(inner, "voice")
        self.sim.turn_on(["voice"], "realistic", 5)
        t0 = time.monotonic()
        with self.assertRaises(fb.NoResponse) as cm:
            cloud.open(self.req(), 0.3, 0.6)
        waited = time.monotonic() - t0
        self.assertEqual(cm.exception.reason, "deadline")
        self.assertGreaterEqual(waited, 0.28)
        self.assertEqual(inner.calls, 0)
        # The "hung" request is still outstanding, like a real black hole: the next attempt is a straggler.
        with self.assertRaises(fb.NoResponse) as cm:
            cloud.open(self.req(), 0.3, 0.6)
        self.assertEqual(cm.exception.reason, "straggler")

    def test_other_services_are_untouched(self):
        inner = CountingOpener()
        cloud = fb.HeaderDeadlineOpener(inner, "ask")
        self.sim.turn_on(["voice"], "instant", 5)
        with self.assertRaises(fb.NoResponse) as cm:
            cloud.open(self.req(), 2, 5)
        self.assertEqual(cm.exception.reason, "connect")  # the real (refused) connection
        self.assertEqual(inner.calls, 1)

    def test_fallback_disabled_gets_network_error(self):
        inner = CountingOpener()
        cloud = fb.HeaderDeadlineOpener(inner, "ask")
        self.sim.turn_on(["ask"], "instant", 5)
        with self.assertRaises(upstream.UpstreamError) as cm:
            upstream.post_bounded(inner, cloud, self.req(), 5, 1000, no_response_seconds=None)
        self.assertEqual(cm.exception.category, "network_error")
        self.assertEqual(inner.calls, 0)

    def test_gate_reset(self):
        gate = fb.CloudGate(60)
        self.assertTrue(gate.begin())
        gate.finish(False)
        self.assertTrue(gate.cooling_down())
        gate.reset()
        self.assertFalse(gate.cooling_down())
        self.assertTrue(gate.begin())


if __name__ == "__main__":
    unittest.main()
