"""AdminService: the /api/admin/* entry point, sign-in, sessions and background jobs."""
import collections
import hmac
import http.cookies
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

import assistant as assistant_mod
import netsim

from .core import (ApiError, _clean_err, COOKIE, load_password, log, LOGIN_MAX_FAILURES, LOGIN_WINDOW,
                   verify_password)
from .ai import AIMixin
from .keys import KeysMixin
from .oauth import CalendarOAuthMixin
from .ops import OpsMixin
from .settings import SettingsMixin


class AdminService(KeysMixin, CalendarOAuthMixin, AIMixin, SettingsMixin, OpsMixin):
    """One admin, password sign-in, sessions in memory. handle() is the only entry point;
    it finds endpoints by name: api_<method>_<route> on this class or a mixin."""

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
    # entry point (called by routes/admin_api.py after its https + LAN checks)
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
