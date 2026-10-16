#!/usr/bin/env python3
"""Tablet dashboard server.

Serves the static dashboard from web/, a server-time API, cached Open-Meteo
weather, cached Google Calendar events, and test media from media/. Python 3 standard library only.
See README.md for configuration and operation.
"""
import argparse
import email.utils
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import assistant as assistant_mod
import gcal as gcal_mod
import maps as maps_mod
import voice as voice_mod

VERSION = "0.6.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "TabletDashboard/%s (personal home dashboard)" % VERSION

DEFAULT_CONFIG = {
    "bind": "0.0.0.0",
    "port": 8099,
    "https_port": None,
    "tls_cert": "data/tls/server.crt",
    "tls_key": "data/tls/server.key",
    "timezone": "UTC",
    "clock_default_24h": True,
    "web_dir": "web",
    "media_dir": "media",
    "weather": {
        "latitude": None,
        "longitude": None,
        "label": "",
        "timezone": "UTC",
        "temperature_unit": "celsius",
        "refresh_minutes": 15,
        "stale_after_minutes": 35,
        "timeout_seconds": 10,
        "max_response_bytes": 262144,
        "api_url": OPEN_METEO_URL,
    },
    "assistant": dict(assistant_mod.DEFAULT_ASSISTANT_CONFIG),
    "maps": dict(maps_mod.DEFAULT_MAPS_CONFIG),
    "voice": dict(voice_mod.DEFAULT_VOICE_CONFIG),
    "calendar": dict(gcal_mod.DEFAULT_CALENDAR_CONFIG),
}

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".crt": "application/x-x509-ca-cert",  # public CA certificate for optional install on the tablet
}
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
}

CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'")

# WMO weather interpretation codes, per https://open-meteo.com/en/docs
WMO_CODES = {
    0: ("Clear sky", "clear"),
    1: ("Mainly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Rime fog", "fog"),
    51: ("Light drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Dense drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"),
    81: ("Rain showers", "rain"),
    82: ("Violent showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm, hail", "storm"),
    97: ("Heavy thunderstorm", "storm"),
    99: ("Thunderstorm, hail", "storm"),
}

log = logging.getLogger("dashboard")


class ConfigError(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_zone(name, key):
    if not isinstance(name, str) or not name:
        raise ConfigError("%s must be an IANA timezone name" % key)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("%s: unknown timezone %r" % (key, name))


def load_config(path):
    """Return the merged, validated configuration. Keys starting with '_' are comments."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                user = json.load(f)
            except json.JSONDecodeError as exc:
                raise ConfigError("%s: invalid JSON: %s" % (path, exc))
        if not isinstance(user, dict):
            raise ConfigError("config root must be a JSON object")
        for key, value in user.items():
            if key.startswith("_"):
                continue
            if key not in cfg:
                raise ConfigError("unknown config key: %s" % key)
            if key in ("weather", "assistant", "maps", "voice", "calendar"):
                if not isinstance(value, dict):
                    raise ConfigError("%s must be a JSON object" % key)
                for wkey, wvalue in value.items():
                    if wkey.startswith("_"):
                        continue
                    if wkey not in cfg[key]:
                        raise ConfigError("unknown config key: %s.%s" % (key, wkey))
                    cfg[key][wkey] = wvalue
            else:
                cfg[key] = value
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    if not isinstance(cfg["port"], int) or not 1 <= cfg["port"] <= 65535:
        raise ConfigError("port must be an integer 1-65535")
    hp = cfg["https_port"]
    if hp is not None and (not isinstance(hp, int) or isinstance(hp, bool) or not 1 <= hp <= 65535 or hp == cfg["port"]):
        raise ConfigError("https_port must be null or an integer 1-65535 different from port")
    for key in ("tls_cert", "tls_key"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise ConfigError("%s must be a path" % key)
    if not isinstance(cfg["bind"], str):
        raise ConfigError("bind must be a string")
    if not isinstance(cfg["clock_default_24h"], bool):
        raise ConfigError("clock_default_24h must be true or false")
    _load_zone(cfg["timezone"], "timezone")

    w = cfg["weather"]
    lat, lon = w["latitude"], w["longitude"]
    if (lat is None) != (lon is None):
        raise ConfigError("weather.latitude and weather.longitude must both be set or both be null")
    if lat is not None:
        if not _is_number(lat) or not -90 <= lat <= 90:
            raise ConfigError("weather.latitude must be a number between -90 and 90")
        if not _is_number(lon) or not -180 <= lon <= 180:
            raise ConfigError("weather.longitude must be a number between -180 and 180")
    if not isinstance(w["label"], str):
        raise ConfigError("weather.label must be a string")
    _load_zone(w["timezone"], "weather.timezone")
    if w["temperature_unit"] not in ("celsius", "fahrenheit"):
        raise ConfigError("weather.temperature_unit must be 'celsius' or 'fahrenheit'")
    if not _is_number(w["refresh_minutes"]) or w["refresh_minutes"] < 5:
        raise ConfigError("weather.refresh_minutes must be at least 5")
    if not _is_number(w["stale_after_minutes"]) or w["stale_after_minutes"] <= 0:
        raise ConfigError("weather.stale_after_minutes must be positive")
    if not _is_number(w["timeout_seconds"]) or not 0 < w["timeout_seconds"] <= 60:
        raise ConfigError("weather.timeout_seconds must be between 0 and 60")
    if not isinstance(w["max_response_bytes"], int) or not 1024 <= w["max_response_bytes"] <= 4194304:
        raise ConfigError("weather.max_response_bytes must be an integer 1024-4194304")
    if not isinstance(w["api_url"], str) or not re.match(r"^https?://", w["api_url"]):
        raise ConfigError("weather.api_url must be an http(s) URL")
    try:
        assistant_mod.validate_assistant_config(cfg["assistant"])
        maps_mod.validate_maps_config(cfg["maps"])
        voice_mod.validate_voice_config(cfg["voice"])
        gcal_mod.validate_calendar_config(cfg["calendar"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(str(exc))


class WeatherError(Exception):
    pass


class WeatherService:
    """Fetches Open-Meteo in a background thread; requests only ever read the cache."""

    def __init__(self, wcfg):
        self.cfg = wcfg
        self.configured = wcfg["latitude"] is not None
        self.unit = "F" if wcfg["temperature_unit"] == "fahrenheit" else "C"
        self._lock = threading.Lock()
        self._data = None
        self._updated_ms = None
        self._last_attempt_ms = None
        self._last_error = None
        self._failures = 0
        self._stop = threading.Event()
        self._thread = None

    def build_url(self):
        w = self.cfg
        params = {
            "latitude": "%.4f" % w["latitude"],
            "longitude": "%.4f" % w["longitude"],
            "current": "temperature_2m,relative_humidity_2m,weather_code,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": w["timezone"],
            "temperature_unit": w["temperature_unit"],
            "forecast_days": "4",
        }
        return w["api_url"] + "?" + urllib.parse.urlencode(params, safe=",/")

    def _download(self):
        limit = self.cfg["max_response_bytes"]
        timeout = self.cfg["timeout_seconds"]
        deadline = time.monotonic() + timeout * 2
        req = urllib.request.Request(self.build_url(), headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > limit:
                    raise WeatherError("response too large")
                chunks, total = [], 0
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise WeatherError("response too large")
                    if time.monotonic() > deadline:
                        raise WeatherError("timeout")
                    chunks.append(chunk)
        except urllib.error.HTTPError as exc:
            reason = ""
            try:
                body = json.loads(exc.read(4096).decode("utf-8", "replace"))
                reason = str(body.get("reason", ""))[:200]
            except Exception:
                pass
            log.warning("weather: HTTP %s %s", exc.code, reason)
            raise WeatherError("HTTP %d" % exc.code)
        except WeatherError:
            raise
        except (TimeoutError, OSError) as exc:
            # urllib.error.URLError is an OSError subclass; socket timeouts arrive here too.
            text = "timeout" if "timed out" in str(exc) else "network error"
            log.warning("weather: %s (%s)", text, exc)
            raise WeatherError(text)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WeatherError("invalid response")

    def normalize(self, raw):
        try:
            cur, daily = raw["current"], raw["daily"]
            temp, hum, code = cur["temperature_2m"], cur["relative_humidity_2m"], cur["weather_code"]
            if not _is_number(temp) or not isinstance(code, int):
                raise WeatherError("invalid response")
            dates = daily["time"]
            days = []
            for i in range(min(4, len(dates))):
                dcode = daily["weather_code"][i]
                hi, lo = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
                if not isinstance(dates[i], str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", dates[i]):
                    raise WeatherError("invalid response")
                label, icon = WMO_CODES.get(dcode, ("Unknown", "unknown"))
                days.append({
                    "date": dates[i],
                    "code": dcode if isinstance(dcode, int) else None,
                    "condition": label,
                    "icon": icon,
                    "max": hi if _is_number(hi) else None,
                    "min": lo if _is_number(lo) else None,
                })
            if not days:
                raise WeatherError("invalid response")
        except (KeyError, TypeError, IndexError):
            raise WeatherError("invalid response")
        label, icon = WMO_CODES.get(code, ("Unknown", "unknown"))
        is_day = bool(cur.get("is_day", 1))
        if icon == "clear" and not is_day:
            icon = "clear-night"
        elif icon == "partly" and not is_day:
            icon = "partly-night"
        return {
            "current": {
                "temperature": temp,
                "humidity": hum if _is_number(hum) else None,
                "code": code,
                "condition": label,
                "icon": icon,
                "is_day": is_day,
            },
            "today": days[0],
            "forecast": days[1:],
        }

    def fetch_once(self):
        """Fetch and cache weather. Returns True on success. Never raises."""
        if not self.configured:
            return False
        started = now_ms()
        try:
            data = self.normalize(self._download())
        except WeatherError as exc:
            with self._lock:
                self._last_attempt_ms = started
                self._last_error = str(exc)
                self._failures += 1
            return False
        except Exception:
            log.exception("weather: unexpected error")
            with self._lock:
                self._last_attempt_ms = started
                self._last_error = "internal error"
                self._failures += 1
            return False
        with self._lock:
            self._data = data
            self._updated_ms = now_ms()
            self._last_attempt_ms = started
            self._last_error = None
            self._failures = 0
        log.info("weather: updated")
        return True

    def next_delay(self):
        refresh = self.cfg["refresh_minutes"] * 60
        with self._lock:
            failures = self._failures
        if failures == 0:
            return refresh
        return min(refresh, 60 * (2 ** (failures - 1)))  # 1, 2, 4, 8 min, then refresh

    def snapshot(self):
        with self._lock:
            snap = {
                "configured": self.configured,
                "label": self.cfg["label"],
                "unit": self.unit,
                "updated_ms": self._updated_ms,
                "last_attempt_ms": self._last_attempt_ms,
                "error": self._last_error,
                "stale": False,
                "data": self._data,
                "attribution": {"text": "Weather data by Open-Meteo.com",
                                "url": "https://open-meteo.com/",
                                "license": "CC BY 4.0"},
            }
            if not self.configured:
                snap["status"] = "unconfigured"
            elif self._data is None:
                snap["status"] = "error" if self._last_error else "loading"
            else:
                snap["status"] = "ok"
                age_ms = now_ms() - self._updated_ms
                snap["stale"] = (self._last_error is not None
                                 or age_ms > self.cfg["stale_after_minutes"] * 60000)
        return snap

    def _run(self):
        while not self._stop.is_set():
            self.fetch_once()
            if self._stop.wait(self.next_delay()):
                break

    def start(self):
        if not self.configured:
            log.info("weather: location not configured; not fetching")
            return
        self._thread = threading.Thread(target=self._run, name="weather", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def resolve_under(root, rel):
    """Map a URL path (still percent-encoded) to a regular file inside root, or None.

    Rejects traversal, hidden segments, backslashes, NUL, directories and symlinks
    that resolve outside root.
    """
    path = urllib.parse.unquote(rel)
    if "\x00" in path or "\\" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or any(p.startswith(".") for p in parts):
        return None
    real = os.path.realpath(os.path.join(root, *parts))
    if not real.startswith(root + os.sep) or not os.path.isfile(real):
        return None
    return real


def parse_range(header, size):
    """Return (start, end) inclusive, None to serve the whole file, or 'unsatisfiable'."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None  # absent, malformed or multi-range: ignore and send 200
    if m.group(1) == "":
        suffix = int(m.group(2))
        if suffix == 0 or size == 0:
            return "unsatisfiable"
        return max(0, size - suffix), size - 1
    start = int(m.group(1))
    if start >= size:
        return "unsatisfiable"
    end = int(m.group(2)) if m.group(2) else size - 1
    if end < start:
        return None
    return start, min(end, size - 1)


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg["timezone"])
        self.web_root = os.path.realpath(os.path.join(BASE_DIR, cfg["web_dir"]))
        self.media_root = os.path.realpath(os.path.join(BASE_DIR, cfg["media_dir"]))
        self.weather = WeatherService(cfg["weather"])
        self.assistant = assistant_mod.Assistant(cfg["assistant"], self.tz, BASE_DIR)
        self.voice = voice_mod.Transcriber(cfg["voice"], self.tz, BASE_DIR)
        w = cfg["weather"]
        self.maps = maps_mod.MapService(cfg["maps"], w["latitude"], w["longitude"], w["label"],
                                        BASE_DIR, USER_AGENT)
        self.calendar = gcal_mod.CalendarService(cfg["calendar"], self.tz, BASE_DIR, USER_AGENT)

    def check(self):
        problems = []
        if not os.path.isfile(os.path.join(self.web_root, "index.html")):
            problems.append("missing %s/index.html" % self.web_root)
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
            "https_port": self.cfg["https_port"],
        }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TabletDashboard/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20  # idle socket timeout per connection

    @property
    def app(self):
        return self.server.app

    # --- routing -----------------------------------------------------------
    def do_GET(self):
        self._dispatch(head=False)

    def do_HEAD(self):
        self._dispatch(head=True)

    def _method_not_allowed(self):
        # The request body (if any) is never read, so the connection cannot be reused.
        self.close_connection = True
        self._send_bytes(405, b"Method not allowed\n", "text/plain; charset=utf-8",
                         head=False, extra={"Allow": "GET, HEAD"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _method_not_allowed

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/transcribe":
            return self._do_transcribe()
        if path != "/api/assistant":
            return self._method_not_allowed()
        # Always close after a POST: early rejections leave the body unread.
        self.close_connection = True
        a = self.app.assistant
        cfg = a.cfg
        if not assistant_mod.is_lan_client(self.client_address[0]):
            return self._assistant_error(403, "forbidden", "The assistant is available on the local network only.")
        problem = assistant_mod.check_origin(self.headers, cfg["allowed_hosts"])
        if problem:
            return self._assistant_error(403, "forbidden_origin", "Cross-origin requests are not allowed.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._assistant_error(415, "unsupported_media_type", "Send application/json.")
        if self.headers.get("Transfer-Encoding"):
            return self._assistant_error(411, "length_required", "Chunked bodies are not accepted.")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self._assistant_error(411, "length_required", "Content-Length is required.")
        length = int(length)
        if length == 0:
            return self._assistant_error(400, "bad_request", "Empty request.")
        if length > cfg["max_body_bytes"]:
            return self._assistant_error(413, "too_large", "Request is too large.")
        try:
            raw = self.rfile.read(length)
        except OSError:
            return
        if len(raw) != length:
            return self._assistant_error(400, "bad_request", "Incomplete request.")
        try:
            question = assistant_mod.parse_question(raw, cfg)
        except assistant_mod.RequestError as exc:
            return self._assistant_error(exc.status, exc.code, exc.message)
        status, body = a.ask(question)
        extra = {}
        retry = body.get("error", {}).get("retry_after") if not body.get("ok") else None
        if retry:
            extra["Retry-After"] = str(retry)
        self._send_json(body, False, status=status, extra=extra)

    AUDIO_TYPES = ("video/mp4", "video/3gpp", "video/webm", "video/ogg", "application/ogg",
                   "application/octet-stream")

    def _do_transcribe(self):
        """POST /api/transcribe: raw audio body (one recording) -> {"ok", "text", ...}."""
        self.close_connection = True  # early rejections leave the body unread
        v = self.app.voice
        if not assistant_mod.is_lan_client(self.client_address[0]):
            return self._assistant_error(403, "forbidden", "Voice input is available on the local network only.")
        if assistant_mod.check_origin(self.headers, self.app.assistant.cfg["allowed_hosts"]):
            return self._assistant_error(403, "forbidden_origin", "Cross-origin requests are not allowed.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith("audio/") or ctype in self.AUDIO_TYPES):
            return self._assistant_error(415, "unsupported_media_type", "Send an audio recording.")
        if self.headers.get("Transfer-Encoding"):
            return self._assistant_error(411, "length_required", "Chunked bodies are not accepted.")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self._assistant_error(411, "length_required", "Content-Length is required.")
        length = int(length)
        if length == 0:
            return self._assistant_error(400, "bad_request", "Empty recording.")
        if length > v.cfg["max_upload_bytes"]:
            return self._assistant_error(413, "too_large", "That recording is too large. Keep it under %d seconds."
                                         % v.cfg["max_audio_seconds"])
        try:
            raw = self.rfile.read(length)
        except OSError:
            return
        if len(raw) != length:
            return self._assistant_error(400, "bad_request", "Incomplete upload.")
        status, body = v.transcribe(raw)
        extra = {}
        retry = body.get("error", {}).get("retry_after") if not body.get("ok") else None
        if retry:
            extra["Retry-After"] = str(retry)
        self._send_json(body, False, status=status, extra=extra)

    def _assistant_error(self, status, code, message):
        self._send_json({"ok": False, "error": {"code": code, "message": message}}, False, status=status)

    def _dispatch(self, head):
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/"):
            return self._send_text(400, "Bad request", head)
        if path == "/api/time":
            return self._send_json(self.app.time_payload(), head)
        if path == "/api/weather":
            return self._send_json(self.app.weather.snapshot(), head)
        if path == "/api/maps":
            return self._send_json(self.app.maps.snapshot(), head)
        if path == "/api/calendar":
            # Personal data: local network only, like the assistant.
            if not assistant_mod.is_lan_client(self.client_address[0]):
                return self._send_json({"error": "forbidden"}, head, status=403)
            return self._send_json(self.app.calendar.snapshot(), head)
        if path == "/api/config":
            return self._send_json(self.app.config_payload(), head)
        if path == "/api/assistant/status":
            return self._send_json(self.app.assistant.status(), head)
        if path == "/api/voice/status":
            return self._send_json(self.app.voice.status(), head)
        if path.startswith("/api/"):
            return self._send_json({"error": "not found"}, head, status=404)
        if path.startswith("/maps/"):
            full = self.app.maps.path_for(path[len("/maps/"):])
            if not full:
                return self._send_text(404, "Not found", head)
            return self._send_file(full, "image/png", head, ranges=False)
        if path.startswith("/media/"):
            full = resolve_under(self.app.media_root, path[len("/media/"):])
            ctype = MEDIA_TYPES.get(os.path.splitext(full)[1].lower()) if full else None
            if not ctype:
                return self._send_text(404, "Not found", head)
            return self._send_file(full, ctype, head, ranges=True)
        rel = "index.html" if path == "/" else path[1:]
        full = resolve_under(self.app.web_root, rel)
        ctype = STATIC_TYPES.get(os.path.splitext(full)[1].lower()) if full else None
        if not ctype:
            return self._send_text(404, "Not found", head)
        return self._send_file(full, ctype, head, ranges=False)

    # --- responses ---------------------------------------------------------
    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_bytes(self, status, body, ctype, head, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_text(self, status, text, head):
        self._send_bytes(status, (text + "\n").encode("utf-8"), "text/plain; charset=utf-8",
                         head, {"Cache-Control": "no-store"})

    def _send_json(self, obj, head, status=200, extra=None):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        headers.update(extra or {})
        self._send_bytes(status, body, "application/json; charset=utf-8", head, headers)

    def _send_file(self, full, ctype, head, ranges):
        try:
            f = open(full, "rb")
        except OSError:
            return self._send_text(404, "Not found", head)
        with f:
            st = os.fstat(f.fileno())
            size = st.st_size
            span = parse_range(self.headers.get("Range"), size) if ranges else None
            if span == "unsatisfiable":
                return self._send_bytes(416, b"", "text/plain", head,
                                        {"Content-Range": "bytes */%d" % size, "Accept-Ranges": "bytes"})
            start, end = span if span else (0, size - 1)
            length = end - start + 1 if size else 0
            self.send_response(206 if span else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Last-Modified", email.utils.formatdate(st.st_mtime, usegmt=True))
            self._common_headers()
            if ranges:
                self.send_header("Accept-Ranges", "bytes")
                if span:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            else:
                self.send_header("Cache-Control", "no-cache")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            if head:
                return
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    # --- logging -----------------------------------------------------------
    def log_request(self, code="-", size="-"):
        # Routine successful API polls would dominate the log; record everything else.
        path = getattr(self, "path", "")
        if path.startswith("/api/") and isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, app):
        self.app = app
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
    srv = DashboardServer((cfg["bind"], cfg["https_port"]), app)
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
    app = App(cfg)
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
    app.calendar.start()
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
