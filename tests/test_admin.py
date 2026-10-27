"""Tests for the admin panel (admin/ and its routes in routes/admin_api.py).

The admin listener is a plain-http DashboardServer flagged is_tls=True (the flag is what the
handler checks), so no certificates are needed. All files live in a temp directory; nothing leaves
this machine. Run: python3 -m unittest discover -s tests -v
"""
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import admin  # noqa: E402
import httpcore  # noqa: E402
import gcal  # noqa: E402
import gcal_auth  # noqa: E402
import netsim  # noqa: E402
import server  # noqa: E402
from test_server import DashboardTestCase, start_thread_server  # noqa: E402

PASSWORD = "correct horse battery"
CLIENT_ID = "123456789-abcdefg.apps.googleusercontent.com"


class AdminTestCase(DashboardTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="admin-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        with open(os.path.join(self.web, "admin.html"), "w") as f:
            f.write("<!DOCTYPE html><title>admin</title>")
        p = lambda name: os.path.join(self.dir, name)  # noqa: E731
        self.env = p(".env")
        self.config_path = p("config.json")
        with open(self.config_path, "w") as f:
            json.dump({"_comment": "test", "assistant": {"max_per_hour": 20}}, f)
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        cfg["web_dir"], cfg["media_dir"] = self.web, self.media
        cfg["weather"].update(latitude=None, longitude=None)
        for sec in ("assistant", "voice"):
            cfg[sec].update(env_file=self.env, state_file=p(sec + "-state.json"), usage_log=p(sec + "-usage.jsonl"))
        cfg["maps"]["env_file"] = self.env
        cfg["calendar"].update(env_file=self.env, token_file=p("gcal-token.json"))
        cfg["kuma"]["env_file"] = self.env
        cfg["health"]["enabled"] = False
        cfg["local_ai"]["dir"] = self.dir
        cfg["admin"]["password_file"] = p("admin.json")
        server.validate_config(cfg)
        self.sim = netsim.install(p("offline-test.json"))
        self.addCleanup(setattr, netsim, "SIM", None)
        self.app = server.App(cfg, self.config_path)
        self.app.admin.restart_hook = mock.Mock()
        # load_config for the copy under test: same checks, but test paths stay in place
        self.app.admin.load_config = lambda path: self._load(path, cfg)
        httpd = server.DashboardServer(("127.0.0.1", 0), self.app, is_tls=True)
        start_thread_server(httpd)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_port
        self.cookie = None

    @staticmethod
    def _load(path, base):
        merged = server.load_config(path)
        for sec in ("assistant", "voice", "maps", "calendar", "kuma", "admin", "local_ai"):
            for k, v in base[sec].items():
                if k.endswith("_file") or k in ("usage_log", "dir", "password_file"):
                    merged[sec][k] = v
        return merged

    def set_password(self):
        with open(self.app.admin.password_path, "w") as f:
            json.dump(admin.hash_password(PASSWORD), f)
        os.chmod(self.app.admin.password_path, 0o600)

    def call(self, method, route, body=None, headers=None, cookie=True, port=None):
        h = {"Host": "127.0.0.1:%d" % self.port}
        if method == "POST":
            h.update({"Content-Type": "application/json", "X-Admin": "1"})
        if cookie and self.cookie:
            h["Cookie"] = "%s=%s" % (admin.COOKIE, self.cookie)
        h.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
        conn.request(method, route if route.startswith("/") else "/api/admin/" + route, body=data, headers=h)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp, json.loads(raw)
        except ValueError:
            return resp, raw

    def login(self):
        self.set_password()
        resp, body = self.call("POST", "login", {"password": PASSWORD})
        self.assertEqual(resp.status, 200, body)
        cookie = resp.getheader("Set-Cookie")
        for attr in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)
        self.cookie = re.search(r"%s=([^;]+)" % admin.COOKIE, cookie).group(1)

    def write_env(self, text):
        with open(self.env, "w") as f:
            f.write(text)
        os.chmod(self.env, 0o600)


class AccessTests(AdminTestCase):
    def test_not_served_on_plain_http_listener(self):
        httpd = server.DashboardServer(("127.0.0.1", 0), self.app)  # is_tls False
        start_thread_server(httpd)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        for path in ("/admin", "/admin.html", "/admin.js", "/api/admin/session"):
            with self.subTest(path=path):
                resp, _ = self.call("GET", path, port=httpd.server_port)
                self.assertEqual(resp.status, 404)

    def test_served_on_https_listener(self):
        resp, body = self.call("GET", "/admin")
        self.assertEqual(resp.status, 200)
        self.assertIn(b"admin", body)
        self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy"))

    def test_not_served_to_non_lan_clients(self):
        with mock.patch.object(httpcore, "is_lan_client", return_value=False):
            for path in ("/admin", "/api/admin/session"):
                with self.subTest(path=path):
                    self.assertEqual(self.call("GET", path)[0].status, 404)

    def test_trusted_ips_do_not_open_admin(self):
        # A trusted remote device (assistant.trusted_ips) counts as home for the dashboard, never for admin.
        self.app.assistant.cfg["trusted_ips"] = ["100.101.102.103"]
        with mock.patch.object(httpcore, "is_lan_client", side_effect=lambda ip, trusted=(): bool(trusted)):
            self.assertEqual(self.call("GET", "/api/admin/session")[0].status, 404)

    def test_needs_session(self):
        for method, route in (("GET", "overview"), ("GET", "keys"), ("POST", "keys/set"), ("POST", "maint")):
            with self.subTest(route=route):
                self.assertEqual(self.call(method, route)[0].status, 401)

    def test_post_needs_csrf_header_and_same_origin(self):
        self.login()
        resp, _ = self.call("POST", "maint", {"action": "refresh"}, headers={"X-Admin": ""})
        self.assertEqual(resp.status, 403)
        resp, _ = self.call("POST", "maint", {"action": "refresh"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 403)
        resp, _ = self.call("POST", "maint", {"action": "refresh"}, headers={"Content-Type": "text/plain"})
        self.assertEqual(resp.status, 415)
        resp, _ = self.call("POST", "maint", {"action": "refresh"})
        self.assertEqual(resp.status, 200)

    def test_unknown_route(self):
        self.login()
        self.assertEqual(self.call("GET", "nothing_here")[0].status, 404)
        self.assertEqual(self.call("GET", "../keys")[0].status, 404)


class LoginTests(AdminTestCase):
    def test_no_password_set(self):
        resp, body = self.call("POST", "login", {"password": "x"})
        self.assertEqual(resp.status, 503)
        self.assertIn("set-admin-password", body["error"])
        self.assertFalse(self.call("GET", "session")[1]["password_set"])

    def test_wrong_password_and_lockout(self):
        self.set_password()
        for _ in range(admin.LOGIN_MAX_FAILURES):
            self.assertEqual(self.call("POST", "login", {"password": "nope"})[0].status, 401)
        resp, _ = self.call("POST", "login", {"password": PASSWORD})
        self.assertEqual(resp.status, 429)  # locked even with the right password

    def test_login_logout(self):
        self.login()
        self.assertTrue(self.call("GET", "session")[1]["logged_in"])
        self.assertEqual(self.call("GET", "overview")[0].status, 200)
        self.assertEqual(self.call("POST", "logout")[0].status, 200)
        self.assertEqual(self.call("GET", "overview")[0].status, 401)

    def test_idle_session_expires(self):
        self.login()
        for s in self.app.admin._sessions.values():
            s["seen"] -= self.app.cfg["admin"]["session_idle_hours"] * 3600 + 1
        self.assertEqual(self.call("GET", "overview")[0].status, 401)

    def test_hash_roundtrip(self):
        rec = admin.hash_password("pw-1234567890")
        self.assertTrue(admin.verify_password("pw-1234567890", rec))
        self.assertFalse(admin.verify_password("pw-1234567891", rec))
        self.assertNotIn("pw-1234567890", json.dumps(rec))
        self.assertFalse(admin.verify_password("x", {"salt": "zz"}))


class KeyTests(AdminTestCase):
    SECRET = "sk-test-SUPERSECRETVALUE-9876"

    def test_set_mask_remove(self):
        self.login()
        self.write_env("# keep me\nOTHER=1\n")
        with self.assertLogs("dashboard.admin", "INFO") as logs:
            resp, body = self.call("POST", "keys/set", {"name": "AI_GATEWAY_API_KEY", "value": " %s\n" % self.SECRET})
        self.assertEqual(resp.status, 200, body)
        self.assertNotIn(self.SECRET, "\n".join(logs.output))
        self.assertEqual(stat.S_IMODE(os.stat(self.env).st_mode), 0o600)
        with open(self.env) as f:
            text = f.read()
        self.assertIn("# keep me\nOTHER=1\n", text)
        self.assertIn("AI_GATEWAY_API_KEY=%s\n" % self.SECRET, text)
        resp, raw = self.call("GET", "keys")
        self.assertNotIn(self.SECRET, json.dumps(raw))
        k = {x["name"]: x for x in raw["keys"]}["AI_GATEWAY_API_KEY"]
        self.assertEqual((k["set"], k["preview"]), (True, "…9876"))
        self.assertEqual(self.app.assistant.status()["configured"], True)  # no restart needed
        self.call("POST", "keys/remove", {"name": "AI_GATEWAY_API_KEY"})
        with open(self.env) as f:
            self.assertNotIn("AI_GATEWAY", f.read())

    def test_keeps_users_alias_name(self):
        self.login()
        self.write_env("GROQ=gsk_old_value_123456\n")
        self.call("POST", "keys/set", {"name": "GROQ_API_KEY", "value": "gsk_new_value_654321"})
        with open(self.env) as f:
            self.assertEqual(f.read(), "GROQ=gsk_new_value_654321\n")

    def test_rejects_bad_values_and_names(self):
        self.login()
        for body in ({"name": "AI_GATEWAY_API_KEY", "value": "short"},
                     {"name": "GOOGLE_CLIENT_ID", "value": "not-a-client-id-123"},
                     {"name": "KUMA_PUSH_URL", "value": "javascript:alert(1)"},
                     {"name": "PATH", "value": "/tmp/evil-path-1234"}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "keys/set", body)[0].status, 400)
        self.assertFalse(os.path.exists(self.env))

    def test_pasted_newlines_cannot_add_lines(self):
        self.login()
        self.call("POST", "keys/set", {"name": "AI_GATEWAY_API_KEY", "value": "abcdefgh1234\nEVIL=1\n"})
        with open(self.env) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["AI_GATEWAY_API_KEY=abcdefgh1234EVIL=1"])

    def test_unsafe_env_file_is_refused(self):
        self.login()
        self.write_env("X=1\n")
        os.chmod(self.env, 0o644)
        self.assertEqual(self.call("POST", "keys/set", {"name": "AI_GATEWAY_API_KEY", "value": self.SECRET})[0].status, 409)


class SettingsTests(AdminTestCase):
    def saved(self):
        with open(self.config_path) as f:
            return json.load(f)

    def test_live_setting_saved_and_applied(self):
        self.login()
        resp, body = self.call("POST", "settings", {"section": "assistant", "values": {"max_per_hour": "7"}})
        self.assertEqual(resp.status, 200, body)
        self.assertFalse(body["restart_required"])
        self.assertEqual(self.app.assistant.cfg["max_per_hour"], 7)
        self.assertEqual(self.saved()["assistant"]["max_per_hour"], 7)
        self.assertEqual(self.saved()["_comment"], "test")  # other content kept
        self.assertTrue(os.listdir(os.path.join(self.dir, "backups")))

    def test_restart_setting_not_applied_live(self):
        self.login()
        resp, body = self.call("POST", "settings", {"section": "maps", "values": {"zoom": 9}})
        self.assertTrue(body["restart_required"])
        self.assertEqual(self.saved()["maps"]["zoom"], 9)
        self.assertNotEqual(self.app.maps.cfg["zoom"], 9)

    def test_invalid_setting_changes_nothing(self):
        self.login()
        before = self.saved()
        for body in ({"section": "assistant", "values": {"max_per_hour": 0}},
                     {"section": "assistant", "values": {"base_url": "http://evil.example"}},
                     {"section": "weather", "values": {"latitude": "200"}},
                     {"section": "general", "values": {"timezone": "Mars/Base"}},
                     {"section": "health", "values": {"targets": "[{\"name\":\"x\",\"type\":\"smtp\"}]"}},
                     {"section": "nope", "values": {}}):
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "settings", body)[0].status, 400)
        self.assertEqual(self.saved(), before)
        self.assertEqual(self.app.assistant.cfg["max_per_hour"], 20)

    def test_ai_save_live(self):
        self.login()
        resp, body = self.call("POST", "ai/save", {"service": "assistant", "values": {
            "model": "openai/gpt-test", "cloud_cooldown_seconds": 30, "fallback_enabled": False}})
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(self.app.assistant.cfg["model"], "openai/gpt-test")
        self.assertEqual(self.app.assistant.gate.cooldown, 30)
        self.assertEqual(self.saved()["assistant"]["model"], "openai/gpt-test")
        resp, _ = self.call("POST", "ai/save", {"service": "assistant", "values": {"base_url": "https://x"}})
        self.assertEqual(resp.status, 400)


class OfflineTests(AdminTestCase):
    def test_switch_and_tablet_flag(self):
        self.login()
        resp, body = self.call("POST", "offline", {"services": ["ask", "weather"], "mode": "instant", "minutes": 5})
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(self.call("GET", "/api/config")[1]["offline_test"], ["ask", "weather"])
        self.app.assistant.gate.finish(False)  # pretend a no-response started a cooldown
        self.assertTrue(self.app.assistant.gate.cooling_down())
        self.call("POST", "offline/off")
        self.assertEqual(self.call("GET", "/api/config")[1]["offline_test"], [])
        self.assertFalse(self.app.assistant.gate.cooling_down())

    def test_bad_request(self):
        self.login()
        resp, _ = self.call("POST", "offline", {"services": ["everything"], "minutes": 5})
        self.assertEqual(resp.status, 400)
        resp, _ = self.call("POST", "offline", {"services": ["ask"], "minutes": 500})
        self.assertEqual(resp.status, 400)

    def test_weather_fetch_fails_while_simulated(self):
        self.sim.turn_on(["weather"], "instant", 5)
        w = server.WeatherService(dict(server.DEFAULT_CONFIG["weather"], latitude=1.0, longitude=2.0,
                                       api_url="http://127.0.0.1:9/"), server.USER_AGENT)
        with self.assertLogs("dashboard", "WARNING"):
            self.assertFalse(w.fetch_once())
        self.assertEqual(w.snapshot()["error"], "network error")


class CalendarTests(AdminTestCase):
    def test_connect_by_pasting_redirect(self):
        self.login()
        self.write_env("GOOGLE_CLIENT_ID=%s\nGOOGLE_CLIENT_SECRET=GOCSPX-secret-123\n" % CLIENT_ID)
        resp, body = self.call("POST", "calendar/start", {"login_hint": "me@example.com"})
        self.assertEqual(resp.status, 200, body)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(body["pending"]["auth_url"]).query)
        self.assertEqual(q["login_hint"], ["me@example.com"])
        redirect = "%s/?state=%s&code=4/abc&scope=%s" % (q["redirect_uri"][0], q["state"][0],
                                                          urllib.parse.quote(gcal.SCOPE))
        bad = redirect.replace(q["state"][0], "wrong-state")
        self.assertEqual(self.call("POST", "calendar/finish", {"url": bad})[0].status, 400)
        with mock.patch.object(gcal_auth, "exchange_code", return_value="1//refresh-token-value") as ex, \
                mock.patch.object(self.app.calendar, "wake") as wake:
            resp, body = self.call("POST", "calendar/finish", {"url": redirect})
        self.assertEqual(resp.status, 200, body)
        self.assertEqual(ex.call_args[0][1], "4/abc")
        wake.assert_called_once()
        token, _, problem = gcal.load_refresh_token(self.app.calendar.token_path)
        self.assertEqual((token, problem), ("1//refresh-token-value", None))
        status = self.call("GET", "calendar")[1]
        self.assertIsNone(status["token_problem"])
        self.assertNotIn("refresh-token-value", json.dumps(status))

    def test_connect_needs_client(self):
        self.login()
        self.assertEqual(self.call("POST", "calendar/start", {})[0].status, 409)


class MaintenanceTests(AdminTestCase):
    def test_restart_uses_hook(self):
        self.login()
        resp, _ = self.call("POST", "maint", {"action": "restart"})
        self.assertEqual(resp.status, 200)
        for _ in range(50):
            if self.app.admin.restart_hook.called:
                break
            import time
            time.sleep(0.02)
        self.app.admin.restart_hook.assert_called_once()

    def test_logs_and_usage(self):
        self.login()
        self.assertEqual(self.call("GET", "logs?file=../../etc/passwd")[0].status, 400)
        self.assertEqual(self.call("GET", "logs?file=dashboard&lines=5")[0].status, 200)
        with open(self.app.assistant.store.usage_path, "w") as f:
            import time
            now = int(time.time() * 1000)
            f.write(json.dumps({"ts_ms": now, "source": "api", "outcome": "success", "reported_cost_usd": 0.001,
                                "total_tokens": 50}) + "\n")
            f.write(json.dumps({"ts_ms": now, "source": "local", "outcome": "success_local", "simulated": True}) + "\n")
            f.write("garbage\n")
        t = self.call("GET", "usage")[1]["assistant"]["totals"]["today"]
        self.assertEqual((t["count"], t["ok"], t["local"], t["simulated"], t["tokens"]), (2, 2, 1, 1, 50))
        self.assertAlmostEqual(t["cost_usd"], 0.001)


if __name__ == "__main__":
    unittest.main()
