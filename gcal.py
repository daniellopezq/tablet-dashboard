"""Google Calendar events for the tablet dashboard.

A background thread refreshes an OAuth access token and lists upcoming events from the
configured calendars (Google Calendar API v3, read-only scope). Requests only ever read the
cached, trimmed result: titles, times and locations. Descriptions, attendees and links are
never fetched beyond what is needed to hide declined events, and never sent to the tablet.

Secrets stay on the server:
  - GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET (a "Desktop app" OAuth client) in the private
    .env file (or the environment), same rules as assistant.py;
  - the refresh token in data/gcal-token.json (mode 600), written by `python3 gcal.py auth`.
None of them is logged or returned to clients.

Command line (run on the server, in a terminal):
  python3 gcal.py auth      one-time sign-in (prompts for the client ID/secret if missing)
  python3 gcal.py status    show what is configured
  python3 gcal.py test      fetch once and print a summary
  python3 gcal.py revoke    revoke the refresh token at Google and delete it locally
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import assistant as assistant_mod
import envfile
import netsim

log = logging.getLogger("dashboard.calendar")

SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/%s/events"
CLIENT_ID_ENV = "GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET"
CLIENT_ID_RE = re.compile(r"^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{10,2048}$")
EVENT_FIELDS = ("items(status,summary,location,start,end,eventType,"
                "attendees(self,responseStatus)),nextPageToken")
MAX_PAGES = 4
FILE_MAX_BYTES = 16384

DEFAULT_CALENDAR_CONFIG = {
    "enabled": True,
    "calendar_ids": ["primary"],
    "days_ahead": 7,
    "max_events": 60,
    "show_location": True,
    "hide_declined": True,
    "refresh_minutes": 10,
    "stale_after_minutes": 40,
    "timeout_seconds": 10,
    "max_response_bytes": 524288,
    "env_file": ".env",
    "token_file": "data/gcal-token.json",
}

# Messages shown on the tablet for each problem code.
PROBLEMS = {
    "not_configured": "Calendar is not connected yet. On the server run: python3 gcal.py auth",
    "insecure_file": "A calendar secret file has unsafe permissions (must be owner-only, mode 600).",
    "invalid_key_format": "The Google client ID or secret in .env looks malformed. Run: python3 gcal.py auth",
    "reauth": "Google sign-in expired or was revoked. On the server run: python3 gcal.py auth",
    "bad_client": "Google rejected the OAuth client ID/secret. Check them, then run: python3 gcal.py auth",
    "api_disabled": "The Google Calendar API is not enabled in the Cloud project.",
    "forbidden": "Google refused access to a calendar (check calendar_ids and the granted scope).",
    "not_found": "A calendar in calendar_ids was not found.",
    "rate_limited": "Google rate limit reached; retrying later.",
    "timeout": "Google did not respond in time.",
    "network error": "Cannot reach Google.",
    "too_large": "Google's response was too large.",
    "invalid response": "Unexpected response from Google.",
    "upstream error": "Google returned an error.",
    "internal error": "Internal error (see the server log).",
}
NEEDS_USER = ("not_configured", "insecure_file", "invalid_key_format", "reauth", "bad_client")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_calendar_config(c):
    if not isinstance(c["enabled"], bool):
        raise ValueError("calendar.enabled must be true or false")
    ids = c["calendar_ids"]
    if (not isinstance(ids, list) or not ids or len(ids) > 10 or len(set(ids)) != len(ids)
            or not all(isinstance(i, str) and 0 < len(i) <= 200 for i in ids)):
        raise ValueError("calendar.calendar_ids must be a list of 1-10 distinct calendar IDs")
    for key, lo, hi in (("days_ahead", 1, 31), ("max_events", 1, 250),
                        ("max_response_bytes", 4096, 4194304)):
        if not isinstance(c[key], int) or isinstance(c[key], bool) or not lo <= c[key] <= hi:
            raise ValueError("calendar.%s must be an integer %d-%d" % (key, lo, hi))
    for key in ("show_location", "hide_declined"):
        if not isinstance(c[key], bool):
            raise ValueError("calendar.%s must be true or false" % key)
    if not _num(c["refresh_minutes"]) or c["refresh_minutes"] < 2:
        raise ValueError("calendar.refresh_minutes must be at least 2")
    if not _num(c["stale_after_minutes"]) or c["stale_after_minutes"] <= 0:
        raise ValueError("calendar.stale_after_minutes must be positive")
    if not _num(c["timeout_seconds"]) or not 0 < c["timeout_seconds"] <= 60:
        raise ValueError("calendar.timeout_seconds must be between 0 and 60")
    for key in ("env_file", "token_file"):
        if not isinstance(c[key], str) or not c[key]:
            raise ValueError("calendar.%s must be a path string" % key)


# --- secrets -------------------------------------------------------------------------------

def load_client(env_path):
    """Return ((client_id, client_secret), problem). Exactly one is None."""
    cid, problem = assistant_mod.load_api_key(env_path, (CLIENT_ID_ENV,))
    if problem:
        return None, problem
    if not CLIENT_ID_RE.match(cid):
        return None, "invalid_key_format"
    secret, problem = assistant_mod.load_api_key(env_path, (CLIENT_SECRET_ENV,))
    if problem:
        return None, problem
    return (cid, secret), None


_read_private = envfile.read_private
write_private = envfile.write_private
set_env_values = envfile.set_values


def load_refresh_token(token_path):
    """Return (refresh_token, mtime_ns, problem)."""
    try:
        raw, mtime = _read_private(token_path)
    except FileNotFoundError:
        return None, None, "not_configured"
    except OSError:  # includes PermissionError and symlinks (ELOOP)
        return None, None, "insecure_file"
    try:
        token = json.loads(raw).get("refresh_token")
    except (ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None, mtime, "reauth"
    return token, mtime, None


def token_info(token_path):
    """{"problem", "created"} for the sign-in token file, without the token itself."""
    refresh, _, problem = load_refresh_token(token_path)
    created = None
    if refresh:
        try:
            created = json.loads(_read_private(token_path)[0]).get("created")
        except (OSError, ValueError, AttributeError):
            pass
    return {"problem": problem, "created": created if isinstance(created, str) else None}


def revoke_token(token_path):
    """Revoke the refresh token at Google and delete it locally. Returns a list of messages."""
    out = []
    refresh, _, problem = load_refresh_token(token_path)
    if refresh:
        req = urllib.request.Request(REVOKE_URL, data=urllib.parse.urlencode({"token": refresh}).encode(),
                                     method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            _http(urllib.request.build_opener(assistant_mod._NoRedirect), req, 15, 65536)
            out.append("Revoked at Google.")
        except CalendarError as exc:
            out.append("Google revoke call failed (%s); remove access at https://myaccount.google.com/connections"
                       % (exc.detail[0] if exc.code == "http" else exc.code))
    elif problem != "not_configured":
        out.append("Token file unreadable (%s)." % problem)
    try:
        os.remove(token_path)
        out.append("Deleted the local sign-in token.")
    except FileNotFoundError:
        out.append("No local token.")
    return out


# --- HTTP ------------------------------------------------------------------------------------

class CalendarError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _http(opener, req, timeout, limit):
    """Return parsed JSON. Raises CalendarError; HTTP errors carry the status in .detail."""
    deadline = time.monotonic() + timeout * 2
    if netsim.blocked("calendar"):
        raise CalendarError("network error")  # offline test switch
    try:
        with opener.open(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > limit:
                raise CalendarError("too_large")
            chunks, total = [], 0
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise CalendarError("too_large")
                if time.monotonic() > deadline:
                    raise CalendarError("timeout")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(8192).decode("utf-8", "replace"))
        except Exception:
            body = {}
        raise CalendarError("http", (exc.code, body if isinstance(body, dict) else {}))
    except CalendarError:
        raise
    except (TimeoutError, OSError) as exc:
        raise CalendarError("timeout" if "timed out" in str(exc) else "network error")
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CalendarError("invalid response")
    if not isinstance(data, dict):
        raise CalendarError("invalid response")
    return data


def _api_reason(body):
    """Collect Google error reason strings (lower-case) from an API error body."""
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    found = [str(err.get("status", ""))]
    for e in err.get("errors") or []:
        if isinstance(e, dict):
            found.append(str(e.get("reason", "")))
    for d in err.get("details") or []:
        if isinstance(d, dict):
            found.append(str(d.get("reason", "")))
    return " ".join(found).lower()


def _clean(text, limit):
    text = re.sub(r"\s+", " ", text if isinstance(text, str) else "").strip()
    return text[:limit - 1] + "…" if len(text) > limit else text


class CalendarService:
    """Fetches events in a background thread; requests only ever read the cache."""

    def __init__(self, ccfg, tz, base_dir, user_agent, clock=time.time):
        self.cfg = ccfg
        self.tz = tz
        self.clock = clock
        self.user_agent = user_agent
        self.env_path = os.path.join(base_dir, ccfg["env_file"])
        self.token_path = os.path.join(base_dir, ccfg["token_file"])
        self._opener = urllib.request.build_opener(assistant_mod._NoRedirect)
        self._lock = threading.Lock()
        self._access = None          # (token, expires_at) kept in memory only
        self._blocked_mtime = None   # token file mtime that Google rejected (invalid_grant)
        self._days = None
        self._count = 0
        self._truncated = False
        self._updated_ms = None
        self._last_attempt_ms = None
        self._problem = None
        self._failures = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    @property
    def enabled(self):
        return self.cfg["enabled"]

    def _now_ms(self):
        return int(self.clock() * 1000)

    # -- OAuth ----------------------------------------------------------------------------
    def _access_token(self, client, refresh, force=False):
        now = self.clock()
        if not force and self._access and self._access[1] > now + 60:
            return self._access[0]
        form = urllib.parse.urlencode({
            "client_id": client[0], "client_secret": client[1],
            "refresh_token": refresh, "grant_type": "refresh_token"}).encode()
        req = urllib.request.Request(TOKEN_URL, data=form, method="POST", headers={
            "User-Agent": self.user_agent, "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded"})
        try:
            data = _http(self._opener, req, self.cfg["timeout_seconds"], 65536)
        except CalendarError as exc:
            if exc.code != "http":
                raise
            status, body = exc.detail
            err = str(body.get("error", ""))
            if err == "invalid_grant":
                raise CalendarError("reauth")
            if err in ("invalid_client", "unauthorized_client"):
                raise CalendarError("bad_client")
            log.warning("calendar: token endpoint HTTP %s %s", status, err[:40])
            raise CalendarError("rate_limited" if status == 429 else "upstream error")
        token, ttl = data.get("access_token"), data.get("expires_in", 3600)
        if not isinstance(token, str) or not TOKEN_RE.match(token):
            raise CalendarError("invalid response")
        if "scope" in data and SCOPE not in str(data["scope"]).split():
            raise CalendarError("reauth")  # signed in without ticking the calendar box
        self._access = (token, now + (ttl if _num(ttl) else 3600))
        return token

    # -- events ---------------------------------------------------------------------------
    def window(self):
        """(start, end) datetimes: local midnight today .. midnight after days_ahead days."""
        today = datetime.fromtimestamp(self.clock(), self.tz).date()
        start = datetime.combine(today, datetime.min.time(), self.tz)
        end = datetime.combine(today + timedelta(days=self.cfg["days_ahead"]), datetime.min.time(), self.tz)
        return start, end

    def _list(self, cal_id, token, start, end):
        items, page = [], None
        for _ in range(MAX_PAGES):
            params = {
                "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                "singleEvents": "true", "orderBy": "startTime",
                "maxResults": str(min(250, self.cfg["max_events"] + 10)),
                "timeZone": self.tz.key, "fields": EVENT_FIELDS,
            }
            if page:
                params["pageToken"] = page
            url = EVENTS_URL % urllib.parse.quote(cal_id, safe="") + "?" + urllib.parse.urlencode(params)
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent, "Accept": "application/json",
                "Authorization": "Bearer " + token})
            data = _http(self._opener, req, self.cfg["timeout_seconds"], self.cfg["max_response_bytes"])
            got = data.get("items", [])
            if not isinstance(got, list):
                raise CalendarError("invalid response")
            items.extend(got)
            page = data.get("nextPageToken")
            if not page or len(items) > self.cfg["max_events"]:
                break
        return items

    def _list_checked(self, cal_id, client, refresh, start, end):
        token = self._access_token(client, refresh)
        for attempt in (0, 1):
            try:
                return self._list(cal_id, token, start, end)
            except CalendarError as exc:
                if exc.code != "http":
                    raise
                status, body = exc.detail
                reason = _api_reason(body)
                if status == 401 and attempt == 0:
                    token = self._access_token(client, refresh, force=True)
                    continue
                if "accessnotconfigured" in reason or "service_disabled" in reason:
                    raise CalendarError("api_disabled")
                if status == 429 or "ratelimitexceeded" in reason or "quotaexceeded" in reason:
                    raise CalendarError("rate_limited")
                if status == 404:
                    raise CalendarError("not_found")
                if status in (401, 403):
                    raise CalendarError("forbidden")
                log.warning("calendar: events HTTP %s", status)
                raise CalendarError("upstream error")

    def _parse_when(self, when):
        """Return (aware datetime or date, is_all_day)."""
        if not isinstance(when, dict):
            raise ValueError("missing time")
        if isinstance(when.get("dateTime"), str):
            dt = datetime.fromisoformat(when["dateTime"])
            if dt.tzinfo is None:
                raise ValueError("naive time")
            return dt.astimezone(self.tz), False
        if isinstance(when.get("date"), str) and re.match(r"^\d{4}-\d{2}-\d{2}$", when["date"]):
            return date.fromisoformat(when["date"]), True
        raise ValueError("missing time")

    def normalize(self, items, start, end):
        """Group trimmed events by local day. Returns (days, count, truncated)."""
        first_day, last_day = start.date(), (end - timedelta(seconds=1)).date()
        events = []
        for it in items:
            if not isinstance(it, dict) or it.get("status") == "cancelled":
                continue
            if it.get("eventType") == "workingLocation":
                continue
            if self.cfg["hide_declined"]:
                me = [a for a in it.get("attendees") or [] if isinstance(a, dict) and a.get("self")]
                if me and me[0].get("responseStatus") == "declined":
                    continue
            try:
                s, all_day = self._parse_when(it.get("start"))
                e, _ = self._parse_when(it.get("end"))
            except ValueError:
                continue
            ev = {
                "title": _clean(it.get("summary"), 120) or "(No title)",
                "all_day": all_day,
                "location": _clean(it.get("location"), 80) if self.cfg["show_location"] else "",
            }
            if all_day:
                d0, d1 = s, max(s, e - timedelta(days=1))  # Google's end date is exclusive
                ev.update(start_ms=None, end_ms=None, start_hm="", end_hm="")
            else:
                d0, d1 = s.date(), max(s, e - timedelta(microseconds=1)).date()
                ev.update(start_ms=int(s.timestamp() * 1000), end_ms=int(e.timestamp() * 1000),
                          start_hm=s.strftime("%H:%M"), end_hm=e.strftime("%H:%M"))
            ev["multi_day"] = d1 > d0
            ev["_sort"] = (d0, 0 if all_day else 1, ev["start_ms"] or 0, ev["title"].lower())
            ev["_span"] = (max(d0, first_day), min(d1, last_day))
            events.append(ev)

        events.sort(key=lambda x: x["_sort"])
        truncated = len(events) > self.cfg["max_events"]
        events = events[:self.cfg["max_events"]]
        by_day = {first_day: []}
        for ev in events:
            a, b = ev.pop("_span")
            ev.pop("_sort")
            d = a
            while d <= b:
                by_day.setdefault(d, []).append(dict(ev))
                if not ev["all_day"]:
                    break  # timed events are listed on their first visible day only
                d += timedelta(days=1)
        days = [{"date": d.isoformat(), "events": by_day[d]} for d in sorted(by_day)]
        return days, len(events), truncated

    def fetch_once(self):
        """Fetch and cache events. Returns True on success. Never raises."""
        if not self.enabled:
            return False
        started = self._now_ms()
        try:
            client, problem = load_client(self.env_path)
            if problem:
                raise CalendarError(problem)
            refresh, mtime, problem = load_refresh_token(self.token_path)
            if problem:
                raise CalendarError(problem)
            if mtime == self._blocked_mtime:
                raise CalendarError("reauth")  # don't retry a rejected token until it changes
            start, end = self.window()
            items = []
            for cal_id in self.cfg["calendar_ids"]:
                items.extend(self._list_checked(cal_id, client, refresh, start, end))
            days, count, truncated = self.normalize(items, start, end)
        except CalendarError as exc:
            code = exc.code if exc.code in PROBLEMS else "upstream error"
            if code == "reauth":
                self._access = None
                self._blocked_mtime = load_refresh_token(self.token_path)[1]
            with self._lock:
                if code != self._problem:
                    log.warning("calendar: %s", code)
                self._last_attempt_ms, self._problem = started, code
                self._failures += 1
            return False
        except Exception:
            log.exception("calendar: unexpected error")
            with self._lock:
                self._last_attempt_ms, self._problem = started, "internal error"
                self._failures += 1
            return False
        with self._lock:
            if self._problem or self._updated_ms is None:
                log.info("calendar: updated (%d events)", count)
            self._days, self._count, self._truncated = days, count, truncated
            self._updated_ms = self._now_ms()
            self._last_attempt_ms = started
            self._problem = None
            self._failures = 0
        return True

    def next_delay(self):
        refresh = self.cfg["refresh_minutes"] * 60
        with self._lock:
            failures, problem = self._failures, self._problem
        if failures == 0:
            return refresh
        if problem in NEEDS_USER:
            return 60  # cheap local re-check, so a fresh sign-in is picked up within a minute
        return min(refresh, 60 * (2 ** (failures - 1)))

    def snapshot(self):
        with self._lock:
            problem = self._problem
            snap = {
                "enabled": self.enabled,
                "timezone": self.tz.key,
                "days_ahead": self.cfg["days_ahead"],
                "updated_ms": self._updated_ms,
                "last_attempt_ms": self._last_attempt_ms,
                "problem": problem,
                "message": PROBLEMS.get(problem, "") if problem else "",
                "stale": False,
                "days": self._days,
                "count": self._count,
                "truncated": self._truncated,
            }
            if not self.enabled:
                snap["status"] = "disabled"
            elif problem in NEEDS_USER and self._days is None:
                snap["status"] = "setup"
            elif self._days is None:
                snap["status"] = "error" if problem else "loading"
            else:
                snap["status"] = "ok"
                age_ms = self._now_ms() - self._updated_ms
                snap["stale"] = bool(problem) or age_ms > self.cfg["stale_after_minutes"] * 60000
        return snap

    def _run(self):
        while not self._stop.is_set():
            self.fetch_once()
            self._wake.wait(self.next_delay())
            self._wake.clear()

    def start(self):
        if not self.enabled:
            log.info("calendar: disabled in config")
            return
        self._thread = threading.Thread(target=self._run, name="calendar", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """Fetch now (admin page), e.g. right after a new sign-in."""
        self._blocked_mtime = None
        self._wake.set()


# --- one-time sign-in (loopback redirect + PKCE) ---------------------------------------------

def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url(client_id, redirect_uri, state, challenge, login_hint=""):
    params = {
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def parse_redirect(text, expected_state):
    """Return the code from a redirect URL or query string. Raises ValueError with a message."""
    text = (text or "").strip()
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("/?")
    if "?" in query:
        query = query.split("?", 1)[1]
    q = urllib.parse.parse_qs(query)
    if q.get("error"):
        raise ValueError("Google returned an error: %s" % q["error"][0][:60])
    if q.get("state", [""])[0] != expected_state:
        raise ValueError("The state value does not match this sign-in attempt. Start again.")
    code = q.get("code", [""])[0]
    if not code or len(code) > 1024:
        raise ValueError("No authorization code found in that URL.")
    granted = q.get("scope", [SCOPE])[0].split()
    if SCOPE not in granted:
        raise ValueError("Calendar access was not granted. Start again and tick the "
                         "\"See events on all your calendars\" box.")
    return code


def exchange_code(client, code, verifier, redirect_uri, timeout=15):
    form = urllib.parse.urlencode({
        "client_id": client[0], "client_secret": client[1], "code": code,
        "code_verifier": verifier, "redirect_uri": redirect_uri,
        "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=form, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(assistant_mod._NoRedirect)
    try:
        data = _http(opener, req, timeout, 65536)
    except CalendarError as exc:
        if exc.code == "http":
            body = exc.detail[1]
            raise ValueError("Google rejected the sign-in (%s: %s)" % (
                str(body.get("error", exc.detail[0]))[:40], str(body.get("error_description", ""))[:120]))
        raise ValueError("Could not reach Google (%s)" % exc.code)
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not TOKEN_RE.match(refresh):
        raise ValueError("Google did not return a refresh token. Remove the app's access at "
                         "https://myaccount.google.com/connections and try again.")
    if SCOPE not in str(data.get("scope", "")).split():
        raise ValueError("Calendar access was not granted. Start again and tick the calendar box.")
    return refresh


def _cli_paths():
    import config  # local import: only for the command line
    cfg = config.load_config(os.path.join(config.BASE_DIR, "config.json"))
    c = cfg["calendar"]
    return (cfg, os.path.join(config.BASE_DIR, c["env_file"]),
            os.path.join(config.BASE_DIR, c["token_file"]))


def _prompt_client(env_path):
    import getpass
    print("Paste the OAuth client details from Google Cloud Console")
    print("(Google Auth Platform > Clients > your Desktop client).")
    cid = input("Client ID: ").strip()
    if not CLIENT_ID_RE.match(cid):
        sys.exit("That doesn't look like a client ID (…apps.googleusercontent.com). Nothing saved.")
    secret = getpass.getpass("Client secret (input hidden): ").strip()
    if not assistant_mod.KEY_RE.match(secret):
        sys.exit("That doesn't look like a client secret. Nothing saved.")
    set_env_values(env_path, {CLIENT_ID_ENV: cid, CLIENT_SECRET_ENV: secret})
    print("Saved to %s (mode 600)." % env_path)


def cmd_auth(args):
    import select
    from http.server import BaseHTTPRequestHandler, HTTPServer
    _, env_path, token_path = _cli_paths()
    client, problem = load_client(env_path)
    if problem or "--new-client" in args:
        if problem == "insecure_file":
            sys.exit("%s must be a regular file owned by you with mode 600." % env_path)
        _prompt_client(env_path)
        client, problem = load_client(env_path)
        if problem:
            sys.exit("Client details could not be read back (%s)." % problem)

    result = {}
    state = secrets.token_urlsafe(24)
    verifier, challenge = pkce_pair()

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                result["code"] = parse_redirect(self.path, state)
                msg = "Signed in. You can close this tab and return to the terminal."
            except ValueError as exc:
                result.setdefault("error", str(exc))
                msg = str(exc)
            body = ("<!doctype html><meta charset=utf-8><title>Dashboard calendar</title>"
                    "<p style='font:18px sans-serif;margin:2em'>%s</p>" % msg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    port = 0
    for a in args:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    srv = HTTPServer(("127.0.0.1", port), Catch)
    redirect_uri = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hint = next((a.split("=", 1)[1] for a in args if a.startswith("--login-hint=")), "")
    url = build_auth_url(client[0], redirect_uri, state, challenge, hint)

    print("\n1. Open this URL in a browser and sign in with the calendar's Google account:\n")
    print(url)
    print("\n2. If Google says the app isn't verified: Advanced > Go to … (unsafe). It is your own app.")
    print("3. Tick the calendar permission box and continue.")
    print("\nIf the browser runs on this server, sign-in finishes by itself. If it runs on another")
    print("computer, the last page will fail to load (\"can't reach 127.0.0.1\"): copy that page's")
    print("full address from the address bar and paste it here, then press Enter.\n")
    deadline = time.monotonic() + 600
    try:
        while "code" not in result and time.monotonic() < deadline:
            if "error" in result:
                sys.exit(result["error"])
            ready, _, _ = select.select([sys.stdin], [], [], 1.0)
            if ready:
                line = sys.stdin.readline()
                if not line:
                    time.sleep(1)
                    continue
                if line.strip():
                    try:
                        result["code"] = parse_redirect(line, state)
                    except ValueError as exc:
                        print(exc)
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    finally:
        srv.shutdown()
    if "code" not in result:
        sys.exit("Timed out after 10 minutes. Run it again.")
    try:
        refresh = exchange_code(client, result["code"], verifier, redirect_uri)
    except ValueError as exc:
        sys.exit(str(exc))
    write_private(token_path, json.dumps({
        "refresh_token": refresh, "scope": SCOPE,
        "created": datetime.now().astimezone().isoformat(timespec="seconds")}) + "\n")
    print("Signed in. Token saved to %s (mode 600)." % token_path)
    print("The running dashboard picks it up within a minute; no restart needed.")


def cmd_status(_args):
    cfg, env_path, token_path = _cli_paths()
    c = cfg["calendar"]
    _, problem = load_client(env_path)
    print("enabled:       %s" % c["enabled"])
    print("calendars:     %s" % ", ".join(c["calendar_ids"]))
    print("client:        %s" % ("ok" if not problem else problem))
    _, _, problem = load_refresh_token(token_path)
    print("sign-in token: %s" % ("ok" if not problem else problem))


def cmd_test(_args):
    import server
    from zoneinfo import ZoneInfo
    cfg, _, _ = _cli_paths()
    svc = CalendarService(cfg["calendar"], ZoneInfo(cfg["timezone"]), server.BASE_DIR, server.USER_AGENT)
    ok = svc.fetch_once()
    snap = svc.snapshot()
    if not ok:
        sys.exit("Failed: %s. %s" % (snap["problem"], snap["message"]))
    print("OK: %d events over %d days%s." % (snap["count"], cfg["calendar"]["days_ahead"],
                                             " (truncated)" if snap["truncated"] else ""))
    for d in snap["days"]:
        for ev in d["events"][:3]:
            print("  %s  %-11s %s" % (d["date"], "all day" if ev["all_day"] else ev["start_hm"] + "-" + ev["end_hm"],
                                      ev["title"]))


def cmd_revoke(_args):
    _, _, token_path = _cli_paths()
    for line in revoke_token(token_path):
        print(line)


def main(argv):
    cmds = {"auth": cmd_auth, "status": cmd_status, "test": cmd_test, "revoke": cmd_revoke}
    if not argv or argv[0] not in cmds:
        print(__doc__.split("Command line")[1].split("\n", 1)[1].rstrip())
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    cmds[argv[0]](argv[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
