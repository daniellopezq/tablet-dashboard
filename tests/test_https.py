"""Tests for the optional https listener (server.make_https_server).

Creates a throwaway CA + server certificate with openssl in a temp directory.
Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
from test_server import DashboardTestCase  # noqa: E402


def make_certs(d):
    run = lambda *args: subprocess.run(args, check=True, capture_output=True, timeout=60)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-keyout", d + "/ca.key",
        "-out", d + "/ca.crt", "-subj", "/CN=test CA", "-addext", "basicConstraints=critical,CA:TRUE")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", d + "/server.key", "-out", d + "/s.csr",
        "-subj", "/CN=127.0.0.1")
    with open(d + "/s.ext", "w") as f:
        f.write("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
    run("openssl", "x509", "-req", "-in", d + "/s.csr", "-CA", d + "/ca.crt", "-CAkey", d + "/ca.key",
        "-CAcreateserial", "-days", "2", "-extfile", d + "/s.ext", "-out", d + "/server.crt")


class HttpsTests(DashboardTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.certdir = tempfile.mkdtemp(prefix="https-test-")
        make_certs(cls.certdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certdir, ignore_errors=True)
        super().tearDownClass()

    def start_https(self):
        cfg = json.loads(json.dumps(server.DEFAULT_CONFIG))
        cfg["web_dir"], cfg["media_dir"] = self.web, self.media
        cfg["bind"], cfg["https_port"] = "127.0.0.1", 0
        cfg["tls_cert"], cfg["tls_key"] = self.certdir + "/server.crt", self.certdir + "/server.key"
        app = server.App(cfg)
        srv = server.make_https_server(cfg, app, server.make_tls_context(cfg))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def get(self, port, path, ctx):
        with urllib.request.urlopen("https://127.0.0.1:%d%s" % (port, path), context=ctx, timeout=10) as r:
            return r.status, r.read()

    def test_trusted_client_and_routes(self):
        port = self.start_https()
        ctx = ssl.create_default_context(cafile=self.certdir + "/ca.crt")
        status, body = self.get(port, "/api/time", ctx)
        self.assertEqual(status, 200)
        self.assertIn("now_ms", json.loads(body))
        self.assertEqual(self.get(port, "/", ctx)[0], 200)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(port, "/../server.key", ctx)
        self.assertEqual(cm.exception.code, 404)

    def test_untrusted_and_stalled_clients_do_not_break_server(self):
        port = self.start_https()
        with self.assertRaises(urllib.error.URLError):  # client rejects the self-signed chain
            self.get(port, "/api/time", ssl.create_default_context())
        stalled = socket.create_connection(("127.0.0.1", port), timeout=5)  # never sends a ClientHello
        self.addCleanup(stalled.close)
        plain = socket.create_connection(("127.0.0.1", port), timeout=5)  # plain http on the https port
        plain.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        plain.close()
        ctx = ssl.create_default_context(cafile=self.certdir + "/ca.crt")
        self.assertEqual(self.get(port, "/api/time", ctx)[0], 200)

    def test_config_validation(self):
        base = json.loads(json.dumps(server.DEFAULT_CONFIG))
        for bad in ({"https_port": 8099}, {"https_port": "8443"}, {"https_port": 70000}, {"tls_cert": ""}):
            with self.subTest(bad=bad):
                cfg = dict(base, **bad)
                with self.assertRaises(server.ConfigError):
                    server.validate_config(cfg)
        self.assertIsNone(base["https_port"])  # off unless configured


if __name__ == "__main__":
    unittest.main()
