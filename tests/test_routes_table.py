"""Characterization test for the HTTP routing (added for the 0.21.3 module split).

Sends one request per route/method on both listeners and compares status, headers and a
normalized body against tests/golden_routes.json, recorded from the code before the split.
The app runs from an empty temporary base directory, so results don't depend on this machine's keys.
Regenerate (only when a behaviour change is intended): python3 tests/test_routes_table.py --record
Run: python3 -m unittest discover -s tests -v
"""
import hashlib
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
from test_https import make_certs  # noqa: E402
from test_server import DashboardTestCase, start_thread_server  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_routes.json")

# Headers that legitimately change between runs or versions.
SKIP_HEADERS = {"date", "server", "last-modified", "etag", "set-cookie"}
# JSON keys whose values change between runs; only their presence is recorded.
VOLATILE = {"now_ms", "local_iso", "updated_ms", "fetched_ms", "age_ms", "started", "uptime_s", "next_ms",
            "checked_ms", "last_ms", "ts", "time", "generated_ms", "version"}

JSON = {"Content-Type": "application/json"}

# (listener, method, path, extra headers, body)
CASES = [
    ("http", "GET", "/", {}, None),
    ("http", "HEAD", "/", {}, None),
    ("http", "GET", "/index.html", {}, None),
    ("http", "GET", "/app.js", {}, None),
    ("http", "GET", "/notes.bin", {}, None),
    ("http", "GET", "/.hidden.js", {}, None),
    ("http", "GET", "/../server.py", {}, None),
    ("http", "GET", "/%2e%2e/outside.txt", {}, None),
    ("http", "GET", "/escape.txt", {}, None),
    ("http", "GET", "/escapedir/outside.txt", {}, None),
    ("http", "GET", "/icons/", {}, None),
    ("http", "GET", "/m/app.js", {}, None),
    ("http", "GET", "/m/.secret.js", {}, None),
    ("http", "GET", "/m/", {}, None),
    ("http", "GET", "/media/clip.mp4", {}, None),
    ("http", "GET", "/media/clip.mp4", {"Range": "bytes=10-19"}, None),
    ("http", "GET", "/media/clip.mp4", {"Range": "bytes=99999-"}, None),
    ("http", "HEAD", "/media/clip.mp4", {}, None),
    ("http", "GET", "/media/missing.mp4", {}, None),
    ("http", "GET", "/maps/x.png", {}, None),
    ("http", "GET", "/api/time", {}, None),
    ("http", "HEAD", "/api/time", {}, None),
    ("http", "GET", "/api/weather", {}, None),
    ("http", "GET", "/api/maps", {}, None),
    ("http", "GET", "/api/calendar", {}, None),
    ("http", "GET", "/api/uptime", {}, None),
    ("http", "GET", "/api/home", {}, None),
    ("http", "GET", "/api/cameras", {}, None),
    ("http", "GET", "/api/cameras/nope.jpg", {}, None),
    ("http", "GET", "/api/cameras/nope/controls", {}, None),
    ("http", "GET", "/api/cameras/nope/live.mjpg", {}, None),
    ("http", "GET", "/api/cameras/BAD!", {}, None),
    ("http", "GET", "/api/config", {}, None),
    ("http", "GET", "/api/assistant/status", {}, None),
    ("http", "GET", "/api/voice/status", {}, None),
    ("http", "GET", "/api/tts/AAAAAAAAAAAAAAAAAAAAAAAA.wav", {}, None),
    ("http", "GET", "/api/tts/bad.wav", {}, None),
    ("http", "GET", "/api/assistant", {}, None),
    ("http", "GET", "/api/unknown", {}, None),
    ("http", "GET", "/api/admin/session", {}, None),
    ("http", "GET", "/admin", {}, None),
    ("http", "GET", "/admin.js", {}, None),
    ("http", "PUT", "/", {}, None),
    ("http", "DELETE", "/api/time", {}, None),
    ("http", "OPTIONS", "/", {}, None),
    ("http", "POST", "/", {}, b""),
    ("http", "POST", "/api/unknown", JSON, b"{}"),
    ("http", "POST", "/api/assistant", {}, b"{}"),
    ("http", "POST", "/api/assistant", {"Content-Type": "text/plain"}, b"hi"),
    ("http", "POST", "/api/assistant", JSON, b""),
    ("http", "POST", "/api/assistant", JSON, b"x" * 70000),
    ("http", "POST", "/api/assistant", JSON, b"not json"),
    ("http", "POST", "/api/assistant", JSON, b'{"question": ""}'),
    ("http", "POST", "/api/assistant", dict(JSON, Origin="http://evil.example"), b'{"question": "hi"}'),
    ("http", "POST", "/api/assistant", JSON, b'{"question": "hi", "extra": 1}'),
    ("http", "POST", "/api/assistant/confirm", JSON, b'{"token": "AAAAAAAAAAAAAAAA"}'),
    ("http", "POST", "/api/assistant/confirm", JSON, b'{"token": 1}'),
    ("http", "POST", "/api/assistant/confirm", {"Content-Type": "text/plain"}, b"x"),
    ("http", "POST", "/api/transcribe", {}, b""),
    ("http", "POST", "/api/transcribe", {"Content-Type": "text/plain"}, b"abc"),
    ("http", "POST", "/api/transcribe", {"Content-Type": "application/octet-stream"}, b"not audio"),
    ("http", "POST", "/api/home/action", JSON, b"{}"),
    ("http", "POST", "/api/home/action", {"Content-Type": "text/plain"}, b"{}"),
    ("http", "POST", "/api/home/action", JSON, b"[1]"),
    ("http", "POST", "/api/cameras/snapshot", JSON, b'{"id": "nope"}'),
    ("http", "POST", "/api/cameras/control", JSON, b'{"id": "nope", "action": "left"}'),
    ("http", "POST", "/api/admin/login", JSON, b"{}"),
    ("https", "GET", "/", {}, None),
    ("https", "GET", "/api/time", {}, None),
    ("https", "GET", "/admin", {}, None),
    ("https", "GET", "/admin/", {}, None),
    ("https", "GET", "/admin.js", {}, None),
    ("https", "GET", "/api/admin/session", {}, None),
    ("https", "GET", "/api/admin/overview", {}, None),
    ("https", "GET", "/api/admin/nope", {}, None),
    ("https", "POST", "/api/admin/login", JSON, b"{}"),
    ("https", "POST", "/api/admin/login", dict(JSON, **{"X-Admin": "1"}), b"{}"),
    ("https", "POST", "/api/admin/logout", dict(JSON, **{"X-Admin": "1"}), b"{}"),
    ("https", "GET", "/../server.key", {}, None),
]


def normalize(obj):
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in VOLATILE else normalize(v)) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    if isinstance(obj, float):
        return "<float>"
    return obj


def describe(resp, body):
    headers = {k.lower(): v for k, v in resp.getheaders() if k.lower() not in SKIP_HEADERS}
    ctype = headers.get("content-type", "")
    out = {"status": resp.status}
    if ctype.startswith("application/json") and body:
        headers.pop("content-length", None)
        try:
            out["json"] = normalize(json.loads(body))
        except ValueError:
            out["body_sha256"] = hashlib.sha256(body).hexdigest()
    else:
        out["body_sha256"] = hashlib.sha256(body).hexdigest()
    out["headers"] = headers
    return out


class RoutesTable(DashboardTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(os.path.join(cls.web, "admin.html"), "w") as f:
            f.write("<!DOCTYPE html><title>admin</title>")
        with open(os.path.join(cls.web, "admin.js"), "w") as f:
            f.write("var admin = 1;")
        cls.certdir = tempfile.mkdtemp(prefix="routes-test-")
        make_certs(cls.certdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certdir, ignore_errors=True)
        super().tearDownClass()

    def start(self):
        # A throwaway base directory: no .env keys, no admin password, no data/ from this machine.
        base = tempfile.mkdtemp(prefix="routes-base-", dir=self.tmp)
        with mock.patch("server.BASE_DIR", base):
            app = self.make_app()
        # Pin what depends on this machine: no Piper installed, and Plex running with Jellyfin stopped.
        app.speaker.cfg["binary"] = os.path.join(base, "no-piper")
        app.home.runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="active\ninactive\n")
        app.cfg["bind"], app.cfg["https_port"] = "127.0.0.1", 0
        app.cfg["tls_cert"], app.cfg["tls_key"] = self.certdir + "/server.crt", self.certdir + "/server.key"
        http_port = self.serve(app)
        srv = server.make_https_server(app.cfg, app, server.make_tls_context(app.cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return http_port, srv.server_address[1]

    def run_cases(self):
        http_port, https_port = self.start()
        ctx = ssl.create_default_context(cafile=self.certdir + "/ca.crt")
        results = {}
        for listener, method, path, headers, body in CASES:
            if listener == "http":
                conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=10)
            else:
                conn = http.client.HTTPSConnection("127.0.0.1", https_port, timeout=10, context=ctx)
            conn.putrequest(method, path, skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            if body:
                conn.send(body)
            resp = conn.getresponse()
            data = b"" if method == "HEAD" else resp.read()
            conn.close()
            key = "%s %s %s %s %s" % (listener, method, path, json.dumps(headers, sort_keys=True),
                                      hashlib.sha256(body or b"").hexdigest()[:12])
            results[key] = describe(resp, data)
        return results

    def test_lan_only_routes_refuse_outside_clients(self):
        http_port, _ = self.start()
        get_paths = ["/api/calendar", "/api/uptime", "/api/home", "/api/cameras", "/api/cameras/x.jpg",
                     "/api/cameras/x/live.mjpg", "/api/tts/AAAAAAAAAAAAAAAAAAAAAAAA.wav"]
        posts = ["/api/assistant", "/api/assistant/confirm", "/api/transcribe", "/api/home/action", "/api/cameras/snapshot",
                 "/api/cameras/control"]
        with mock.patch("assistant.is_lan_client", return_value=False):
            for path in get_paths:
                with self.subTest(path=path):
                    resp, body = self.request(http_port, path)
                    self.assertEqual((resp.status, json.loads(body)), (403, {"error": "forbidden"}))
            for path in posts:
                with self.subTest(path=path):
                    conn = http.client.HTTPConnection("127.0.0.1", http_port, timeout=10)
                    conn.request("POST", path, body=b"{}", headers=JSON)
                    resp = conn.getresponse()
                    data = json.loads(resp.read())
                    conn.close()
                    self.assertEqual((resp.status, data["error"]["code"]), (403, "forbidden"))
            self.assertEqual(self.request(http_port, "/api/time")[0].status, 200)

    def test_routes_match_golden(self):
        with open(GOLDEN) as f:
            golden = json.load(f)
        results = self.run_cases()
        self.assertEqual(sorted(results), sorted(golden))
        for key in golden:
            with self.subTest(case=key):
                self.assertEqual(results[key], golden[key])


if __name__ == "__main__" and "--record" in sys.argv:
    t = RoutesTable("test_routes_match_golden")
    RoutesTable.setUpClass()
    try:
        t.setUp()
        res = t.run_cases()
        t.doCleanups()
    finally:
        RoutesTable.tearDownClass()
    with open(GOLDEN, "w") as f:
        json.dump(res, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    print("recorded %d cases to %s" % (len(res), GOLDEN))
elif __name__ == "__main__":
    unittest.main()
