"""Tests for gcal.py using a local mock of Google's token and Calendar endpoints.

No network access and no real credentials. Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gcal  # noqa: E402
import gcal_auth  # noqa: E402

TZ = ZoneInfo("America/Tijuana")
CLIENT_ID = "123456-abcdef.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-testsecret123"
REFRESH = "1//refresh-token-value"
ACCESS = "ya29.access-token-value"
# 2026-10-15 09:00 in Tijuana (PDT, UTC-7)
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=TZ).timestamp()


def ev(summary, start, end, **extra):
    def when(v):
        return {"date": v} if len(v) == 10 else {"dateTime": v}
    d = {"status": "confirmed", "summary": summary, "start": when(start), "end": when(end),
         "description": "SECRET-DESCRIPTION", "htmlLink": "https://calendar.google.com/x"}
    d.update(extra)
    return d


EVENTS = [
    ev("Dentist", "2026-10-15T10:00:00-07:00", "2026-10-15T11:00:00-07:00", location="123 Main St"),
    ev("Breakfast", "2026-10-15T08:00:00-07:00", "2026-10-15T08:30:00-07:00"),
    ev("Trip", "2026-10-16", "2026-10-19"),           # all-day Fri-Sun (end exclusive)
    ev("Cancelled thing", "2026-10-16T09:00:00-07:00", "2026-10-16T10:00:00-07:00", status="cancelled"),
    ev("Declined meeting", "2026-10-16T12:00:00-07:00", "2026-10-16T13:00:00-07:00",
       attendees=[{"email": "x@example.com", "self": True, "responseStatus": "declined"}]),
    ev("Office", "2026-10-17T09:00:00-07:00", "2026-10-17T17:00:00-07:00", eventType="workingLocation"),
    ev("Late show", "2026-10-17T23:00:00-07:00", "2026-10-18T01:00:00-07:00"),
    {"status": "confirmed", "start": {"dateTime": "2026-10-20T15:00:00Z"}, "end": {"dateTime": "2026-10-20T16:00:00Z"}},
]


class Google(BaseHTTPRequestHandler):
    token_mode = "ok"
    events_mode = "ok"
    token_calls = 0
    events_calls = []
    last_auth = []

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client stops reading oversized responses

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        Google.token_calls += 1
        if Google.token_mode == "invalid_grant":
            return self._send(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        if Google.token_mode == "invalid_client":
            return self._send(401, {"error": "invalid_client"})
        ok = (form.get("client_id") == [CLIENT_ID] and form.get("client_secret") == [CLIENT_SECRET]
              and form.get("refresh_token") == [REFRESH] and form.get("grant_type") == ["refresh_token"])
        if not ok:
            return self._send(400, {"error": "invalid_request"})
        self._send(200, {"access_token": ACCESS, "expires_in": 3599, "scope": gcal.SCOPE, "token_type": "Bearer"})

    def do_GET(self):
        Google.events_calls.append(self.path)
        Google.last_auth.append(self.headers.get("Authorization"))
        mode = Google.events_mode
        if mode == "401once":
            Google.events_mode = "ok"
            return self._send(401, {"error": {"code": 401, "status": "UNAUTHENTICATED"}})
        if mode == "disabled":
            return self._send(403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                                              "errors": [{"reason": "accessNotConfigured"}]}})
        if mode == "404":
            return self._send(404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
        if mode == "big":
            return self._send(200, {"items": [ev("x" * 500, "2026-10-15", "2026-10-16")] * 3000})
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "paged":
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if "pageToken" not in q:
                return self._send(200, {"items": EVENTS[:2], "nextPageToken": "p2"})
            return self._send(200, {"items": EVENTS[2:]})
        self._send(200, {"items": EVENTS})

    def log_message(self, *a):
        pass


class CalendarTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Google)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        cls._saved = (gcal.TOKEN_URL, gcal.EVENTS_URL)
        gcal.TOKEN_URL = base + "/token"
        gcal.EVENTS_URL = base + "/calendar/v3/calendars/%s/events"

    @classmethod
    def tearDownClass(cls):
        gcal.TOKEN_URL, gcal.EVENTS_URL = cls._saved
        cls.srv.shutdown()

    def setUp(self):
        Google.token_mode, Google.events_mode = "ok", "ok"
        Google.token_calls, Google.events_calls, Google.last_auth = 0, [], []
        for name in (gcal.CLIENT_ID_ENV, gcal.CLIENT_SECRET_ENV):
            os.environ.pop(name, None)
        self.dir = tempfile.mkdtemp(prefix="gcal-test-")
        self.cfg = dict(gcal.DEFAULT_CALENDAR_CONFIG)
        self.now = NOW
        self.write_env()
        self.write_token()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_env(self, cid=CLIENT_ID, secret=CLIENT_SECRET):
        gcal.set_env_values(os.path.join(self.dir, ".env"),
                            {gcal.CLIENT_ID_ENV: cid, gcal.CLIENT_SECRET_ENV: secret})

    def write_token(self, token=REFRESH):
        gcal.write_private(os.path.join(self.dir, "data", "gcal-token.json"),
                           json.dumps({"refresh_token": token}))

    def service(self):
        return gcal.CalendarService(self.cfg, TZ, self.dir, "test", clock=lambda: self.now)


class FetchTests(CalendarTestCase):
    def test_ok_payload(self):
        svc = self.service()
        self.assertTrue(svc.fetch_once())
        snap = svc.snapshot()
        self.assertEqual(snap["status"], "ok")
        self.assertFalse(snap["stale"])
        days = {d["date"]: [e["title"] for e in d["events"]] for d in snap["days"]}
        self.assertEqual(days["2026-10-15"], ["Breakfast", "Dentist"])  # sorted by start
        self.assertEqual(days["2026-10-16"], ["Trip"])                  # cancelled + declined hidden
        self.assertEqual(days["2026-10-17"], ["Trip", "Late show"])     # all-day first; workingLocation hidden
        self.assertEqual(days["2026-10-18"], ["Trip"])                  # end date is exclusive
        self.assertEqual(days["2026-10-20"], ["(No title)"])
        self.assertNotIn("2026-10-19", days)
        self.assertEqual(snap["count"], 5)

        dentist = snap["days"][0]["events"][1]
        self.assertEqual((dentist["start_hm"], dentist["end_hm"]), ("10:00", "11:00"))
        self.assertEqual(dentist["location"], "123 Main St")
        self.assertFalse(dentist["all_day"])
        late = snap["days"][2]["events"][1]
        self.assertTrue(late["multi_day"])
        utc = snap["days"][-1]["events"][0]
        self.assertEqual(utc["start_hm"], "08:00")  # 15:00Z shown in Tijuana time

    def test_request_shape(self):
        svc = self.service()
        svc.fetch_once()
        self.assertEqual(Google.last_auth, ["Bearer " + ACCESS])
        url = urllib.parse.urlsplit(Google.events_calls[0])
        self.assertEqual(url.path, "/calendar/v3/calendars/primary/events")
        q = urllib.parse.parse_qs(url.query)
        self.assertEqual(q["timeMin"], ["2026-10-15T00:00:00-07:00"])
        self.assertEqual(q["timeMax"], ["2026-10-22T00:00:00-07:00"])
        self.assertEqual(q["singleEvents"], ["true"])
        self.assertEqual(q["orderBy"], ["startTime"])
        self.assertIn("fields", q)

    def test_calendar_id_is_escaped(self):
        self.cfg["calendar_ids"] = ["en.mexican#holiday@group.v.calendar.google.com"]
        self.service().fetch_once()
        self.assertTrue(Google.events_calls[0].startswith(
            "/calendar/v3/calendars/en.mexican%23holiday%40group.v.calendar.google.com/events?"))

    def test_no_secrets_or_descriptions_in_snapshot(self):
        svc = self.service()
        svc.fetch_once()
        text = json.dumps(svc.snapshot())
        for secret in (CLIENT_ID, CLIENT_SECRET, REFRESH, ACCESS, "SECRET-DESCRIPTION", "htmlLink", "x@example.com"):
            self.assertNotIn(secret, text)

    def test_access_token_reused(self):
        svc = self.service()
        svc.fetch_once()
        svc.fetch_once()
        self.assertEqual(Google.token_calls, 1)
        self.now += 3600  # expired: refreshed again
        svc.fetch_once()
        self.assertEqual(Google.token_calls, 2)

    def test_401_refreshes_once(self):
        Google.events_mode = "401once"
        svc = self.service()
        self.assertTrue(svc.fetch_once())
        self.assertEqual(Google.token_calls, 2)

    def test_pagination(self):
        Google.events_mode = "paged"
        svc = self.service()
        self.assertTrue(svc.fetch_once())
        self.assertEqual(len(Google.events_calls), 2)
        self.assertEqual(svc.snapshot()["count"], 5)

    def test_max_events_truncates(self):
        self.cfg["max_events"] = 2
        svc = self.service()
        svc.fetch_once()
        snap = svc.snapshot()
        self.assertTrue(snap["truncated"])
        self.assertEqual(snap["count"], 2)

    def test_empty_today_still_listed(self):
        self.now = datetime(2026, 10, 19, 9, 0, tzinfo=TZ).timestamp()
        svc = self.service()
        svc.fetch_once()
        self.assertEqual(svc.snapshot()["days"][0], {"date": "2026-10-19", "events": []})

    def test_location_can_be_hidden(self):
        self.cfg["show_location"] = False
        svc = self.service()
        svc.fetch_once()
        self.assertNotIn("Reforma", json.dumps(svc.snapshot()))


class ProblemTests(CalendarTestCase):
    def assertProblem(self, problem, status):
        svc = self.service()
        self.assertFalse(svc.fetch_once())
        snap = svc.snapshot()
        self.assertEqual((snap["problem"], snap["status"]), (problem, status))
        self.assertTrue(snap["message"])
        return svc

    def test_no_client(self):
        os.remove(os.path.join(self.dir, ".env"))
        svc = self.assertProblem("not_configured", "setup")
        self.assertEqual(svc.next_delay(), 60)
        self.assertEqual(Google.token_calls, 0)

    def test_no_token(self):
        os.remove(os.path.join(self.dir, "data", "gcal-token.json"))
        self.assertProblem("not_configured", "setup")

    def test_bad_client_id(self):
        self.write_env(cid="not-a-client-id")
        self.assertProblem("invalid_key_format", "setup")

    def test_insecure_token_file(self):
        os.chmod(os.path.join(self.dir, "data", "gcal-token.json"), 0o644)
        self.assertProblem("insecure_file", "setup")

    def test_symlinked_token_rejected(self):
        path = os.path.join(self.dir, "data", "gcal-token.json")
        os.rename(path, path + ".real")
        os.symlink(path + ".real", path)
        self.assertProblem("insecure_file", "setup")

    def test_invalid_grant_then_recovers_after_new_token(self):
        Google.token_mode = "invalid_grant"
        svc = self.assertProblem("reauth", "setup")
        svc.fetch_once()
        self.assertEqual(Google.token_calls, 1)  # rejected token is not retried
        Google.token_mode = "ok"
        os.remove(os.path.join(self.dir, "data", "gcal-token.json"))
        self.write_token()
        os.utime(os.path.join(self.dir, "data", "gcal-token.json"), ns=(1, 1))  # distinct mtime
        self.assertTrue(svc.fetch_once())
        self.assertEqual(svc.snapshot()["status"], "ok")

    def test_invalid_client(self):
        Google.token_mode = "invalid_client"
        self.assertProblem("bad_client", "setup")

    def test_api_disabled(self):
        Google.events_mode = "disabled"
        self.assertProblem("api_disabled", "error")

    def test_not_found(self):
        Google.events_mode = "404"
        self.assertProblem("not_found", "error")

    def test_too_large(self):
        Google.events_mode = "big"
        self.assertProblem("too_large", "error")

    def test_redirect_not_followed(self):
        Google.events_mode = "redirect"
        self.assertProblem("upstream error", "error")

    def test_stale_keeps_last_data(self):
        svc = self.service()
        svc.fetch_once()
        Google.events_mode = "disabled"
        svc.fetch_once()
        snap = svc.snapshot()
        self.assertEqual(snap["status"], "ok")
        self.assertTrue(snap["stale"])
        self.assertEqual(snap["count"], 5)

    def test_disabled(self):
        self.cfg["enabled"] = False
        svc = self.service()
        self.assertFalse(svc.fetch_once())
        self.assertEqual(svc.snapshot()["status"], "disabled")


class RouteTests(unittest.TestCase):
    def test_api_calendar_route(self):
        import http.client
        import server
        cfg = server.load_config(None)
        app = server.App(cfg)
        httpd = server.DashboardServer(("127.0.0.1", 0), app)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("GET", "/api/calendar")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            self.assertEqual(resp.status, 200)
            self.assertIn("no-store", resp.getheader("Cache-Control"))
            self.assertEqual(data["status"], "loading")  # background thread not started in tests
            self.assertIsNone(data["days"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_api_calendar_lan_only(self):
        import httpcore
        import routes
        self.assertFalse(httpcore.is_lan_client("8.8.8.8"))  # the route reuses this check
        self.assertTrue(routes.GET["/api/calendar"][1])  # marked LAN only in the route table


class AuthHelperTests(unittest.TestCase):
    def test_auth_url(self):
        verifier, challenge = gcal_auth.pkce_pair()
        self.assertTrue(43 <= len(verifier) <= 128)
        url = gcal_auth.build_auth_url(CLIENT_ID, "http://127.0.0.1:5555", "st", challenge)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertEqual(q["access_type"], ["offline"])
        self.assertEqual(q["scope"], [gcal.SCOPE])
        self.assertEqual(q["redirect_uri"], ["http://127.0.0.1:5555"])

    def test_parse_redirect(self):
        scope = urllib.parse.quote(gcal.SCOPE)
        self.assertEqual(gcal_auth.parse_redirect("http://127.0.0.1:5555/?state=st&code=4/abc&scope=" + scope, "st"), "4/abc")
        self.assertEqual(gcal_auth.parse_redirect("/?state=st&code=4/abc", "st"), "4/abc")
        with self.assertRaises(ValueError):
            gcal_auth.parse_redirect("http://127.0.0.1:5555/?state=other&code=4/abc", "st")
        with self.assertRaises(ValueError):
            gcal_auth.parse_redirect("http://127.0.0.1:5555/?state=st&error=access_denied", "st")
        with self.assertRaises(ValueError):  # user unticked the calendar box
            gcal_auth.parse_redirect("http://127.0.0.1:5555/?state=st&code=4/abc&scope=openid", "st")

    def test_set_env_values_keeps_other_lines(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, ".env")
            gcal.write_private(path, "OTHER=1\nGOOGLE_CLIENT_ID=old\n")
            gcal.set_env_values(path, {"GOOGLE_CLIENT_ID": "new"})
            with open(path) as f:
                self.assertEqual(f.read(), "OTHER=1\nGOOGLE_CLIENT_ID=new\n")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        finally:
            shutil.rmtree(d)

    def test_config_validation(self):
        gcal.validate_calendar_config(dict(gcal.DEFAULT_CALENDAR_CONFIG))
        for bad in ({"calendar_ids": []}, {"days_ahead": 0}, {"days_ahead": 60}, {"refresh_minutes": 1},
                    {"enabled": "yes"}, {"calendar_ids": ["a", "a"]}):
            with self.assertRaises(ValueError):
                gcal.validate_calendar_config(dict(gcal.DEFAULT_CALENDAR_CONFIG, **bad))


if __name__ == "__main__":
    unittest.main()
