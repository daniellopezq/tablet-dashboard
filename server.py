#!/usr/bin/env python3
"""Tablet dashboard server.

Serves the static dashboard from web/, a server-time API, cached Open-Meteo
weather, cached Google Calendar events, and test media from media/. Python 3 standard library only.
See README.md for configuration and operation.

This file wires the services together (App) and runs the http/https listeners. Configuration is in
config.py, URL routing in routes/, response helpers in httpcore.py.
"""
import argparse
import logging
import os
import signal
import ssl
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import admin as admin_mod
import assistant as assistant_mod
import cameras as cameras_mod
import gcal as gcal_mod
import googletts
import health as health_mod
import home as home_mod
import homeai
import httpcore
import localai as localai_mod
import maps as maps_mod
import netsim
import pgstore
import routes
import tts as tts_mod
import voice as voice_mod
# Re-exported: tests and older callers use server.load_config, server.parse_range, etc.
from config import BASE_DIR, DEFAULT_CONFIG, ConfigError, load_config, validate_config  # noqa: F401
from httpcore import MEDIA_TYPES, STATIC_TYPES, parse_range, resolve_under  # noqa: F401
from weather import WeatherService

VERSION = "0.25.1"
USER_AGENT = "TabletDashboard/%s (personal home dashboard)" % VERSION

log = logging.getLogger("dashboard")


class App:
    def __init__(self, cfg, config_path=None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg["timezone"])
        self.web_root = os.path.realpath(os.path.join(BASE_DIR, cfg["web_dir"]))
        self.modern_root = os.path.realpath(os.path.join(BASE_DIR, cfg["modern_dir"]))
        self.media_root = os.path.realpath(os.path.join(BASE_DIR, cfg["media_dir"]))
        self.weather = WeatherService(cfg["weather"], USER_AGENT)
        self.db = pgstore.Database(cfg["database"])
        self.weather.on_update = lambda data: self.db.record_weather(cfg["weather"]["label"], data)
        self.assistant = assistant_mod.Assistant(cfg["assistant"], self.tz, BASE_DIR)
        self.voice = voice_mod.Transcriber(cfg["voice"], self.tz, BASE_DIR)
        w = cfg["weather"]
        self.maps = maps_mod.MapService(cfg["maps"], w["latitude"], w["longitude"], w["label"],
                                        BASE_DIR, USER_AGENT)
        self.calendar = gcal_mod.CalendarService(cfg["calendar"], self.tz, BASE_DIR, USER_AGENT)
        self.localai = localai_mod.LocalAI(cfg["local_ai"], BASE_DIR, self.assistant, self.voice)
        self.admin = admin_mod.AdminService(self, BASE_DIR, config_path or os.path.join(BASE_DIR, "config.json"),
                                            load_config, VERSION)
        self.health = health_mod.HealthChecker(cfg["health"], cfg["kuma"], BASE_DIR, self.admin.builtin_checks)
        self.home = home_mod.Home(cfg["home"], BASE_DIR)
        self.health.stopped_hint = self.home.intentionally_stopped
        self.cameras = cameras_mod.Cameras(cfg["cameras"], self.home)
        google_tts = googletts.GoogleTTS(cfg["tts"], os.path.join(BASE_DIR, cfg["tts"]["google_key_file"]),
                                         os.path.join(BASE_DIR, "data", "google-tts-usage.json"))
        self.speaker = tts_mod.Speaker(cfg["tts"], bank_dir=os.path.join(BASE_DIR, "data", "tts-phrases"),
                                       google=google_tts)
        self.assistant.speaker = self.speaker
        self.assistant.house = homeai.House(self.home, self.cameras, weather=self.weather.snapshot,
                                            history=self.db.weather_summary)
        self.speaker.set_phrases(self.assistant.house.speech_phrases)

    def check(self):
        problems = []
        if not os.path.isfile(os.path.join(self.web_root, "index.html")):
            problems.append("missing %s/index.html" % self.web_root)
        if self.cfg["ui"]["modern_enabled"] and not os.path.isfile(os.path.join(self.modern_root, "index.html")):
            problems.append("missing %s/index.html (or set ui.modern_enabled to false)" % self.modern_root)
        if not os.path.isdir(self.media_root):
            problems.append("missing media directory %s" % self.media_root)
        return problems

    def time_payload(self):
        now = time.time()
        local = datetime.fromtimestamp(now, self.tz)
        minutes = round(local.utcoffset().total_seconds() / 60)
        sign = "-" if minutes < 0 else "+"
        return {
            "now_ms": int(now * 1000),
            "timezone": self.cfg["timezone"],
            "utc_offset_minutes": minutes,
            "utc_offset": "%s%02d:%02d" % (sign, abs(minutes) // 60, abs(minutes) % 60),
            "abbreviation": local.tzname(),
            "local_iso": local.isoformat(timespec="seconds"),
        }

    def config_payload(self):
        return {
            "version": VERSION,
            "timezone": self.cfg["timezone"],
            "clock_default_24h": self.cfg["clock_default_24h"],
            "weather_configured": self.weather.configured,
            "temperature_unit": self.weather.unit,
            "maps_configured": self.maps.configured,
            "calendar_enabled": self.calendar.enabled,
            "home_enabled": self.cfg["home"]["enabled"],
            "cameras_enabled": self.cfg["cameras"]["enabled"] and bool(self.cfg["cameras"]["list"]),
            "https_port": self.cfg["https_port"],
            "offline_test": netsim.SIM.state()["services"] if netsim.SIM else [],
        }


class DashboardHandler(httpcore.ResponseMixin, BaseHTTPRequestHandler):
    """Routes live in routes/; response helpers in httpcore.ResponseMixin."""
    server_version = "TabletDashboard/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20  # idle socket timeout per connection

    def do_GET(self):
        routes.dispatch(self, "GET")

    def do_HEAD(self):
        routes.dispatch(self, "HEAD")

    def do_POST(self):
        routes.dispatch(self, "POST")

    def _method_not_allowed(self):
        self.method_not_allowed()

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, app, is_tls=False):
        self.app = app
        self.is_tls = is_tls
        super().__init__(address, DashboardHandler)

    def handle_error(self, request, client_address):
        # TLS handshakes from browsers that reject the self-signed certificate, and dropped
        # connections, are routine: log one line instead of a traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            log.info("%s connection ended: %s", client_address[0], type(exc).__name__)
            return
        super().handle_error(request, client_address)


def make_tls_context(cfg):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(os.path.join(BASE_DIR, cfg["tls_cert"]), os.path.join(BASE_DIR, cfg["tls_key"]))
    return ctx


def make_https_server(cfg, app, ctx):
    """Second listener for https (needed for in-page microphone access). The handshake runs
    lazily in each handler thread, so a slow or failing client never blocks accept()."""
    srv = DashboardServer((cfg["bind"], cfg["https_port"]), app, is_tls=True)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


def main(argv=None):
    parser = argparse.ArgumentParser(description="Tablet dashboard server")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "config.json"))
    parser.add_argument("--bind", help="override bind address")
    parser.add_argument("--port", type=int, help="override port")
    parser.add_argument("--pidfile", help="write PID here while running")
    parser.add_argument("--check", action="store_true", help="validate config and files, then exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        log.error("config: %s", exc)
        return 2
    if args.bind:
        cfg["bind"] = args.bind
    if args.port:
        cfg["port"] = args.port
    netsim.install(os.path.join(BASE_DIR, "data", "offline-test.json"))
    app = App(cfg, args.config)
    problems = app.check()
    if problems:
        for p in problems:
            log.error("check: %s", p)
        return 2
    if args.check:
        print("config OK: %s:%d tz=%s weather=%s" % (
            cfg["bind"], cfg["port"], cfg["timezone"],
            "configured" if app.weather.configured else "not configured"))
        return 0

    tls_ctx = None
    if cfg["https_port"]:
        try:
            tls_ctx = make_tls_context(cfg)
        except (OSError, ssl.SSLError) as exc:
            log.error("https: cannot load certificate/key (%s); run scripts/make-cert.sh", type(exc).__name__)
            return 2

    httpd = DashboardServer((cfg["bind"], cfg["port"]), app)
    httpsd = make_https_server(cfg, app, tls_ctx) if tls_ctx else None
    if args.pidfile:
        with open(args.pidfile, "w") as f:
            f.write("%d\n" % os.getpid())

    def _shutdown(*_):
        for srv in (httpd, httpsd):
            if srv:
                threading.Thread(target=srv.shutdown).start()

    signal.signal(signal.SIGTERM, _shutdown)
    app.weather.start()
    app.maps.start()
    app.cameras.start_ai_refresh()
    app.calendar.start()
    app.health.start()
    app.speaker.start_bank()
    if httpsd:
        threading.Thread(target=httpsd.serve_forever, name="https", daemon=True).start()
        log.info("dashboard https listening on %s:%d", cfg["bind"], cfg["https_port"])
    log.info("dashboard %s listening on %s:%d (pid %d)", VERSION, cfg["bind"], cfg["port"], os.getpid())
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.weather.stop()
        app.maps.stop()
        app.calendar.stop()
        app.health.stop()
        app.cameras.close()
        if httpsd:
            httpsd.shutdown()
            httpsd.server_close()
        httpd.server_close()
        if args.pidfile:
            try:
                os.remove(args.pidfile)
            except OSError:
                pass
        log.info("dashboard stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
