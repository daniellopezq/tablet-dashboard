"""Admin panel backend for the tablet dashboard (single admin, password login).

Served only on the https listener and only to LAN clients (server.py enforces both before calling
in). Every API call except login needs a session cookie (HttpOnly, Secure, SameSite=Strict); every
POST also needs the X-Admin: 1 header, a same-origin Origin/Host, and a JSON body.

Secrets are write-only here: the keys page shows whether a key is set and its last 4 characters,
never the value. Nothing secret is logged.

Command line:
  python3 admin.py set-password    set or change the admin password (scripts/set-admin-password.sh)

Python 3 standard library only.
"""
import collections
import email.utils
import getpass
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import assistant as assistant_mod
import envfile
import gcal as gcal_mod
import health as health_mod
import localai as localai_mod
import maps as maps_mod
import netsim
import voice as voice_mod

log = logging.getLogger("dashboard.admin")

DEFAULT_ADMIN_CONFIG = {
    "enabled": True,
    "password_file": "data/admin.json",
    "session_idle_hours": 8,
    "session_max_days": 7,
    "max_body_bytes": 65536,
}
COOKIE = "dash_admin"
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 5
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 10
LOG_FILES = {"dashboard": "logs/dashboard.log", "llama": "logs/llama-server.log", "whisper": "logs/whisper-server.log"}
BACKUP_EXCLUDE = {".env", "data", "backups", "logs", "media", "run", "__pycache__", ".git"}


def validate_admin_config(a):
    if not isinstance(a["enabled"], bool):
        raise ValueError("admin.enabled must be true or false")
    if not isinstance(a["password_file"], str) or not a["password_file"]:
        raise ValueError("admin.password_file must be a path string")
    for key, lo, hi in (("session_idle_hours", 0.1, 168), ("session_max_days", 0.1, 90),
                        ("max_body_bytes", 1024, 1048576)):
        v = a[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError("admin.%s must be a number %s-%s" % (key, lo, hi))


# --- password -------------------------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=64 * 1024 * 1024, **SCRYPT)
    return {"scheme": "scrypt", "n": SCRYPT["n"], "r": SCRYPT["r"], "p": SCRYPT["p"],
            "salt": salt.hex(), "hash": dk.hex(),
            "set_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def verify_password(password, rec):
    try:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(rec["salt"]), n=int(rec["n"]),
                            r=int(rec["r"]), p=int(rec["p"]), dklen=len(rec["hash"]) // 2,
                            maxmem=64 * 1024 * 1024)
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), rec["hash"])


def load_password(path):
    """The stored hash record, or None if no password is set (or the file is unsafe)."""
    try:
        raw, _ = envfile.read_private(path)
        rec = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.error("admin: %s is unreadable or not owner-only (mode 600); login disabled", path)
        return None
    return rec if isinstance(rec, dict) and rec.get("scheme") == "scrypt" else None


# --- keys -----------------------------------------------------------------------------------

KEYS = [
    {"name": "AI_GATEWAY_API_KEY", "label": "Vercel AI Gateway", "used_by": "Ask (cloud model)", "test": "gateway"},
    {"name": "GROQ_API_KEY", "aliases": ["GROQ"], "label": "Groq", "used_by": "Voice (cloud speech-to-text)",
     "test": "groq"},
    {"name": "OPENWEATHER_API_KEY", "label": "OpenWeatherMap", "used_by": "Maps (weather tiles)", "test": "owm",
     "regex": maps_mod.KEY_RE},
    {"name": "GOOGLE_CLIENT_ID", "label": "Google OAuth client ID", "used_by": "Calendar", "secret": False,
     "regex": gcal_mod.CLIENT_ID_RE},
    {"name": "GOOGLE_CLIENT_SECRET", "label": "Google OAuth client secret", "used_by": "Calendar", "test": "google"},
    {"name": "KUMA_API", "aliases": ["KUMA_API_KEY"], "label": "Uptime Kuma API key", "used_by": "Health (/metrics)",
     "test": "kuma"},
    {"name": "KUMA_PUSH_URL", "label": "Uptime Kuma push URL", "used_by": "Health (heartbeat)", "kind": "url",
     "test": "kuma_push"},
    {"name": "HA_TOKEN", "label": "Home Assistant long-lived token", "used_by": "Home tab", "test": "ha"},
]
KEYS_BY_NAME = {k["name"]: k for k in KEYS}


def _mask(value, secret=True):
    if not secret:
        return value
    return "…" + value[-4:] if len(value) >= 12 else "set"


# --- settings schema (what the Settings page may edit) --------------------------------------
# (key, type, label, restart_needed). Types: bool, int, num, numnull, str, strnull, enum:a|b, list, json

SETTINGS = collections.OrderedDict([
    ("general", ("General", [
        ("timezone", "str", "Time zone (IANA name)", True),
        ("clock_default_24h", "bool", "24-hour clock by default", True)])),
    ("weather", ("Weather", [
        ("label", "str", "Place name", True),
        ("latitude", "numnull", "Latitude", True),
        ("longitude", "numnull", "Longitude", True),
        ("timezone", "str", "Forecast time zone", True),
        ("temperature_unit", "enum:celsius|fahrenheit", "Temperature unit", True),
        ("refresh_minutes", "num", "Refresh every (minutes, ≥ 5)", True)])),
    ("assistant", ("Ask (assistant)", [
        ("enabled", "bool", "Enabled", False),
        ("max_output_tokens", "int", "Max answer tokens (cloud)", False),
        ("disable_thinking", "bool", "Turn model thinking off", False),
        ("min_interval_seconds", "num", "Seconds between questions", False),
        ("max_per_hour", "int", "Questions per hour", False),
        ("max_per_day", "int", "Questions per day", False),
        ("price_per_million_input_usd", "numnull", "Price per million input tokens (USD, for estimates)", False),
        ("price_per_million_output_usd", "numnull", "Price per million output tokens (USD)", False)])),
    ("voice", ("Voice", [
        ("enabled", "bool", "Enabled", False),
        ("auto_ask", "bool", "Ask automatically after transcribing", False),
        ("language", "strnull", "Language code (empty = auto)", False),
        ("min_interval_seconds", "num", "Seconds between recordings", False),
        ("max_per_hour", "int", "Recordings per hour", False),
        ("max_per_day", "int", "Recordings per day", False)])),
    ("maps", ("Maps", [
        ("enabled", "bool", "Enabled", True),
        ("zoom", "int", "Zoom (3-13)", True),
        ("rotate_seconds", "int", "Rotate every (seconds)", True),
        ("refresh_minutes", "num", "Refresh every (minutes, ≥ 10)", True),
        ("base_brightness", "num", "Base map brightness (0-1)", True),
        ("contrast", "num", "Contrast (0.5-3)", True),
        ("saturation", "num", "Saturation (0.5-3)", True),
        ("overlay_opacity", "num", "Overlay opacity (0-1)", True)])),
    ("calendar", ("Calendar", [
        ("enabled", "bool", "Enabled", True),
        ("calendar_ids", "list", "Calendar IDs (one per line)", False),
        ("days_ahead", "int", "Days ahead (1-31)", False),
        ("show_location", "bool", "Show locations", False),
        ("hide_declined", "bool", "Hide declined events", False)])),
    ("health", ("Health checks", [
        ("enabled", "bool", "Enabled", True),
        ("interval_seconds", "num", "Check every (seconds)", False),
        ("timeout_seconds", "num", "Timeout per check (seconds)", False),
        ("google_token_max_age_days", "numnull", "Google sign-in lifetime in days (7 while the app is in Testing; "
                                                 "empty once published)", False),
        ("disk_warn_gb", "num", "Warn below this much free disk (GB)", False),
        ("targets", "json", "Targets: [{\"name\",\"type\":\"tcp\",\"host\",\"port\"} or "
                            "{\"name\",\"type\":\"http\",\"url\"}]", False)])),
    ("kuma", ("Uptime Kuma", [
        ("enabled", "bool", "Enabled", False),
        ("base_url", "str", "Kuma URL, e.g. http://192.168.1.10:3001", False),
        ("status_page_slug", "str", "Status page slug (/status/<slug>)", False),
        ("use_metrics", "bool", "Read /metrics instead (needs KUMA_API)", False),
        ("push_enabled", "bool", "Send a heartbeat to KUMA_PUSH_URL", False)])),
])
AI_KEYS = {
    "assistant": ("model", "max_output_tokens", "fallback_enabled", "fallback_max_output_tokens",
                  "fallback_timeout_seconds", "no_response_seconds", "cloud_cooldown_seconds"),
    "voice": ("model", "fallback_enabled", "fallback_timeout_seconds", "no_response_seconds",
              "cloud_cooldown_seconds"),
}


def coerce(ftype, value):
    """Convert a form value to the config type. Raises ValueError."""
    if ftype == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if ftype in ("int", "num", "numnull"):
        if ftype == "numnull" and (value is None or value == ""):
            return None
        if isinstance(value, str):
            value = value.strip()
            try:
                value = int(value) if re.match(r"^-?\d+$", value) else float(value)
            except ValueError:
                raise ValueError("must be a number")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("must be a number")
        if ftype == "int":
            if value != int(value):
                raise ValueError("must be a whole number")
            value = int(value)
        return value
    if ftype in ("str", "strnull"):
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("must be text")
        value = value.strip()
        return None if ftype == "strnull" and not value else value
    if ftype.startswith("enum:"):
        if value not in ftype[5:].split("|"):
            raise ValueError("must be one of " + ftype[5:].replace("|", ", "))
        return value
    if ftype == "list":
        if isinstance(value, str):
            value = [v.strip() for v in value.splitlines() if v.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("must be a list")
        return value
    if ftype == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError("invalid JSON: %s" % exc)
        return value
    raise ValueError("unknown field type")


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


def _tail(path, max_lines, max_bytes=512 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably cut
    return lines[-max_lines:]


def _clean_err(exc):
    reason = getattr(exc, "reason", None) or exc
    return str(getattr(reason, "strerror", None) or getattr(exc, "category", None) or reason)[:160]


class AdminService:
    def __init__(self, app, base_dir, config_path, load_config, version):
        """app: server.App (weather, maps, calendar, assistant, voice, localai, cfg, tz)."""
        self.app = app
        self.cfg = app.cfg["admin"]
        self.base_dir = base_dir
        self.config_path = config_path
        self.load_config = load_config
        self.version = version
        self.started = time.time()
        self.password_path = os.path.join(base_dir, self.cfg["password_file"])
        self._lock = threading.Lock()
        self._config_lock = threading.Lock()
        self._sessions = {}
        self._failures = collections.defaultdict(list)
        self._job = None
        self._job_lock = threading.Lock()
        self._oauth = None
        self._model_cache = {}
        self._cert = (0, None)
        self.restart_hook = self._exec_restart
        if netsim.SIM is not None:
            netsim.SIM.on_change(self._offline_changed)

    # ======================================================================================
    # entry point (called by server.py after its https + LAN checks)
    # ======================================================================================
    def handle(self, method, path, query, headers, body, client_ip):
        """Return (status, json_obj, extra_headers). Never raises."""
        try:
            route = path[len("/api/admin/"):]
            if method == "POST":
                problem = assistant_mod.check_origin(headers, self.app.cfg["assistant"]["allowed_hosts"])
                if problem or headers.get("X-Admin") != "1":
                    raise ApiError(403, "Cross-origin or scripted requests are not allowed.")
                try:
                    data = json.loads(body.decode("utf-8")) if body else {}
                except (UnicodeDecodeError, ValueError):
                    raise ApiError(400, "Send a JSON object.")
                if not isinstance(data, dict):
                    raise ApiError(400, "Send a JSON object.")
            else:
                data = {k: v[-1] for k, v in urllib.parse.parse_qs(query).items()}
            if route == "session" and method == "GET":
                return 200, self._session_info(headers), {}
            if route == "login" and method == "POST":
                return self._login(data, client_ip)
            token = self._valid_session(headers)
            if not token:
                raise ApiError(401, "Please sign in.")
            if route == "logout" and method == "POST":
                with self._lock:
                    self._sessions.pop(token, None)
                return 200, {"ok": True}, {"Set-Cookie": self._cookie("", expire=True)}
            fn = getattr(self, "api_%s_%s" % (method.lower(), route.replace("/", "_").replace("-", "_")), None)
            if fn is None or not re.match(r"^[a-z0-9/_-]+$", route):
                raise ApiError(404, "Unknown admin call.")
            result = fn(data)
            return 200, result if isinstance(result, dict) else {"ok": True}, {}
        except ApiError as exc:
            return exc.status, dict({"ok": False, "error": exc.message}, **exc.extra), {}
        except Exception:
            log.exception("admin: internal error on %s %s", method, path)
            return 500, {"ok": False, "error": "Internal error (see the dashboard log)."}, {}

    # ======================================================================================
    # auth
    # ======================================================================================
    def _cookie(self, value, expire=False):
        c = http.cookies.SimpleCookie()
        c[COOKIE] = value
        m = c[COOKIE]
        m["path"] = "/"
        m["httponly"] = True
        m["secure"] = True
        m["samesite"] = "Strict"
        m["max-age"] = 0 if expire else int(self.cfg["session_max_days"] * 86400)
        return m.OutputString()

    def _token_from(self, headers):
        raw = headers.get("Cookie")
        if not raw:
            return None
        try:
            c = http.cookies.SimpleCookie(raw)
        except http.cookies.CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def _valid_session(self, headers):
        token = self._token_from(headers)
        if not token:
            return None
        now = time.time()
        with self._lock:
            for t in [t for t, s in self._sessions.items()
                      if now - s["seen"] > self.cfg["session_idle_hours"] * 3600
                      or now - s["created"] > self.cfg["session_max_days"] * 86400]:
                del self._sessions[t]
            for t, s in self._sessions.items():
                if hmac.compare_digest(t, token):
                    s["seen"] = now
                    return t
        return None

    def _session_info(self, headers):
        return {"ok": True, "logged_in": bool(self._valid_session(headers)),
                "password_set": load_password(self.password_path) is not None,
                "enabled": self.cfg["enabled"], "version": self.version}

    def _login(self, data, ip):
        now = time.time()
        with self._lock:
            fails = [t for t in self._failures[ip] if now - t < LOGIN_WINDOW]
            self._failures[ip] = fails
            if len(fails) >= LOGIN_MAX_FAILURES:
                wait = int(LOGIN_WINDOW - (now - fails[0])) + 1
                raise ApiError(429, "Too many failed sign-ins. Try again in %d minutes." % (wait // 60 + 1),
                               {"retry_after": wait})
        rec = load_password(self.password_path)
        if rec is None:
            raise ApiError(503, "No admin password is set. On the server run: scripts/set-admin-password.sh")
        pw = data.get("password")
        ok = isinstance(pw, str) and 0 < len(pw) <= 1024 and verify_password(pw, rec)
        if not ok:
            with self._lock:
                self._failures[ip].append(now)
            log.warning("admin: failed sign-in from %s", ip)
            raise ApiError(401, "Wrong password.")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._failures.pop(ip, None)
            self._sessions[token] = {"created": now, "seen": now, "ip": ip}
        log.info("admin: signed in from %s", ip)
        return 200, {"ok": True}, {"Set-Cookie": self._cookie(token)}

    # ======================================================================================
    # jobs: one long-running action at a time (tests, model restarts, backups)
    # ======================================================================================
    def _start_job(self, kind, fn):
        with self._job_lock:
            if self._job and self._job["state"] == "running":
                raise ApiError(409, "Another action (%s) is still running." % self._job["kind"])
            job = {"id": secrets.token_hex(6), "kind": kind, "state": "running",
                   "started_ms": int(time.time() * 1000), "finished_ms": None, "result": None}
            self._job = job

        def run():
            try:
                result = fn()
                state = "done"
            except Exception as exc:
                log.exception("admin: job %s failed", kind)
                result, state = {"ok": False, "error": _clean_err(exc)}, "error"
            with self._job_lock:
                job.update(state=state, result=result, finished_ms=int(time.time() * 1000))

        threading.Thread(target=run, name="admin-job", daemon=True).start()
        return {"ok": True, "job": dict(job)}

    def api_get_job(self, _data):
        with self._job_lock:
            return {"ok": True, "job": dict(self._job) if self._job else None}

    # ======================================================================================
    # overview
    # ======================================================================================
    def cert_expiry(self):
        """(unix seconds, text) of the https certificate's notAfter; cached for an hour."""
        if time.time() - self._cert[0] < 3600:
            return self._cert[1]
        path = os.path.join(self.base_dir, self.app.cfg["tls_cert"])
        value = None
        if os.path.isfile(path):
            try:
                r = subprocess.run(["openssl", "x509", "-enddate", "-noout", "-in", path],
                                   capture_output=True, text=True, timeout=10)
                text = r.stdout.strip().split("=", 1)[-1]
                value = (email.utils.mktime_tz(email.utils.parsedate_tz(
                    re.sub(r"^(\w+)\s+(\d+) ([\d:]+) (\d+) GMT$", r"\2 \1 \4 \3 +0000", text))), text)
            except (OSError, subprocess.SubprocessError, TypeError, ValueError):
                value = None
        self._cert = (time.time(), value)
        return value

    def api_get_overview(self, _data):
        a = self.app
        du = shutil.disk_usage(self.base_dir)
        mem = localai_mod.meminfo()
        try:
            load = os.getloadavg()
        except OSError:
            load = None
        cert = self.cert_expiry()
        snaps = {"weather": a.weather.snapshot(), "maps": a.maps.snapshot(), "calendar": a.calendar.snapshot()}
        return {
            "ok": True, "version": self.version, "pid": os.getpid(), "started_ms": int(self.started * 1000),
            "uptime_seconds": int(time.time() - self.started),
            "disk": {"free": du.free, "total": du.total}, "mem": mem, "load": load,
            "cert": {"expires": cert[0], "text": cert[1]} if cert else None,
            "ports": {"http": a.cfg["port"], "https": a.cfg["https_port"]},
            "offline_test": netsim.SIM.state() if netsim.SIM else {"active": False},
            "services": {
                "weather": {"status": snaps["weather"]["status"], "stale": snaps["weather"]["stale"],
                            "error": snaps["weather"]["error"], "updated_ms": snaps["weather"]["updated_ms"]},
                "maps": {"status": snaps["maps"]["status"], "error": snaps["maps"]["error"],
                         "layers": len(snaps["maps"]["layers"]), "calls": snaps["maps"]["calls_since_start"]},
                "calendar": {"status": snaps["calendar"]["status"], "stale": snaps["calendar"]["stale"],
                             "problem": snaps["calendar"]["problem"], "message": snaps["calendar"]["message"],
                             "updated_ms": snaps["calendar"]["updated_ms"]},
                "assistant": self._brief(a.assistant.status()),
                "voice": self._brief(a.voice.status()),
            },
            "health": a.health.snapshot()["counts"],
        }

    @staticmethod
    def _brief(st):
        return {"enabled": st["enabled"], "configured": st["configured"], "problem": st["setup_problem"],
                "model": st["model"], "remaining": st.get("remaining"), "fallback": st["fallback"]}

    # ======================================================================================
    # API keys
    # ======================================================================================
    @property
    def env_path(self):
        return os.path.join(self.base_dir, self.app.cfg["assistant"]["env_file"])

    def api_get_keys(self, _data):
        try:
            values = envfile.read_values(self.env_path)
            file_problem = None
        except OSError:
            values, file_problem = {}, "The .env file must be a regular file owned by this user with mode 600."
        out = []
        for k in KEYS:
            names = [k["name"]] + k.get("aliases", [])
            env_name = next((n for n in names if os.environ.get(n)), None)
            file_name = next((n for n in names if values.get(n)), None)
            value = os.environ.get(env_name) if env_name else values.get(file_name) if file_name else None
            out.append({"name": k["name"], "label": k["label"], "used_by": k["used_by"],
                        "secret": k.get("secret", True), "kind": k.get("kind", "key"),
                        "set": bool(value), "stored_as": env_name or file_name,
                        "in_environment": bool(env_name),
                        "preview": _mask(value, k.get("secret", True)) if value else None,
                        "testable": bool(k.get("test"))})
        return {"ok": True, "keys": out, "file_problem": file_problem, "env_file": self.env_path}

    def _key_entry(self, name):
        k = KEYS_BY_NAME.get(name)
        if not k:
            raise ApiError(400, "Unknown key name.")
        return k

    def api_post_keys_set(self, data):
        k = self._key_entry(data.get("name"))
        value = data.get("value")
        if not isinstance(value, str):
            raise ApiError(400, "Paste the value.")
        value = value.strip()
        if k.get("kind") == "url":
            ok = health_mod._http_url(value) and len(value) <= 1000 and not re.search(r"\s", value)
        else:
            value = re.sub(r"\s", "", value)
            ok = bool((k.get("regex") or assistant_mod.KEY_RE).match(value))
        if not ok:
            raise ApiError(400, "That doesn't look like a valid %s. Nothing was saved." % k["label"])
        names = [k["name"]] + k.get("aliases", [])
        try:
            current = envfile.read_values(self.env_path)
        except FileNotFoundError:
            current = {}
        except OSError:
            raise ApiError(409, "The .env file is not owner-only (mode 600); fix it on the server first.")
        target = next((n for n in names if current.get(n)), k["name"])  # keep the user's chosen name
        try:
            envfile.set_values(self.env_path, {target: value})
        except (OSError, ValueError):
            raise ApiError(500, "Could not write the .env file.")
        log.info("admin: key %s updated", target)
        self._after_key_change(k["name"])
        note = " It is also set in the server's environment, which wins until restart." if os.environ.get(target) else ""
        return {"ok": True, "message": "Saved as %s.%s" % (target, note)}

    def api_post_keys_remove(self, data):
        k = self._key_entry(data.get("name"))
        try:
            n = envfile.remove_values(self.env_path, [k["name"]] + k.get("aliases", []))
        except OSError:
            raise ApiError(409, "The .env file is not owner-only (mode 600); fix it on the server first.")
        log.info("admin: key %s removed", k["name"])
        self._after_key_change(k["name"])
        return {"ok": True, "message": "Removed." if n else "It was not in the file."}

    def _after_key_change(self, name):
        if name == "AI_GATEWAY_API_KEY":
            try:
                self.app.assistant.store.clear_block()  # as scripts/set-assistant-key.sh does
            except OSError:
                pass
        if name.startswith("GOOGLE_"):
            self.app.calendar.wake()
        if name == "OPENWEATHER_API_KEY":
            self.app.maps.wake()

    def api_post_keys_test(self, data):
        k = self._key_entry(data.get("name"))
        if not k.get("test"):
            raise ApiError(400, "This value has no test.")
        return self._start_job("test " + k["name"], lambda: self._test_key(k["test"]))

    def _test_key(self, kind):
        t0 = time.monotonic()
        ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731
        if kind == "gateway":
            return self._cloud_test("assistant")
        if kind == "groq":
            v = self.app.voice
            key, problem = assistant_mod.load_api_key(v.env_path, voice_mod.KEY_NAMES)
            if not key:
                return {"ok": False, "error": problem}
            ids = self._fetch_models(v.cfg["base_url"], key)
            return {"ok": True, "message": "Key accepted (%d models listed)" % len(ids), "ms": ms()}
        if kind == "owm":
            key, problem = maps_mod.load_api_key(self.app.maps.env_path)
            if not key:
                return {"ok": False, "error": problem}
            try:
                body = self.app.maps._get(maps_mod.OWM_TILE_URL % ("clouds_new", 3, 1, 3, key), "TabletDashboard-admin")
            except maps_mod.FetchError as exc:
                return {"ok": False, "error": str(exc), "ms": ms()}
            return {"ok": True, "message": "Key accepted (tile %d bytes)" % len(body), "ms": ms()}
        if kind == "google":
            c = self.app.calendar
            client, problem = gcal_mod.load_client(c.env_path)
            if problem:
                return {"ok": False, "error": problem}
            refresh, _, problem = gcal_mod.load_refresh_token(c.token_path)
            if problem:
                return {"ok": False, "error": "client is set; sign-in token: %s" % problem}
            try:
                c._access_token(client, refresh, force=True)
            except gcal_mod.CalendarError as exc:
                return {"ok": False, "error": gcal_mod.PROBLEMS.get(exc.code, exc.code), "ms": ms()}
            return {"ok": True, "message": "Google accepted the client and the sign-in token", "ms": ms()}
        if kind == "kuma":
            r = self.app.health.kuma.fetch()
            return {"ok": r["ok"], "error": r["error"], "ms": ms(),
                    "message": "%d monitors via %s" % (len(r["monitors"]), r["source"]) if r["ok"] else None}
        if kind == "ha":
            return dict(self.app.home.test_connection(), ms=ms())
        if kind == "kuma_push":
            err = self.app.health.kuma.push(True, "test from the dashboard admin page")
            return {"ok": not err, "error": err, "message": None if err else "Heartbeat sent", "ms": ms()}
        return {"ok": False, "error": "unknown test"}

    # ======================================================================================
    # Google Calendar sign-in
    # ======================================================================================
    def api_get_calendar(self, _data):
        c = self.app.calendar
        _, client_problem = gcal_mod.load_client(c.env_path)
        info = gcal_mod.token_info(c.token_path)
        snap = c.snapshot()
        max_age = self.app.cfg["health"]["google_token_max_age_days"]
        expires = None
        if info["created"] and max_age:
            try:
                expires = int((datetime.fromisoformat(info["created"]) + timedelta(days=max_age)).timestamp())
            except ValueError:
                pass
        with self._lock:
            pending = self._oauth_public()
        return {"ok": True, "client_problem": client_problem, "token_problem": info["problem"],
                "token_created": info["created"], "token_max_age_days": max_age, "token_expires": expires,
                "status": snap["status"], "problem": snap["problem"], "message": snap["message"],
                "updated_ms": snap["updated_ms"], "count": snap["count"], "calendar_ids": c.cfg["calendar_ids"],
                "pending": pending}

    def _oauth_public(self):
        o = self._oauth
        if not o:
            return None
        if time.time() > o["expires"] and o["result"] is None:
            o["result"] = "error"
            o["message"] = "Timed out. Start again."
            self._stop_oauth_server(o)
        return {"auth_url": o["auth_url"], "redirect_uri": o["redirect_uri"], "result": o["result"],
                "message": o["message"], "expires": int(o["expires"])}

    @staticmethod
    def _stop_oauth_server(o):
        srv, o["srv"] = o.get("srv"), None
        if srv:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()

    def api_post_calendar_start(self, data):
        c = self.app.calendar
        client, problem = gcal_mod.load_client(c.env_path)
        if problem:
            raise ApiError(409, "Save GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET on the API keys page first (%s)."
                           % problem)
        hint = data.get("login_hint") or ""
        if not isinstance(hint, str) or len(hint) > 200 or (hint and not re.match(r"^[^\s@]+@[^\s@]+$", hint)):
            raise ApiError(400, "The account hint must be an email address.")
        state = secrets.token_urlsafe(24)
        verifier, challenge = gcal_mod.pkce_pair()
        admin = self

        class Catch(BaseHTTPRequestHandler):
            def do_GET(self):
                msg = admin._finish_oauth(self.path, from_loopback=True)
                body = ("<!doctype html><meta charset=utf-8><title>Dashboard calendar</title>"
                        "<p style='font:18px sans-serif;margin:2em'>%s</p>" % msg).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Catch)
        redirect_uri = "http://127.0.0.1:%d" % srv.server_address[1]
        threading.Thread(target=srv.serve_forever, name="oauth-loopback", daemon=True).start()
        with self._lock:
            if self._oauth:
                self._stop_oauth_server(self._oauth)
            self._oauth = {"state": state, "verifier": verifier, "redirect_uri": redirect_uri, "srv": srv,
                           "client": client, "expires": time.time() + 600, "result": None, "message": None,
                           "auth_url": gcal_mod.build_auth_url(client[0], redirect_uri, state, challenge, hint)}
            return {"ok": True, "pending": self._oauth_public()}

    def _finish_oauth(self, text, from_loopback=False):
        """Complete sign-in from a redirect URL. Returns a message (also stored for the page)."""
        with self._lock:
            o = self._oauth
            if not o or o["result"] is not None or time.time() > o["expires"]:
                return "No sign-in is waiting. Start again from the admin page."
            o["result"] = "working"
        try:
            code = gcal_mod.parse_redirect(text, o["state"])
            refresh = gcal_mod.exchange_code(o["client"], code, o["verifier"], o["redirect_uri"])
            gcal_mod.write_private(self.app.calendar.token_path, json.dumps({
                "refresh_token": refresh, "scope": gcal_mod.SCOPE,
                "created": datetime.now().astimezone().isoformat(timespec="seconds")}) + "\n")
        except (ValueError, OSError) as exc:
            with self._lock:
                o["result"], o["message"] = None, str(exc)  # let the user try pasting again
            return str(exc)
        with self._lock:
            o["result"], o["message"] = "done", "Signed in. Events load within a few seconds."
            self._stop_oauth_server(o)
        self.app.calendar.wake()
        log.info("admin: Google Calendar signed in%s", " (loopback)" if from_loopback else "")
        return "Signed in. You can close this tab and return to the admin page."

    def api_post_calendar_finish(self, data):
        url = data.get("url")
        if not isinstance(url, str) or not url.strip() or len(url) > 4096:
            raise ApiError(400, "Paste the full address of the page that failed to load.")
        msg = self._finish_oauth(url)
        with self._lock:
            done = bool(self._oauth and self._oauth["result"] == "done")
        if not done:
            raise ApiError(400, msg)
        return {"ok": True, "message": msg}

    def api_post_calendar_cancel(self, _data):
        with self._lock:
            if self._oauth:
                self._stop_oauth_server(self._oauth)
            self._oauth = None
        return {"ok": True}

    def api_post_calendar_revoke(self, _data):
        msgs = gcal_mod.revoke_token(self.app.calendar.token_path)
        self.app.calendar.wake()
        log.info("admin: Google Calendar sign-in revoked")
        return {"ok": True, "message": " ".join(msgs)}

    # ======================================================================================
    # health
    # ======================================================================================
    def api_get_health(self, _data):
        return dict(self.app.health.snapshot(), ok=True)

    def api_post_health_run(self, _data):
        return self._start_job("health check", lambda: {"ok": True, "count": len(self.app.health.run_once())})

    def builtin_checks(self):
        """Dashboard checks for health.HealthChecker (runs in its thread)."""
        a, out = self.app, []

        def add(cid, name, state, detail, group="Dashboard"):
            out.append({"id": cid, "name": name, "group": group, "state": state, "detail": detail})

        bind = a.cfg["bind"] if a.cfg["bind"] not in ("", "0.0.0.0", "::") else "127.0.0.1"
        for label, port in (("http", a.cfg["port"]), ("https", a.cfg["https_port"])):
            if port:
                state, detail = health_mod.tcp_check(bind, port, 2)
                add("listen:" + label, "Dashboard %s :%d" % (label, port), state, detail)
        w = a.weather.snapshot()
        if w["configured"]:
            add("weather", "Weather (Open-Meteo)", "ok" if w["status"] == "ok" and not w["stale"] else
                "warn" if w["status"] in ("ok", "loading") else "fail",
                w["error"] or ("loading" if w["status"] == "loading" else _age(w["updated_ms"])))
        m = a.maps.snapshot()
        if m["configured"]:
            stale = any(l["stale"] for l in m["layers"])
            add("maps", "Weather maps", "warn" if m["status"] == "loading" else "fail" if m["status"] != "ok"
                else "warn" if stale or m["error"] else "ok",
                m["error"] or "%d layers, %s" % (len(m["layers"]), _age(max((l["updated_ms"] for l in m["layers"]),
                                                                            default=None))))
        c = a.calendar.snapshot()
        if c["enabled"]:
            add("calendar", "Google Calendar", "ok" if c["status"] == "ok" and not c["stale"] else
                "warn" if c["status"] in ("ok", "loading") else "fail",
                c["message"] or ("loading" if c["status"] == "loading" else _age(c["updated_ms"])))
            info = gcal_mod.token_info(a.calendar.token_path)
            max_age = a.cfg["health"]["google_token_max_age_days"]
            if info["created"] and max_age:
                try:
                    left = (datetime.fromisoformat(info["created"]) + timedelta(days=max_age)
                            - datetime.now().astimezone()).total_seconds() / 86400
                    add("gtoken", "Google sign-in lifetime", "fail" if left <= 0 else "warn" if left < 2 else "ok",
                        "expired, reconnect on the Calendar page" if left <= 0 else
                        "%.1f days left (app in Testing mode)" % left)
                except ValueError:
                    pass
        for name, svc in (("Ask", a.assistant), ("Voice", a.voice)):
            st = svc.status()
            if st["enabled"]:
                add("key:" + name, "%s API key" % name, "ok" if st["configured"] else "fail",
                    "set" if st["configured"] else st["setup_problem"])
        for kind, label in (("llm", "Local chat model"), ("stt", "Local speech model")):
            enabled = (a.assistant if kind == "llm" else a.voice).cfg["fallback_enabled"]
            if enabled:
                s = a.localai.server(kind)
                add("local:" + kind, label, "ok" if s["healthy"] else "fail",
                    "%s · %s" % (s["model"] or "not running", s["health"]))
        du = shutil.disk_usage(self.base_dir)
        warn = a.cfg["health"]["disk_warn_gb"] * 1e9
        add("disk", "Disk free (/)", "fail" if du.free < min(warn, 1e9) else "warn" if du.free < warn else "ok",
            "%.1f GB of %.0f GB" % (du.free / 1e9, du.total / 1e9))
        cert = self.cert_expiry()
        if cert:
            days = (cert[0] - time.time()) / 86400
            add("cert", "https certificate", "fail" if days <= 0 else "warn" if days < 30 else "ok",
                "expires %s (%d days)" % (cert[1], days))
        if netsim.SIM:
            st = netsim.SIM.state()
            if st["active"]:
                add("offline", "Offline test switch", "warn", "ON for %s, %d min left"
                    % (", ".join(st["services"]), st["remaining_seconds"] // 60 + 1))
        return out

    # ======================================================================================
    # settings (config.json)
    # ======================================================================================
    def _raw_config(self):
        try:
            with open(self.config_path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = {}
        if not isinstance(raw, dict):
            raise ApiError(500, "config.json is not a JSON object.")
        return raw

    def save_config(self, changes):
        """Write {section: {key: value}} ("general" = top level) to config.json after validating the
        whole result exactly as the server would load it. Backs up the old file."""
        with self._config_lock:
            raw = self._raw_config()
            for section, vals in changes.items():
                target = raw if section == "general" else raw.setdefault(section, {})
                target.update(vals)
            text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
            tmp = self.config_path + ".admin.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                merged = self.load_config(tmp)
            except Exception as exc:
                os.remove(tmp)
                raise ApiError(400, "Not saved: %s" % exc)
            if os.path.exists(self.config_path):
                bdir = os.path.join(os.path.dirname(os.path.abspath(self.config_path)), "backups")
                os.makedirs(bdir, exist_ok=True)
                shutil.copy2(self.config_path, os.path.join(
                    bdir, "config-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")))
                shutil.copymode(self.config_path, tmp)
            os.replace(tmp, self.config_path)
        log.info("admin: config.json updated (%s)", ", ".join(sorted(changes)))
        return merged

    def api_get_settings(self, _data):
        cfg = self.app.cfg
        sections = []
        for sid, (title, fields) in SETTINGS.items():
            src = cfg if sid == "general" else cfg[sid]
            sections.append({"id": sid, "title": title, "fields": [
                {"key": k, "type": t, "label": label, "restart": r, "value": src[k]} for k, t, label, r in fields]})
        return {"ok": True, "sections": sections}

    def api_post_settings(self, data):
        sid, values = data.get("section"), data.get("values")
        if sid not in SETTINGS or not isinstance(values, dict):
            raise ApiError(400, "Unknown settings section.")
        fields = {k: (t, r) for k, t, _, r in SETTINGS[sid][1]}
        clean, restart = {}, False
        src = self.app.cfg if sid == "general" else self.app.cfg[sid]
        for key, value in values.items():
            if key not in fields:
                raise ApiError(400, "%s cannot be edited here." % key)
            try:
                clean[key] = coerce(fields[key][0], value)
            except ValueError as exc:
                raise ApiError(400, "%s %s" % (key, exc))
        changed = {k: v for k, v in clean.items() if src.get(k) != v}
        if not changed:
            return {"ok": True, "message": "No changes.", "restart_required": False}
        merged = self.save_config({sid: changed})
        live = {k: merged[sid][k] for k in changed if not fields[k][1]} if sid != "general" else {}
        restart = any(fields[k][1] for k in changed)
        self._apply_live(sid, live)
        return {"ok": True, "restart_required": restart,
                "message": "Saved. Restart the dashboard to apply." if restart else "Saved and applied."}

    def _apply_live(self, sid, live):
        if not live:
            return
        a = self.app
        if sid == "assistant":
            a.assistant.apply_settings(live)
        elif sid == "voice":
            a.voice.apply_settings(live)
        else:
            a.cfg[sid].update(live)
        if sid == "calendar":
            a.calendar.wake()
        if sid in ("health", "kuma"):
            a.health.wake()

    # ======================================================================================
    # AI models (cloud + local)
    # ======================================================================================
    def _last_entry(self, store):
        lines = _tail(store.usage_path, 30, 64 * 1024)
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and not e.get("admin_test"):
                return {k: e.get(k) for k in ("ts_ms", "source", "model", "outcome", "duration_ms", "cloud",
                                              "simulated")}
        return None

    def api_get_ai(self, _data):
        a = self.app
        out = {"ok": True, "local": a.localai.status(),
               "offline_test": netsim.SIM.state() if netsim.SIM else {"active": False}}
        for sid, svc, keynames in (("assistant", a.assistant, (assistant_mod.KEY_ENV,)),
                                   ("voice", a.voice, voice_mod.KEY_NAMES)):
            key, problem = assistant_mod.load_api_key(svc.env_path, keynames)
            c = svc.cfg
            out[sid] = {"settings": {k: c[k] for k in AI_KEYS[sid]}, "base_url": c["base_url"],
                        "configured": key is not None, "problem": problem,
                        "fallback_model": c["fallback_model"], "fallback_base_url": c["fallback_base_url"],
                        "cooling_down": svc.gate.cooling_down(), "last": self._last_entry(svc.store),
                        "cloud_models": self._model_cache.get(sid, (0, None))[1]}
        return out

    def _fetch_models(self, base_url, key):
        req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={
            "Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "TabletDashboard-admin"})
        opener = urllib.request.build_opener(assistant_mod._NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                raw = resp.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ApiError(502, "The provider answered HTTP %d%s." % (
                exc.code, " (key rejected)" if exc.code == 401 else ""))
        except OSError as exc:
            raise ApiError(502, "Could not reach the provider (%s)." % _clean_err(exc))
        try:
            items = json.loads(raw.decode("utf-8"))["data"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise ApiError(502, "Unexpected model list from the provider.")
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]

    def api_post_ai_models(self, data):
        sid = data.get("service")
        if sid not in AI_KEYS:
            raise ApiError(400, "Unknown service.")
        cached = self._model_cache.get(sid)
        if cached and time.time() - cached[0] < 3600 and not data.get("force"):
            return {"ok": True, "models": cached[1]}
        svc = self.app.assistant if sid == "assistant" else self.app.voice
        key, problem = assistant_mod.load_api_key(
            svc.env_path, (assistant_mod.KEY_ENV,) if sid == "assistant" else voice_mod.KEY_NAMES)
        if not key:
            raise ApiError(409, "Set the API key first (%s)." % problem)
        items = self._fetch_models(svc.cfg["base_url"], key)
        if sid == "assistant":
            items = [i for i in items if i.get("type") in (None, "language")]
            models = sorted({i["id"] for i in items if "/" in i["id"]})
        else:
            models = sorted({i["id"] for i in items if "whisper" in i["id"].lower()})
        self._model_cache[sid] = (time.time(), models)
        return {"ok": True, "models": models}

    def api_post_ai_save(self, data):
        sid, values = data.get("service"), data.get("values")
        if sid not in AI_KEYS or not isinstance(values, dict):
            raise ApiError(400, "Unknown service.")
        types = {"model": "str", "fallback_enabled": "bool", "max_output_tokens": "int",
                 "fallback_max_output_tokens": "int", "fallback_timeout_seconds": "num",
                 "no_response_seconds": "num", "cloud_cooldown_seconds": "num"}
        clean = {}
        for k, v in values.items():
            if k not in AI_KEYS[sid]:
                raise ApiError(400, "%s cannot be edited here." % k)
            try:
                clean[k] = coerce(types[k], v)
            except ValueError as exc:
                raise ApiError(400, "%s %s" % (k, exc))
        svc = self.app.assistant if sid == "assistant" else self.app.voice
        changed = {k: v for k, v in clean.items() if svc.cfg.get(k) != v}
        if not changed:
            return {"ok": True, "message": "No changes."}
        merged = self.save_config({sid: changed})
        svc.apply_settings({k: merged[sid][k] for k in changed})
        log.info("admin: %s settings changed live (%s)", sid, ", ".join(sorted(changed)))
        return {"ok": True, "message": "Saved and applied (no restart needed)."}

    def _cloud_test(self, sid):
        """One real cloud call, outside the usage limits (but recorded in the usage log)."""
        svc = self.app.assistant if sid == "assistant" else self.app.voice
        names = (assistant_mod.KEY_ENV,) if sid == "assistant" else voice_mod.KEY_NAMES
        key, problem = assistant_mod.load_api_key(svc.env_path, names)
        if not key:
            return {"ok": False, "error": "API key: %s" % problem}
        t0 = time.monotonic()
        entry = {"ts_ms": int(time.time() * 1000), "model": svc.cfg["model"], "source": "api", "admin_test": True}
        try:
            if sid == "assistant":
                result = svc._call_upstream(key, "Reply with just the word OK.")
                text = result["answer"]
                entry.update(svc._usage_fields(result["usage"]))
            else:
                wav = self.app.localai.sample_wav()
                if wav is None:
                    return {"ok": False, "error": "sample audio %s not found" % localai_mod.SAMPLE_WAV}
                text = svc._call_upstream(key, wav, svc.cfg["timeout_seconds"])["text"]
        except Exception as exc:  # NoResponse, UpstreamError
            entry.update(outcome=getattr(exc, "category", None) or type(exc).__name__,
                         http_status=getattr(exc, "http_status", None))
            svc._log_outcome(entry)
            msg = ("no response (simulated offline?)" if type(exc).__name__ == "NoResponse"
                   else "%s%s" % (entry["outcome"], " HTTP %s" % entry["http_status"] if entry["http_status"] else ""))
            return {"ok": False, "error": msg, "ms": int((time.monotonic() - t0) * 1000)}
        entry.update(outcome="success", http_status=200, duration_ms=int((time.monotonic() - t0) * 1000))
        svc._log_outcome(entry)
        return {"ok": True, "text": text[:400], "ms": entry["duration_ms"], "model": svc.cfg["model"],
                "cost_usd": entry.get("reported_cost_usd") or entry.get("estimated_cost_usd"),
                "tokens": entry.get("total_tokens")}

    def api_post_ai_test(self, data):
        sid, target = data.get("service"), data.get("target")
        if sid not in AI_KEYS or target not in ("cloud", "local"):
            raise ApiError(400, "Unknown test.")
        la = self.app.localai
        if target == "cloud":
            fn = lambda: self._cloud_test(sid)  # noqa: E731
        elif sid == "assistant":
            q = data.get("question") or "What is the capital of France? Answer in one sentence."
            if not isinstance(q, str) or len(q) > 500:
                raise ApiError(400, "Question too long.")
            fn = lambda: la.test_llm(q)  # noqa: E731
        else:
            fn = la.test_stt
        return self._start_job("%s %s test" % (target, sid), fn)

    def api_post_ai_local_save(self, data):
        la = self.app.localai
        keys = ("llm_model", "llm_backend", "stt_model", "stt_backend", "threads")
        values = {}
        for k in keys:
            if k in data:
                try:
                    values[k] = coerce("int" if k == "threads" else "str", data[k])
                except ValueError as exc:
                    raise ApiError(400, "%s %s" % (k, exc))
        problem = la.check_choice(values)
        if problem:
            raise ApiError(400, problem)
        changes = {"local_ai": values}
        if "llm_model" in values:
            changes["assistant"] = {"fallback_model": os.path.splitext(values["llm_model"])[0]}
        if "stt_model" in values:
            changes["voice"] = {"fallback_model": os.path.splitext(values["stt_model"])[0]}
        merged = self.save_config(changes)
        la.cfg.update(merged["local_ai"])
        for sid in ("assistant", "voice"):
            if sid in changes:
                (self.app.assistant if sid == "assistant" else self.app.voice).apply_settings(changes[sid])
        if data.get("restart"):
            return self._start_job("restart local models", lambda: self._local_action("restart"))
        return {"ok": True, "message": "Saved. Restart the local models to load them. " + localai_mod.SWAP_NOTE}

    def _local_action(self, action):
        ok, output = self.app.localai.run(action)
        return {"ok": ok, "output": output, "servers": {k: self.app.localai.server(k) for k in ("llm", "stt")}}

    def api_post_ai_local_action(self, data):
        action = data.get("action")
        if action not in ("start", "stop", "restart"):
            raise ApiError(400, "Unknown action.")
        return self._start_job("%s local models" % action, lambda: self._local_action(action))

    # ======================================================================================
    # offline test switch
    # ======================================================================================
    def _offline_changed(self, active):
        if not active:
            # Back online: use the cloud again right away instead of waiting out a cooldown.
            self.app.assistant.gate.reset()
            self.app.voice.gate.reset()

    def _sim(self):
        if netsim.SIM is None:
            raise ApiError(503, "The offline test switch is not available in this process.")
        return netsim.SIM

    def api_get_offline(self, _data):
        a = self.app
        return {"ok": True, "state": self._sim().state(), "services": list(netsim.SERVICES),
                "modes": list(netsim.MODES), "max_minutes": netsim.MAX_MINUTES,
                "gates": {"ask": a.assistant.gate.cooling_down(), "voice": a.voice.gate.cooling_down()},
                "fallback_enabled": {"ask": a.assistant.cfg["fallback_enabled"],
                                     "voice": a.voice.cfg["fallback_enabled"]},
                "no_response_seconds": {"ask": a.assistant.cfg["no_response_seconds"],
                                        "voice": a.voice.cfg["no_response_seconds"]}}

    def api_post_offline(self, data):
        try:
            st = self._sim().turn_on(data.get("services"), data.get("mode", "realistic"),
                                     coerce("num", data.get("minutes", 15)))
        except ValueError as exc:
            raise ApiError(400, str(exc))
        self.app.health.wake()
        return {"ok": True, "state": st}

    def api_post_offline_off(self, _data):
        st = self._sim().turn_off()
        self.app.assistant.gate.reset()
        self.app.voice.gate.reset()
        self.app.health.wake()
        return {"ok": True, "state": st}

    def api_post_offline_fallback_test(self, data):
        which = data.get("services") or ["ask", "voice"]
        if not isinstance(which, list) or not set(which) <= {"ask", "voice"}:
            raise ApiError(400, "services must be ask and/or voice")
        return self._start_job("fallback test", lambda: self._fallback_test(which))

    def _fallback_test(self, which):
        """Real requests through the normal pipelines (they count toward the usage limits)."""
        out = {"ok": True, "offline_test": netsim.SIM.state() if netsim.SIM else None}
        if "ask" in which:
            t0 = time.monotonic()
            status, body = self.app.assistant.ask("Fallback test: what is 2 + 2? Answer in a few words.")
            out["ask"] = {"status": status, "source": body.get("source"), "ok": body.get("ok"),
                          "answer": (body.get("answer") or "")[:300], "error": body.get("error"),
                          "ms": int((time.monotonic() - t0) * 1000)}
        if "voice" in which:
            wav = self.app.localai.sample_wav()
            if wav is None:
                out["voice"] = {"ok": False, "error": {"message": "sample audio not found"}}
            else:
                t0 = time.monotonic()
                status, body = self.app.voice.transcribe(wav)
                out["voice"] = {"status": status, "source": body.get("source"), "ok": body.get("ok"),
                                "text": (body.get("text") or "")[:300], "error": body.get("error"),
                                "ms": int((time.monotonic() - t0) * 1000)}
        return out

    # ======================================================================================
    # logs and usage
    # ======================================================================================
    def api_get_logs(self, data):
        name = data.get("file", "dashboard")
        if name not in LOG_FILES:
            raise ApiError(400, "Unknown log.")
        try:
            lines_wanted = max(10, min(2000, int(data.get("lines", 300))))
        except ValueError:
            lines_wanted = 300
        level = data.get("level", "")
        q = (data.get("q") or "").lower()[:100]
        lines = _tail(os.path.join(self.base_dir, LOG_FILES[name]), 20000)
        if level in ("WARNING", "ERROR"):
            keep = ("WARNING", "ERROR", "CRITICAL") if level == "WARNING" else ("ERROR", "CRITICAL")
            lines = [ln for ln in lines if any(" %s " % k in ln for k in keep) or ln.startswith("Traceback")]
        if q:
            lines = [ln for ln in lines if q in ln.lower()]
        return {"ok": True, "file": name, "lines": lines[-lines_wanted:]}

    def _summarize(self, path):
        tz = self.app.tz
        now = datetime.now(tz)
        day0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        month0 = int(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        blank = lambda: {"count": 0, "ok": 0, "local": 0, "errors": 0, "simulated": 0, "admin_tests": 0,  # noqa: E731
                         "cost_usd": 0.0, "tokens": 0, "audio_seconds": 0.0}
        sums = {"today": blank(), "month": blank()}
        recent = collections.deque(maxlen=25)
        for line in _tail(path, 200000, 8 * 1024 * 1024):
            try:
                e = json.loads(line)
                ts = int(e["ts_ms"])
            except (ValueError, KeyError, TypeError):
                continue
            recent.append({k: e.get(k) for k in ("ts_ms", "source", "model", "outcome", "http_status",
                                                  "duration_ms", "cloud", "simulated", "admin_test",
                                                  "total_tokens", "audio_seconds")})
            for period, start in (("today", day0), ("month", month0)):
                if ts < start:
                    continue
                s = sums[period]
                s["count"] += 1
                outcome = str(e.get("outcome", ""))
                s["ok"] += outcome.startswith("success") or outcome.startswith("no_speech")
                s["errors"] += not (outcome.startswith("success") or outcome.startswith("no_speech"))
                s["local"] += e.get("source") == "local"
                s["simulated"] += bool(e.get("simulated"))
                s["admin_tests"] += bool(e.get("admin_test"))
                cost = e.get("reported_cost_usd") if e.get("reported_cost_usd") is not None else e.get("estimated_cost_usd")
                if isinstance(cost, (int, float)):
                    s["cost_usd"] += cost
                if isinstance(e.get("total_tokens"), int):
                    s["tokens"] += e["total_tokens"]
                if isinstance(e.get("audio_seconds"), (int, float)):
                    s["audio_seconds"] += e["audio_seconds"]
        for s in sums.values():
            s["cost_usd"] = round(s["cost_usd"], 6)
            s["audio_seconds"] = round(s["audio_seconds"], 1)
        return {"totals": sums, "recent": list(reversed(recent))}

    def api_get_usage(self, _data):
        return {"ok": True, "assistant": self._summarize(self.app.assistant.store.usage_path),
                "voice": self._summarize(self.app.voice.store.usage_path)}

    # ======================================================================================
    # maintenance
    # ======================================================================================
    def api_post_maint(self, data):
        a = self.app
        action = data.get("action")
        if action == "refresh":
            a.weather.wake()
            a.maps.wake()
            a.calendar.wake()
            a.health.wake()
            return {"ok": True, "message": "Weather, maps, calendar and health checks are refreshing."}
        if action == "clear_maps":
            a.maps.clear_cache()
            return {"ok": True, "message": "Rendered maps deleted; they are being rebuilt (base tiles kept)."}
        if action == "clear_ai_block":
            a.assistant.store.clear_block()
            return {"ok": True, "message": "The assistant's quota pause was cleared."}
        if action == "backup":
            return self._start_job("backup", self._backup)
        if action == "restart":
            log.warning("admin: restart requested")
            threading.Thread(target=self.restart_hook, name="admin-restart", daemon=True).start()
            return {"ok": True, "message": "Restarting. The page reconnects in a few seconds."}
        raise ApiError(400, "Unknown action.")

    def _backup(self):
        bdir = os.path.join(self.base_dir, "backups")
        os.makedirs(bdir, exist_ok=True)
        name = "admin-%s.tar.gz" % datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(bdir, name)

        def keep(info):
            parts = info.name.split("/")
            if len(parts) > 1 and parts[1] in BACKUP_EXCLUDE:
                return None
            if "__pycache__" in parts or parts[-1].endswith(".tmp"):
                return None
            return info

        with tarfile.open(path, "w:gz") as tar:
            tar.add(self.base_dir, arcname=os.path.basename(self.base_dir), filter=keep)
        return {"ok": True, "path": path, "bytes": os.path.getsize(path),
                "message": "Saved %s (code and config; .env and data/ are not included)." % name}

    def _exec_restart(self):
        time.sleep(0.7)  # let the response reach the browser
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _age(ms):
    if not ms:
        return "never updated"
    s = time.time() - ms / 1000.0
    if s < 90:
        return "updated %d s ago" % s
    if s < 5400:
        return "updated %d min ago" % (s / 60)
    return "updated %.1f h ago" % (s / 3600)


def _cli(argv):
    if argv != ["set-password"]:
        print(__doc__.split("Command line:")[1].split("\n\n")[0].strip())
        return 2
    import server  # local import: config loading lives there
    cfg = server.load_config(os.path.join(server.BASE_DIR, "config.json"))
    path = os.path.join(server.BASE_DIR, cfg["admin"]["password_file"])
    if not sys.stdin.isatty():
        print("Run this in an interactive terminal so the password can be typed without echo.")
        return 1
    pw = getpass.getpass("New admin password (input hidden, at least %d characters): " % MIN_PASSWORD)
    if len(pw) < MIN_PASSWORD:
        print("Too short. Nothing was saved.")
        return 1
    if getpass.getpass("Type it again: ") != pw:
        print("The two entries differ. Nothing was saved.")
        return 1
    envfile.write_private(path, json.dumps(hash_password(pw)) + "\n")
    print("Saved to %s (mode 600). Existing sessions stay signed in until they expire or the server restarts."
          % path)
    print("Open https://<server>:%s/admin" % (cfg["https_port"] or "<https_port>"))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(_cli(sys.argv[1:]))
