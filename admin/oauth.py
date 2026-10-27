"""Google Calendar sign-in from the admin page (loopback or paste-the-URL OAuth)."""
import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import gcal as gcal_mod
import gcal_auth

from .core import ApiError, log


class CalendarOAuthMixin:
    """Mixed into AdminService (admin/service.py)."""

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
        verifier, challenge = gcal_auth.pkce_pair()
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
                           "auth_url": gcal_auth.build_auth_url(client[0], redirect_uri, state, challenge, hint)}
            return {"ok": True, "pending": self._oauth_public()}

    def _finish_oauth(self, text, from_loopback=False):
        """Complete sign-in from a redirect URL. Returns a message (also stored for the page)."""
        with self._lock:
            o = self._oauth
            if not o or o["result"] is not None or time.time() > o["expires"]:
                return "No sign-in is waiting. Start again from the admin page."
            o["result"] = "working"
        try:
            code = gcal_auth.parse_redirect(text, o["state"])
            refresh = gcal_auth.exchange_code(o["client"], code, o["verifier"], o["redirect_uri"])
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
