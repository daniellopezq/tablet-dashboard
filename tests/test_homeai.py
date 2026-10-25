"""Tests for "Ask the house" (0.22.0): homeai.py (matcher, actions, confirmation, cameras), Home.set_state,
the assistant's house/JSON/memory/camera paths, the cameras' background refresh, and tts.py.

All traffic goes to local mocks (Home Assistant, AI Gateway, a fake Piper); nothing leaves this machine
and nothing in the house is switched. Run: python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant as am  # noqa: E402
import cameras  # noqa: E402
import home  # noqa: E402
import homeai  # noqa: E402
import tts  # noqa: E402
from test_assistant import AssistantTestCase, MockGateway, TZ  # noqa: E402
from test_home import TOKEN, FakeClock, FakeSystem, MockHA  # noqa: E402

BUTTONS = [
    {"id": "media", "label": "Media server", "kind": "media_switch", "confirm": True},
    {"id": "alarm", "label": "Alarma Casa", "kind": "ha", "on_service": "alarm_control_panel.alarm_arm_away",
     "off_service": "alarm_control_panel.alarm_disarm", "entity_id": "alarm_control_panel.casa",
     "state_entity": "alarm_control_panel.casa", "confirm": True, "aliases": ["alarma", "alarm"]},
    {"id": "fan", "label": "Abanico", "kind": "ha", "service": "switch.toggle", "entity_id": "switch.fan",
     "state_entity": "switch.fan", "aliases": ["ventilador", "fan"]},
    {"id": "room", "label": "Recámara plug", "kind": "ha", "service": "switch.toggle",
     "entity_id": "switch.room", "state_entity": "switch.room"},
    {"id": "room_main", "label": "Recámara principal", "kind": "ha", "service": "light.toggle",
     "entity_id": "light.room_main", "state_entity": "light.room_main"},
    {"id": "night", "label": "Movie night", "kind": "ha", "service": "script.turn_on",
     "entity_id": "script.movie_night"},
]
STATES = {"alarm_control_panel.casa": "disarmed", "switch.fan": "on", "switch.room": "off",
          "light.room_main": "off"}


class FakeCameras:
    def __init__(self):
        self.cams = [{"id": "street", "label": "V380", "aliases": ["calle", "street"], "live": True},
                     {"id": "sala", "label": "Sala", "aliases": [], "live": False}]
        self.asked = []
        self.fail = False

    def ai_cameras(self):
        return [dict(c) for c in self.cams]

    def frame_for_ai(self, cid, max_age=60):
        self.asked.append((cid, max_age))
        if self.fail:
            raise home.ActionError("camera not answering", 504)
        return b"\xff\xd8fake-jpeg\xff\xd9", "image/jpeg", 0 if cid == "street" else 45


class HomeFixture:
    """A real home.Home against the mock Home Assistant (states reset per test)."""

    @classmethod
    def start_ha(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHA)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.ha_url = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def stop_ha(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def make_home(self, tmp, system=None):
        env = os.path.join(tmp, ".env")
        fd = os.open(env, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, ("HA_TOKEN=%s\n" % TOKEN).encode())
        os.close(fd)
        MockHA.calls[:] = []
        MockHA.states = dict(STATES)
        cfg = json.loads(json.dumps(home.DEFAULT_HOME_CONFIG))
        cfg.update(ha_url=self.ha_url, buttons=json.loads(json.dumps(BUTTONS)), min_interval_seconds=0)
        home.validate_home_config(cfg)
        self.system = system or FakeSystem("plex")
        return home.Home(cfg, tmp, runner=self.system, clock=FakeClock())


class MatcherTests(HomeFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.start_ha()

    @classmethod
    def tearDownClass(cls):
        cls.stop_ha()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="homeai-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.make_home(self.tmp)
        self.cams = FakeCameras()
        self.house = homeai.House(self.home, self.cams, clock=FakeClock())

    def m(self, text, last=None):
        return self.house.match(text, last)

    def test_commands_es_en_accents_aliases(self):
        self.assertEqual(self.m("Apaga el abanico"), {"type": "actions", "actions": [("fan", "off")]})
        self.assertEqual(self.m("prende el ventilador por favor"), {"type": "actions", "actions": [("fan", "on")]})
        self.assertEqual(self.m("turn the fan off"), {"type": "actions", "actions": [("fan", "off")]})
        self.assertEqual(self.m("Turn on the RECAMARA"), {"type": "actions", "actions": [("room", "on")]})
        self.assertEqual(self.m("apaga la recámara principal"), {"type": "actions", "actions": [("room_main", "off")]})
        self.assertEqual(self.m("arma la alarma"), {"type": "actions", "actions": [("alarm", "on")]})
        self.assertEqual(self.m("desarma la alarma"), {"type": "actions", "actions": [("alarm", "off")]})
        self.assertEqual(self.m("cambia a jellyfin el media server"), {"type": "actions", "actions": [("media", "jellyfin")]})
        self.assertEqual(self.m("enciende movie night"), {"type": "actions", "actions": [("night", "press")]})

    def test_several_buttons_need_and(self):
        self.assertEqual(self.m("apaga el abanico y la recamara principal"),
                         {"type": "actions", "actions": [("fan", "off"), ("room_main", "off")]})
        self.assertIsNone(self.m("apaga abanico recamara principal"))

    def test_left_to_the_ai(self):
        for text in ("", "what is 2+2?", "apaga todo", "¿está lloviendo?", "pon algo de música",
                     "turn on the fan and turn off the recamara", "abanico"):
            self.assertIsNone(self.m(text), text)

    def test_state_questions(self):
        self.assertEqual(self.m("¿está prendido el abanico?"), {"type": "state", "ids": ["fan"]})
        self.assertEqual(self.m("is the alarm armed?"), {"type": "state", "ids": ["alarm"]})
        self.assertEqual(self.m("¿qué está prendido?"), {"type": "state", "ids": []})
        self.assertEqual(self.m("what's on?"), {"type": "state", "ids": []})
        self.assertEqual(self.house.state_answer([], "es"), "Prendido: Abanico.")
        self.assertEqual(self.house.state_answer(["alarm", "fan"], "en"), "Alarma Casa: disarmed. Abanico is on.")
        self.assertEqual(self.house.state_answer(["media"], "es"), "El servidor activo es Plex.")

    def test_look_picks_camera(self):
        self.assertEqual(self.m("¿qué ves en la calle?"), {"type": "look", "camera": "street"})
        self.assertEqual(self.m("is anyone in the sala?"), {"type": "look", "camera": "sala"})
        self.assertIsNone(self.m("what do you see?"))
        self.assertEqual(self.m("¿y sigue ahí?", last="street"), {"type": "look", "camera": "street"})

    def test_context_block(self):
        text = self.house.context_text("local time Thu")
        self.assertIn('- fan "Abanico" [on/off]: on also called: ventilador, fan', text)
        self.assertIn('- alarm "Alarma Casa" [alarm, needs confirmation]: off', text)
        self.assertIn('- media "Media server" [media switch Plex/Jellyfin, needs confirmation]: plex', text)
        self.assertIn('- night "Movie night" [press only]: unknown', text)
        self.assertIn('- street "V380" [live] also called: calle, street', text)
        self.assertNotIn("switch.fan", text)  # no entity ids for the model


class ActionTests(HomeFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.start_ha()

    @classmethod
    def tearDownClass(cls):
        cls.stop_ha()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="homeai-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.make_home(self.tmp)
        self.clock = FakeClock()
        self.house = homeai.House(self.home, FakeCameras(), clock=self.clock)

    def test_set_state_never_toggles_blindly(self):
        r = self.home.set_state("fan", "on")  # already on
        self.assertEqual(r, {"ok": True, "changed": False, "state": "on"})
        self.assertEqual(MockHA.calls, [])
        r = self.home.set_state("fan", "off")
        self.assertTrue(r["changed"])
        self.assertEqual(MockHA.calls[-1], ("/api/services/switch/toggle", {"entity_id": "switch.fan"}))
        self.assertEqual(MockHA.states["switch.fan"], "off")
        self.home.set_state("alarm", "on")
        self.assertEqual(MockHA.calls[-1][0], "/api/services/alarm_control_panel/alarm_arm_away")
        with self.assertRaises(home.ActionError):
            self.home.set_state("night", "on")  # press-only
        self.home.set_state("night", "press")
        self.assertEqual(MockHA.calls[-1][0], "/api/services/script/turn_on")
        with self.assertRaises(home.ActionError):
            self.home.set_state("nope", "on")

    def test_run_actions_and_phrases(self):
        say, done, confirm = self.house.run_actions([("fan", "off"), ("room", "off")], "es")
        self.assertEqual(say, "Abanico apagado. Recámara plug ya estaba apagado.")
        self.assertEqual([d["result"] for d in done], ["done", "unchanged"])
        self.assertIsNone(confirm)

    def test_confirmation_is_enforced_server_side(self):
        say, done, confirm = self.house.run_actions([("alarm", "on"), ("fan", "off")], "en", "conv-1234")
        self.assertEqual([d["id"] for d in done], ["fan"])
        self.assertNotIn("/api/services/alarm_control_panel/alarm_arm_away", [c[0] for c in MockHA.calls])
        self.assertTrue(confirm["token"])
        self.assertEqual(confirm["title"], "Arm Alarma Casa")
        self.assertIn("Arm Alarma Casa? Please confirm on the screen.", say)
        with self.assertRaises(home.ActionError):
            self.house.confirm(confirm["token"], "other-conv")  # bound to the conversation (and now used up)
        _, _, confirm = self.house.run_actions([("alarm", "on")], "en", "conv-1234")
        say, done = self.house.confirm(confirm["token"], "conv-1234")
        self.assertEqual(MockHA.calls[-1][0], "/api/services/alarm_control_panel/alarm_arm_away")
        self.assertEqual(done[0]["result"], "done")
        with self.assertRaises(home.ActionError) as cm:
            self.house.confirm(confirm["token"], "conv-1234")  # one time only
        self.assertEqual(cm.exception.code, 410)

    def test_confirmation_expires(self):
        _, _, confirm = self.house.run_actions([("media", "jellyfin")], "es")
        self.clock.t += homeai.CONFIRM_SECONDS + 1
        with self.assertRaises(home.ActionError):
            self.house.confirm(confirm["token"])
        self.assertFalse(any(c[:2] == ["sudo", "-n"] for c in self.system.calls))

    def test_speech_phrases_cover_the_replies(self):
        bank = set(self.house.speech_phrases())
        said = []
        for lang in ("es", "en"):
            for bid, want in (("fan", "off"), ("fan", "off"), ("fan", "on"), ("room", "on"), ("night", "press"),
                              ("media", "jellyfin"), ("alarm", "on")):
                b = self.home.button(bid)
                said.append(self.house._do(b, want, lang)[0])
                said.append(self.house.run_actions([(bid, want)], lang, confirm_all=True)[0])
            said.append(self.house.state_answer(["alarm", "fan", "media", "night"], lang))
            said.append(self.house.state_answer([], lang))
        for text in said:
            if text.startswith(("Prendido: ", "On: ")) and ", " in text:
                continue  # lists of several are left to Piper
            self.assertIsNotNone(tts.split_banked(text, bank.__contains__), text)
        self.assertEqual(len(bank), len(self.house.speech_phrases()))  # no duplicates

    def test_valid_action_filters(self):
        self.assertIsNone(self.house.valid_action("unknown", "on"))
        self.assertIsNone(self.house.valid_action("fan", "explode"))
        self.assertEqual(self.house.valid_action("night", "on"), ("night", "press"))
        self.assertEqual(self.house.valid_action("media", "plex"), ("media", "plex"))


class AssistantHouseTests(HomeFixture, AssistantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.start_ha()

    @classmethod
    def tearDownClass(cls):
        cls.stop_ha()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        home_dir = os.path.join(self.dir, "home")  # its own .env: the AI key lives in self.dir/.env
        os.mkdir(home_dir)
        self.home = self.make_home(home_dir)
        self.cams = FakeCameras()
        self.now = [1_800_000_000_000]
        self.a = am.Assistant(self.acfg(), TZ, self.dir, clock=lambda: self.now[0])
        self.a.house = homeai.House(self.home, self.cams)

    def test_local_match_uses_no_quota_and_no_ai(self):
        status, body = self.a.ask("apaga el abanico")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "house")
        self.assertEqual(body["answer"], "Abanico apagado.")
        self.assertEqual(MockGateway.calls, [])
        self.assertEqual(self.a.status()["remaining"], {"hour": 20, "day": 100})

    def test_model_json_actions_and_unknown_ids(self):
        MockGateway.answer = json.dumps({"say": "Ok", "actions": [{"button": "fan", "to": "off"},
                                                                  {"button": "garage", "to": "on"}], "look": ""})
        status, body = self.a.ask("it's too cold in here")
        self.assertEqual(status, 200)
        self.assertEqual(body["answer"], "Abanico is off.")
        self.assertEqual([d["id"] for d in body["actions_done"]], ["fan"])
        sent = MockGateway.calls[0]["body"]["messages"][0]["content"]
        self.assertTrue(sent.startswith(am.HOUSE_PROMPT))
        self.assertIn('- fan "Abanico"', sent)

    def test_model_cannot_skip_confirmation(self):
        MockGateway.answer = '```json\n{"say": "Armada", "actions": [{"button": "alarm", "to": "on"}]}\n```'
        _, body = self.a.ask("protege la casa", conversation="conv-abcdefgh")
        self.assertIn("confirm", body)
        self.assertNotIn("/api/services/alarm_control_panel/alarm_arm_away", [c[0] for c in MockHA.calls])
        status, body2 = self.a.confirm(body["confirm"]["token"], "conv-abcdefgh")
        self.assertEqual(status, 200)
        self.assertEqual(MockHA.calls[-1][0], "/api/services/alarm_control_panel/alarm_arm_away")
        status, _ = self.a.confirm(body["confirm"]["token"], "conv-abcdefgh")
        self.assertEqual(status, 410)

    def test_offer_is_held_for_confirmation(self):
        # The model asks and lists actions anyway: nothing runs until the person taps Yes.
        MockGateway.answer = json.dumps({"say": "¿Quieres que apague el abanico?",
                                         "actions": [{"button": "fan", "to": "off"}]})
        status, body = self.a.ask("hace frío aquí", conversation="conv-offer123")
        self.assertEqual(status, 200)
        self.assertEqual(body["answer"], "¿Quieres que apague el abanico?")
        self.assertEqual(body["actions_done"], [])
        self.assertEqual(body["confirm"]["title"], "Apagar Abanico")
        self.assertEqual(MockHA.calls, [])
        self.assertEqual(MockHA.states["switch.fan"], "on")
        status, body = self.a.confirm(body["confirm"]["token"], "conv-offer123")
        self.assertEqual(status, 200)
        self.assertEqual(MockHA.states["switch.fan"], "off")

    def test_prompt_tells_model_not_to_act_when_offering(self):
        self.assertIn("leave actions empty and wait for the answer", am.HOUSE_PROMPT)
        self.assertIn("when suggesting or asking, leave actions empty", am.LOCAL_HOUSE_PROMPT)

    def test_not_json_is_a_plain_answer(self):
        MockGateway.answer = "Paris is the capital of **France**."
        _, body = self.a.ask("capital of france?")
        self.assertEqual(body["answer"], "Paris is the capital of France.")
        self.assertNotIn("actions_done", body)
        MockGateway.answer = '{"say": "Cortado a la mit'
        _, body = self.a.ask("cuéntame algo")
        self.assertEqual(body["answer"], "Cortado a la mit")

    def test_memory_follow_up_and_expiry(self):
        MockGateway.answer = json.dumps({"say": "Abanico está prendido.", "actions": []})
        self.a.ask("how is the fan doing today", conversation="conv-memory1")
        MockGateway.answer = json.dumps({"say": "Listo", "actions": [{"button": "fan", "to": "off"}]})
        self.a.ask("then switch it off", conversation="conv-memory1")
        msgs = MockGateway.calls[1]["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "how is the fan doing today")
        self.now[0] += (self.a.cfg["memory_seconds"] + 1) * 1000
        self.a.ask("and again?", conversation="conv-memory1")
        self.assertEqual(len(MockGateway.calls[2]["body"]["messages"]), 2)
        self.a.ask("no conversation id")
        self.assertEqual(len(MockGateway.calls[3]["body"]["messages"]), 2)

    def test_look_sends_one_image_to_the_cloud(self):
        MockGateway.answer = "I see an empty street."
        status, body = self.a.ask("¿qué ves en la calle?", conversation="conv-look1234")
        self.assertEqual(status, 200)
        self.assertEqual(body["camera"], "street")
        self.assertEqual(self.cams.asked, [("street", 60)])
        content = MockGateway.calls[0]["body"]["messages"][-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "¿qué ves en la calle?"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/"))
        self.assertIn('"V380", taken just now', MockGateway.calls[0]["body"]["messages"][0]["content"])
        self.a.ask("¿y sigue ahí?", conversation="conv-look1234")  # same camera again
        self.assertEqual(self.cams.asked[-1][0], "street")

    def test_model_look_reply(self):
        MockGateway.answer = json.dumps({"say": "Let me look", "actions": [], "look": "sala"})
        self.a.ask("anything weird going on inside?")
        self.assertEqual(self.cams.asked, [("sala", 60)])
        self.assertEqual(len(MockGateway.calls), 2)
        self.assertIn("about 45 seconds ago", MockGateway.calls[1]["body"]["messages"][0]["content"])

    def test_model_look_is_not_blocked_by_the_question_gap(self):
        a = am.Assistant(self.acfg(min_interval_seconds=10), TZ, self.dir, clock=lambda: self.now[0])
        a.house = homeai.House(self.home, self.cams, weather=lambda: {
            "label": "Casa", "unit": "C", "data": {"current": {"temperature": 17, "condition": "Light rain", "humidity": 90}}})
        MockGateway.answer = json.dumps({"say": "Déjame ver", "actions": [], "look": "street"})
        status, body = a.ask("se ve feo afuera, todo bien?")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(MockGateway.calls), 2)
        self.assertIn("WEATHER at Casa: now 17°C, light rain", MockGateway.calls[0]["body"]["messages"][0]["content"])
        self.assertIn("WEATHER at Casa", MockGateway.calls[1]["body"]["messages"][0]["content"])
        self.assertEqual(a.status()["remaining"], {"hour": 18, "day": 98})
        status, body = a.ask("y ahora?")  # the gap still applies to the next question
        self.assertEqual(status, 429)

    def test_more_look_phrases(self):
        h = self.a.house
        self.assertEqual(h.match("¿puedes ver si dejé ropa colgada? con la cámara de la calle")["camera"], "street")
        self.assertEqual(h.match("can you check the street")["camera"], "street")
        self.assertEqual(h.match("checa la sala")["camera"], "sala")

    def test_look_offline_and_camera_failure(self):
        self.a.cfg["fallback_enabled"] = True
        self.a.gate.begin()
        self.a.gate.finish(False)  # the cloud just failed to respond: cooling down
        status, body = self.a.ask("is anyone in the sala?")
        self.assertEqual(body["source"], "offline")
        self.assertIn("internet", body["answer"])
        self.assertEqual(self.cams.asked, [])
        self.a.cfg["fallback_enabled"] = False
        self.cams.fail = True
        status, body = self.a.ask("is anyone in the sala?")
        self.assertEqual(status, 502)
        self.assertEqual(body["error"]["code"], "camera_unavailable")

    def test_home_control_off_is_the_old_assistant(self):
        self.a.cfg["home_control"] = False
        MockGateway.answer = "Four."
        _, body = self.a.ask("apaga el abanico")
        self.assertEqual(body["answer"], "Four.")
        self.assertEqual(MockGateway.calls[0]["body"]["messages"][0]["content"], am.SYSTEM_PROMPT)
        self.assertEqual(MockHA.calls, [])

    def test_request_parsing(self):
        cfg = self.a.cfg
        r = am.parse_request(b'{"question": " hi ", "conversation": "abc-12345", "speak": true}', cfg)
        self.assertEqual(r, {"question": "hi", "conversation": "abc-12345", "speak": True})
        for bad in (b'{"question": "hi", "x": 1}', b'{"question": "hi", "conversation": "A!"}',
                    b'{"question": "hi", "speak": "yes"}', b'{"conversation": "abc-12345"}'):
            with self.assertRaises(am.RequestError):
                am.parse_request(bad, cfg)
        self.assertEqual(am.parse_question(b'{"question": "hi"}', cfg), "hi")
        with self.assertRaises(am.RequestError):
            am.parse_confirm(b'{"token": "x"}')


class FakeHomeForCameras:
    env_path = "/nonexistent/.env"

    def __init__(self, clock):
        self.clock = clock
        self.services = []
        self.fail = False
        self.updated = "2026-01-01T00:00:00+00:00"

    def ha_service(self, service, entity_id):
        if self.fail and service == "blink.trigger_camera":
            raise home.ActionError("Home Assistant said HTTP 500")
        self.services.append((service, entity_id))

    def ha_rows(self, fresh=False):
        return {"camera.sala": {"state": "idle", "last_updated": self.updated}}

    def ha_image(self, entity_id):
        return b"img", "image/jpeg"


class CameraRefreshTests(unittest.TestCase):
    def make(self, **cam):
        cfg = json.loads(json.dumps(cameras.DEFAULT_CAMERAS_CONFIG))
        cfg["list"] = [dict({"id": "sala", "label": "Sala", "entity_id": "camera.sala", "ai_refresh_seconds": 60}, **cam),
                       {"id": "door", "label": "Door", "entity_id": "camera.door"}]
        cameras.validate_cameras_config(cfg)
        self.clock = FakeClock(2_000_000_000.0)
        self.fh = FakeHomeForCameras(self.clock)
        return cameras.Cameras(cfg, self.fh, clock=self.clock, sleep=lambda s: None)

    def triggers(self):
        return [e for s, e in self.fh.services if s == "blink.trigger_camera"]

    def test_only_configured_cameras_on_schedule(self):
        c = self.make()
        c.refresh_due()
        c.refresh_due()  # same moment: not due again
        self.assertEqual(self.triggers(), ["camera.sala"])
        self.clock.t += 61
        c.refresh_due()
        self.assertEqual(self.triggers(), ["camera.sala", "camera.sala"])
        c.cfg["ai_refresh_enabled"] = False
        self.clock.t += 61
        c.refresh_due()
        self.assertEqual(len(self.triggers()), 2)

    def test_backoff_on_errors(self):
        c = self.make()
        self.fh.fail = True
        c.refresh_due()
        self.assertEqual(c._refresh_next["sala"] - self.clock.t, 120)
        self.clock.t += 121
        c.refresh_due()
        self.assertEqual(c._refresh_next["sala"] - self.clock.t, 240)
        for _ in range(5):
            self.clock.t = c._refresh_next["sala"] + 1
            c.refresh_due()
        self.assertEqual(c._refresh_next["sala"] - self.clock.t, cameras.AI_REFRESH_MAX_BACKOFF)
        self.fh.fail = False
        self.clock.t = c._refresh_next["sala"] + 1
        c.refresh_due()
        self.assertEqual(c._refresh_next["sala"] - self.clock.t, 60)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.make(ai_refresh_seconds=10)
        with self.assertRaises(ValueError):
            self.make(snapshot=False)
        with self.assertRaises(ValueError):
            self.make(aliases="calle")

    def test_frame_for_ai_refreshes_old_snapshot(self):
        c = self.make()
        data, ctype, age = c.frame_for_ai("door", max_age=60)
        self.assertIn(("blink.trigger_camera", "camera.door"), self.fh.services)
        self.assertEqual(data, b"img")


FAKE_PIPER = """#!/bin/sh
# Fake Piper for tests: reads text on stdin, writes 0.1 s of silence as raw 16-bit audio.
cat > /dev/null
[ -n "$PIPER_FAIL" ] && exit 3
head -c 4410 /dev/zero
"""


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tts-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "piper")
        with open(self.bin, "w") as f:
            f.write(FAKE_PIPER)
        os.chmod(self.bin, stat.S_IRWXU)
        voices = {}
        for lang in ("es", "en"):
            path = os.path.join(self.tmp, "%s.onnx" % lang)
            open(path, "wb").close()
            with open(path + ".json", "w") as f:
                json.dump({"audio": {"sample_rate": 22050}}, f)
            voices[lang] = path
        self.cfg = json.loads(json.dumps(tts.DEFAULT_TTS_CONFIG))
        self.cfg.update(binary=self.bin, voices=voices, cache_items=2)
        tts.validate_tts_config(self.cfg)
        self.calls = []

        def runner(argv, **kw):
            self.calls.append((argv, kw["input"]))
            return subprocess.run(argv, **kw)
        self.s = tts.Speaker(self.cfg, runner=runner)

    def test_guess_lang(self):
        self.assertEqual(tts.guess_lang("Listo, apagué el abanico."), "es")
        self.assertEqual(tts.guess_lang("Todo está bien"), "es")
        self.assertEqual(tts.guess_lang("Done, the fan is off."), "en")
        self.assertEqual(tts.guess_lang("hola que tal"), "es")

    def test_wav_once_then_cached(self):
        sid = self.s.prepare("**Hola**, esto es una prueba. https://example.com")
        status, wav = self.s.wav(sid)
        self.assertEqual(status, 200)
        self.assertEqual(wav[:4], b"RIFF")
        argv, text = self.calls[0]
        self.assertIn(self.cfg["voices"]["es"], argv)
        self.assertEqual(text, "Hola, esto es una prueba.".encode())
        self.s.wav(sid)
        self.assertEqual(len(self.calls), 1)

    def test_cache_limit_and_bad_ids(self):
        ids = [self.s.prepare("one two three %d" % i) for i in range(3)]
        self.assertEqual(self.s.wav(ids[0]), (404, None))  # pushed out of the 2-item cache
        self.assertEqual(self.s.wav(ids[2])[0], 200)
        self.assertEqual(self.s.wav("../../etc/passwd"), (404, None))

    def test_failure_and_unavailable(self):
        os.environ["PIPER_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "PIPER_FAIL", None)
        self.assertEqual(self.s.wav(self.s.prepare("hello there"))[0], 502)
        self.cfg["enabled"] = False
        self.assertIsNone(self.s.prepare("hello"))
        self.cfg["enabled"] = True
        self.cfg["binary"] = "/nonexistent/piper"
        self.assertIsNone(self.s.prepare("hello"))
        self.assertIn("not installed", self.s.problem())

    def bank(self, phrases=("Listo, apagué Abanico.", "Done, Abanico is off.")):
        self.cfg["phrases_extra"] = []
        self.s.bank_dir = os.path.join(self.tmp, "bank")
        self.s.sleep = lambda _: None
        self.s.set_phrases(lambda: list(phrases))
        self.s.build_bank()
        return sorted(os.listdir(self.s.bank_dir))

    def test_bank_is_built_private_and_skips_piper(self):
        files = self.bank()
        self.assertEqual(len(files), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.s.bank_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.s.bank_dir, files[0])).st_mode), 0o600)
        self.assertEqual(self.s.status()["phrases"], {"ready": 2, "total": 2})
        self.s.build_bank()
        self.assertEqual(len(self.calls), 2)  # nothing new to render
        status, wav = self.s.wav(self.s.prepare("Listo, apagué Abanico."))
        self.assertEqual((status, wav[:4]), (200, b"RIFF"))
        self.assertEqual(len(self.calls), 2)  # served from the bank

    def test_bank_joins_banked_sentences(self):
        self.bank()
        status, wav = self.s.wav(self.s.prepare("Listo, apagué Abanico. Done, Abanico is off."))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 2)
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual(w.getnframes(), 2 * 2205)

    def test_answers_are_never_written_to_disk(self):
        files = self.bank()
        self.s.wav(self.s.prepare("Listo, apagué Abanico. La capital de Francia es París."))
        self.assertEqual(len(self.calls), 3)  # one sentence is not banked: Piper does the whole answer
        self.assertEqual(sorted(os.listdir(self.s.bank_dir)), files)

    def test_bank_follows_speed_and_prunes(self):
        old = self.bank()
        self.s.apply_settings({"length_scale_es": 1.0})
        self.assertTrue(self.s._wake.is_set())
        self.s._wake.clear()
        new = self.bank()
        self.assertEqual(len(new), 2)
        self.assertEqual(len(set(old) & set(new)), 1)  # the English clip is unchanged, the Spanish one re-rendered
        self.assertEqual(len(self.calls), 3)

    def test_bank_off_when_unavailable_or_disabled(self):
        self.cfg["phrase_bank"] = False
        self.s.bank_dir = os.path.join(self.tmp, "bank")
        self.s.build_bank()
        self.assertFalse(os.path.exists(self.s.bank_dir))
        self.assertEqual(self.s.status()["phrases"], {"ready": 0, "total": 0})
        with self.assertRaises(ValueError):
            tts.validate_tts_config(dict(self.cfg, phrases_extra=["ok", ""]))

    def test_assistant_adds_speech_id(self):
        a = am.Assistant.__new__(am.Assistant)
        a.speaker = self.s
        body = a._speech({"ok": True, "answer": "Done."}, True)
        self.assertIn("speech_id", body)
        self.assertNotIn("speech_id", a._speech({"ok": True, "answer": "Done."}, False))


if __name__ == "__main__":
    unittest.main()
