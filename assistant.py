"""Standalone-question AI assistant backend (Vercel AI Gateway, OpenAI-compatible).

Each question is sent alone with a short system instruction: no history, no tools.
Python 3 standard library only. The API key is read from the environment or a private
.env file and is never logged, returned to clients or written anywhere else.

Global usage limits (min interval, per-hour, per-day) are enforced here and persisted in a
small JSON store outside the web directory, protected by a thread lock and an flock so
concurrent requests and restarts cannot bypass them.
"""
import contextlib
import fcntl
import ipaddress
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import fallback as fb
import netsim

log = logging.getLogger("dashboard.assistant")

SYSTEM_PROMPT = (
    "Answer the user's standalone question clearly and briefly, usually within 120 words. "
    "If uncertain, say so. You have no access to live weather, calendars, cameras, or server "
    "status. Do not claim to have checked them."
)
# The local fallback model gets its own instruction. Small models read "you have no access to
# live data" as a reason to refuse plain facts, drift out of the question's language, and like
# markdown/LaTeX, which the plain-text answer box shows as raw symbols.
LOCAL_SYSTEM_PROMPT = (
    "Answer the user's question in one or two short sentences, in the same language as the "
    "question. Use plain text only: no markdown, no asterisks, no LaTeX or math notation. "
    "If you are not sure of a fact, say so briefly."
)
LOCAL_TEMPERATURE = 0.3  # the 2B model was most accurate at low temperature in testing

KEY_ENV = "AI_GATEWAY_API_KEY"
KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")
MAX_ANSWER_CHARS = 4000
HOUR_MS = 3600 * 1000
KEEP_MS = 50 * HOUR_MS

DEFAULT_ASSISTANT_CONFIG = {
    "enabled": True,
    "model": "google/gemini-2.5-flash-lite",
    "base_url": "https://ai-gateway.vercel.sh/v1",
    "env_file": ".env",
    "state_file": "data/assistant-state.json",
    "usage_log": "data/assistant-usage.jsonl",
    "max_question_chars": 1000,
    "max_body_bytes": 8192,
    "max_output_tokens": 300,
    "disable_thinking": True,
    "timeout_seconds": 30,
    "max_response_bytes": 65536,
    "min_interval_seconds": 10,
    "max_per_hour": 20,
    "max_per_day": 100,
    "quota_block_minutes": 60,
    "allowed_hosts": [],
    "price_per_million_input_usd": None,
    "price_per_million_output_usd": None,
    # Local fallback (llama-server on this machine). Used ONLY when the cloud sends no response
    # headers within no_response_seconds; any HTTP status is a response and never falls back.
    "fallback_enabled": True,
    "fallback_base_url": "http://127.0.0.1:8091/v1",
    "fallback_model": "qwen3.5-2b",
    "fallback_max_output_tokens": 120,  # ~11 s at the measured 11 tok/s, inside the 25 s budget
    "fallback_timeout_seconds": 25,
    "no_response_seconds": 5,
    "cloud_cooldown_seconds": 60,
}


def now_ms():
    return int(time.time() * 1000)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_assistant_config(a):
    """Raise ValueError (caller wraps it) for unusable settings."""
    if not isinstance(a["enabled"], bool):
        raise ValueError("assistant.enabled must be true or false")
    if not isinstance(a["model"], str) or not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:-]+$", a["model"]):
        raise ValueError("assistant.model must look like provider/model")
    parts = urllib.parse.urlsplit(a["base_url"]) if isinstance(a["base_url"], str) else None
    if not parts or parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("assistant.base_url must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("assistant.base_url must use https (http is allowed only for loopback tests)")
    for key in ("env_file", "state_file", "usage_log"):
        if not isinstance(a[key], str) or not a[key]:
            raise ValueError("assistant.%s must be a path string" % key)
    for key, lo, hi in (("max_question_chars", 1, 4000), ("max_body_bytes", 512, 65536),
                        ("max_output_tokens", 16, 4096), ("max_response_bytes", 1024, 1048576),
                        ("max_per_hour", 1, 10000), ("max_per_day", 1, 100000),
                        ("quota_block_minutes", 1, 10080)):
        if not isinstance(a[key], int) or isinstance(a[key], bool) or not lo <= a[key] <= hi:
            raise ValueError("assistant.%s must be an integer %d-%d" % (key, lo, hi))
    if not _num(a["timeout_seconds"]) or not 1 <= a["timeout_seconds"] <= 120:
        raise ValueError("assistant.timeout_seconds must be 1-120")
    if not _num(a["min_interval_seconds"]) or not 0 <= a["min_interval_seconds"] <= 3600:
        raise ValueError("assistant.min_interval_seconds must be 0-3600")
    if not isinstance(a["disable_thinking"], bool):
        raise ValueError("assistant.disable_thinking must be true or false")
    if not isinstance(a["allowed_hosts"], list) or not all(isinstance(h, str) for h in a["allowed_hosts"]):
        raise ValueError("assistant.allowed_hosts must be a list of host names")
    for key in ("price_per_million_input_usd", "price_per_million_output_usd"):
        if a[key] is not None and (not _num(a[key]) or a[key] < 0):
            raise ValueError("assistant.%s must be a non-negative number or null" % key)
    validate_fallback_config(a, "assistant", (16, 4096))


def validate_fallback_config(c, section, token_range=None):
    """Shared checks for the local-fallback keys of the assistant and voice sections."""
    if not isinstance(c["fallback_enabled"], bool):
        raise ValueError("%s.fallback_enabled must be true or false" % section)
    parts = urllib.parse.urlsplit(c["fallback_base_url"]) if isinstance(c["fallback_base_url"], str) else None
    if not parts or parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("%s.fallback_base_url must be an http URL on this machine (127.0.0.1)" % section)
    if "fallback_model" in c and (not isinstance(c["fallback_model"], str)
                                  or not re.match(r"^[A-Za-z0-9._:/-]{1,100}$", c["fallback_model"])):
        raise ValueError("%s.fallback_model must be a model name" % section)
    if token_range and (not isinstance(c["fallback_max_output_tokens"], int)
                        or isinstance(c["fallback_max_output_tokens"], bool)
                        or not token_range[0] <= c["fallback_max_output_tokens"] <= token_range[1]):
        raise ValueError("%s.fallback_max_output_tokens must be an integer %d-%d" % ((section,) + token_range))
    for key, lo, hi in (("fallback_timeout_seconds", 1, 120), ("no_response_seconds", 0.1, 60),
                        ("cloud_cooldown_seconds", 0, 3600)):
        if not _num(c[key]) or not lo <= c[key] <= hi:
            raise ValueError("%s.%s must be a number %s-%s" % (section, key, lo, hi))


# --- API key -------------------------------------------------------------------------------

def load_api_key(env_path, names=(KEY_ENV,)):
    """Return (key, problem). Exactly one is None. Messages never contain the key.

    `names` are variable names in priority order (e.g. a standard name plus an alias). The
    environment wins over the file. A .env file is used only if it is a regular, non-symlink
    file owned by this user with no group/other permissions.
    """
    key = ""
    for name in names:
        key = os.environ.get(name, "").strip()
        if key:
            break
    if not key:
        try:
            fd = os.open(env_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, "not_configured"
        except OSError:
            return None, "insecure_file"
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
                return None, "insecure_file"
            raw = os.read(fd, 8192).decode("utf-8", "replace")
        finally:
            os.close(fd)
        found = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, sep, value = line.partition("=")
            if sep and name.strip() in names:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                found[name.strip()] = value.strip()
        for name in names:
            if found.get(name):
                key = found[name]
                break
        if not key:
            return None, "not_configured"
    if not KEY_RE.match(key):
        return None, "invalid_key_format"
    return key, None


# --- request checks (used by the HTTP handler) ---------------------------------------------

def is_lan_client(ip):
    """True for loopback and private-range clients only (the assistant is LAN-only)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _host_only(value):
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].split("]")[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def check_origin(headers, allowed_hosts):
    """Return an error string for cross-origin or rebinding-suspect requests, else None."""
    host = headers.get("Host", "")
    hostname = _host_only(host)
    if not hostname:
        return "missing host"
    is_ip = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip = False
    if not is_ip and hostname != "localhost" and hostname not in [h.lower() for h in allowed_hosts]:
        return "host not allowed"
    origin = headers.get("Origin")
    if origin is not None:
        if origin == "null":
            return "cross-origin"
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != host.strip().lower():
            return "cross-origin"
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return "cross-site"
    return None


class RequestError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def parse_question(raw, cfg):
    """Validate the JSON body and return the question string (RequestError on bad input)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise RequestError(400, "bad_request", "Request must be valid JSON.")
    if not isinstance(data, dict) or set(data.keys()) != {"question"}:
        raise RequestError(400, "bad_request", "Request must be an object with only a question.")
    q = data["question"]
    if not isinstance(q, str):
        raise RequestError(400, "bad_request", "The question must be text.")
    q = q.strip()
    if not q:
        raise RequestError(400, "empty_question", "Type a question first.")
    if "\x00" in q:
        raise RequestError(400, "bad_request", "The question contains invalid characters.")
    if len(q) > cfg["max_question_chars"]:
        raise RequestError(400, "too_long", "Questions are limited to %d characters." % cfg["max_question_chars"])
    return q


# --- usage store ---------------------------------------------------------------------------

def _fmt_wait(seconds):
    seconds = max(1, int(math.ceil(seconds)))
    if seconds < 90:
        return "%d seconds" % seconds
    minutes = int(math.ceil(seconds / 60.0))
    if minutes < 90:
        return "%d minutes" % minutes
    return "%d hours" % int(math.ceil(minutes / 60.0))


class UsageStore:
    """Persistent attempt counters plus an append-only usage log. Safe across threads/processes."""

    def __init__(self, state_path, usage_path, tz, clock=now_ms):
        self.state_path = state_path
        self.usage_path = usage_path
        self.tz = tz
        self.clock = clock
        self._tlock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self):
        directory = os.path.dirname(self.state_path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with self._tlock:
            fd = os.open(self.state_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # closing releases the flock

    def _load(self):
        state = {"attempts": [], "blocked_until_ms": 0, "blocked_reason": ""}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                attempts = data.get("attempts")
                if isinstance(attempts, list):
                    state["attempts"] = sorted(int(t) for t in attempts if isinstance(t, int))
                if isinstance(data.get("blocked_until_ms"), int):
                    state["blocked_until_ms"] = data["blocked_until_ms"]
                if isinstance(data.get("blocked_reason"), str):
                    state["blocked_reason"] = data["blocked_reason"][:64]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            # Unreadable counters must not silently reset limits: fail closed for this call.
            log.error("assistant: usage state unreadable; refusing requests until it is fixed")
            raise
        return state

    def _save(self, state):
        directory = os.path.dirname(self.state_path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    def _day_bounds(self, now):
        local = datetime.fromtimestamp(now / 1000.0, self.tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        nxt = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000), int(nxt.timestamp() * 1000)

    def _counts(self, state, now, limits):
        day_start, day_end = self._day_bounds(now)
        attempts = state["attempts"]
        hour = [t for t in attempts if t > now - HOUR_MS]
        day = [t for t in attempts if t >= day_start]
        return {
            "hour_used": len(hour), "day_used": len(day),
            "hour_left": max(0, limits["max_per_hour"] - len(hour)),
            "day_left": max(0, limits["max_per_day"] - len(day)),
            "hour_list": hour, "day_end": day_end,
        }

    def snapshot(self, limits):
        """Read-only view for the status endpoint."""
        now = self.clock()
        with self._locked():
            state = self._load()
            c = self._counts(state, now, limits)
        blocked = state["blocked_until_ms"] > now
        return {
            "remaining": {"hour": c["hour_left"], "day": c["day_left"]},
            "blocked": blocked,
            "blocked_retry_after": int(math.ceil((state["blocked_until_ms"] - now) / 1000.0)) if blocked else 0,
        }

    def reserve(self, limits):
        """Check limits and, if allowed, durably count one attempt before the upstream call.

        Returns (allowed, info). info has retry_after/code/message when denied, or remaining
        counts when allowed.
        """
        now = self.clock()
        with self._locked():
            state = self._load()
            state["attempts"] = [t for t in state["attempts"] if t > now - KEEP_MS]
            if state["blocked_until_ms"] > now:
                wait = (state["blocked_until_ms"] - now) / 1000.0
                return False, {"code": "quota_exhausted", "status": 503, "retry_after": int(math.ceil(wait)),
                               "message": "The assistant is unavailable: the AI credit or access allowance "
                                          "was reported as used up. It will be checked again in about %s."
                                          % _fmt_wait(wait)}
            c = self._counts(state, now, limits)
            denials = []
            attempts = state["attempts"]
            interval = limits["min_interval_seconds"] * 1000
            if attempts and now - attempts[-1] < interval:
                wait = (attempts[-1] + interval - now) / 1000.0
                denials.append((wait, "rate_limited", "Please wait %s before asking again." % _fmt_wait(wait)))
            if c["hour_used"] >= limits["max_per_hour"]:
                hour = c["hour_list"]
                release = hour[len(hour) - limits["max_per_hour"]] + HOUR_MS
                wait = (release - now) / 1000.0
                denials.append((wait, "hourly_limit", "Hourly limit of %d %s reached. Try again in about %s."
                                % (limits["max_per_hour"], limits.get("noun", "questions"), _fmt_wait(wait))))
            if c["day_used"] >= limits["max_per_day"]:
                wait = (c["day_end"] - now) / 1000.0
                denials.append((wait, "daily_limit", "Daily limit of %d %s reached. It resets at midnight "
                                "(%s time), in about %s."
                                % (limits["max_per_day"], limits.get("noun", "questions"), self.tz.key, _fmt_wait(wait))))
            if denials:
                wait, code, message = max(denials, key=lambda d: d[0])
                return False, {"code": code, "status": 429, "retry_after": int(math.ceil(wait)), "message": message}
            attempts.append(now)
            self._save(state)
            return True, {"remaining": {"hour": c["hour_left"] - 1, "day": c["day_left"] - 1}}

    def block(self, minutes, reason):
        with self._locked():
            state = self._load()
            state["blocked_until_ms"] = self.clock() + int(minutes * 60000)
            state["blocked_reason"] = reason
            self._save(state)

    def clear_block(self):
        with self._locked():
            state = self._load()
            state["blocked_until_ms"] = 0
            state["blocked_reason"] = ""
            self._save(state)

    def record(self, entry):
        """Append one usage line (metadata only; never question, answer or key)."""
        line = json.dumps(entry, separators=(",", ":"), sort_keys=True)
        with self._locked():
            fd = os.open(self.usage_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (line + "\n").encode("utf-8"))
            finally:
                os.close(fd)


# --- upstream call -------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the Authorization header must not leave the configured host."""

    def redirect_request(self, *args, **kwargs):
        return None


class UpstreamError(Exception):
    def __init__(self, category, http_status=None, retry_after=None, error_type=""):
        super().__init__(category)
        self.category, self.http_status = category, http_status
        self.retry_after, self.error_type = retry_after, error_type


def _clean_token(value):
    value = value if isinstance(value, str) else ""
    return value if re.match(r"^[A-Za-z0-9_.-]{1,64}$", value) else ""


def post_bounded(opener, cloud, req, timeout, limit, no_response_seconds=None):
    """Send req and return the response body (at most `limit` bytes, within `timeout` overall).

    With no_response_seconds, the request goes through `cloud` (a fallback.HeaderDeadlineOpener)
    and raises fallback.NoResponse when no headers arrive in time; that is the only fallback
    trigger. Every other failure raises UpstreamError, including any received HTTP status
    (classified by status) and any failure after the headers arrived.
    """
    deadline = time.monotonic() + timeout
    if no_response_seconds is None and cloud is not None and netsim.blocked(cloud.service):
        raise UpstreamError("network_error")  # offline test with the fallback turned off
    try:
        if no_response_seconds is None:
            resp = opener.open(req, timeout=timeout)
        else:
            resp = cloud.open(req, min(no_response_seconds, timeout), timeout)
        with resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > limit:
                raise UpstreamError("bad_response", resp.status)
            chunks, total = [], 0
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UpstreamError("bad_response", resp.status)
                if time.monotonic() > deadline:
                    raise UpstreamError("timeout")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        raise Assistant._classify_http_error(exc)
    except (UpstreamError, fb.NoResponse):
        raise
    except (TimeoutError, OSError) as exc:
        if isinstance(exc, TimeoutError) or "timed out" in str(exc):
            raise UpstreamError("timeout")
        raise UpstreamError("network_error")
    except Exception:
        raise UpstreamError("network_error")
    return b"".join(chunks)


class Assistant:
    def __init__(self, cfg, tz, base_dir, clock=now_ms):
        self.cfg = cfg
        self.clock = clock
        self.env_path = os.path.join(base_dir, cfg["env_file"])
        self.store = UsageStore(os.path.join(base_dir, cfg["state_file"]),
                                os.path.join(base_dir, cfg["usage_log"]), tz, clock)
        self._inflight = threading.Lock()
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._cloud = fb.HeaderDeadlineOpener(self._opener, "ask")
        self.gate = fb.CloudGate(cfg["cloud_cooldown_seconds"])
        self._warned = set()

    @property
    def limits(self):
        c = self.cfg
        return {"min_interval_seconds": c["min_interval_seconds"],
                "max_per_hour": c["max_per_hour"], "max_per_day": c["max_per_day"]}

    def apply_settings(self, values):
        """Live settings change from the admin page (already validated with the full config)."""
        self.cfg.update(values)
        self.gate.cooldown = self.cfg["cloud_cooldown_seconds"]

    # -- status ---------------------------------------------------------------------------
    def _key_problem(self):
        key, problem = load_api_key(self.env_path)
        if problem and problem not in self._warned:
            self._warned.add(problem)
            log.warning("assistant: API key not usable (%s); see README", problem)
        return key, problem

    def status(self):
        key, problem = load_api_key(self.env_path)
        out = {"enabled": self.cfg["enabled"], "configured": key is not None,
               "setup_problem": problem, "model": self.cfg["model"],
               "max_question_chars": self.cfg["max_question_chars"],
               "min_interval_seconds": self.cfg["min_interval_seconds"],
               "limits": {"per_hour": self.cfg["max_per_hour"], "per_day": self.cfg["max_per_day"]},
               "fallback": {"enabled": self.cfg["fallback_enabled"], "cloud_cooling_down": self.gate.cooling_down()}}
        try:
            out.update(self.store.snapshot(self.limits))
        except (OSError, ValueError, TypeError):
            out.update({"remaining": None, "blocked": False, "blocked_retry_after": 0})
        return out

    # -- ask ------------------------------------------------------------------------------
    @staticmethod
    def _err(status, code, message, retry_after=None):
        error = {"code": code, "message": message}
        if retry_after is not None:
            error["retry_after"] = int(retry_after)
        return status, {"ok": False, "error": error}

    def ask(self, question):
        """Return (http_status, json_body). Never raises."""
        if not self.cfg["enabled"]:
            return self._err(503, "disabled", "The assistant is turned off on the server.")
        key, problem = self._key_problem()
        if key is None:
            return self._err(503, "not_configured",
                             "The assistant isn't set up yet. The API key must be added on the server (see README).")
        if not self._inflight.acquire(False):
            return self._err(429, "busy", "Another question is still being answered. Try again in a few seconds.", 5)
        started = self.clock()
        try:
            try:
                allowed, info = self.store.reserve(self.limits)
            except (OSError, ValueError, TypeError):
                return self._err(503, "unavailable", "The assistant's usage counters are unavailable, so it is paused.")
            if not allowed:
                return self._err(info["status"], info["code"], info["message"], info["retry_after"])
            remaining = info["remaining"]
            entry = {"ts_ms": started, "model": self.cfg["model"], "source": "api"}
            if netsim.blocked("ask"):
                entry["simulated"] = True
            use_fallback = self.cfg["fallback_enabled"]
            skipped = use_fallback and not self.gate.begin()
            if skipped:
                return self._answer_locally(question, entry, started, remaining, skipped=True)
            responded = True
            t0 = time.monotonic()
            try:
                result = self._call_upstream(key, question)
            except fb.NoResponse:
                responded = False
                entry["api_wait_ms"] = int((time.monotonic() - t0) * 1000)
                return self._answer_locally(question, entry, started, remaining, skipped=False)
            except UpstreamError as exc:
                entry.update({"outcome": exc.category, "http_status": exc.http_status,
                              "duration_ms": self.clock() - started})
                self._log_outcome(entry)
                return self._map_upstream_error(exc)
            except Exception:  # defensive: anything unexpected becomes a safe generic error
                log.error("assistant: unexpected failure (details withheld)")
                entry.update({"outcome": "internal_error", "duration_ms": self.clock() - started})
                self._log_outcome(entry)
                return self._err(500, "internal_error", "Something went wrong answering that.")
            finally:
                if use_fallback:
                    self.gate.finish(responded)
            entry.update({"outcome": "success", "http_status": 200, "duration_ms": self.clock() - started})
            entry.update(self._usage_fields(result["usage"]))
            self._log_outcome(entry)
            return 200, {"ok": True, "answer": result["answer"], "truncated": result["truncated"],
                         "remaining": remaining, "source": "api"}
        finally:
            self._inflight.release()

    def _answer_locally(self, question, entry, started, remaining, skipped):
        """Answer with the local model after a cloud no-response (or during the cooldown)."""
        entry.update({"source": "local", "model": self.cfg["fallback_model"],
                      "cloud": "skipped" if skipped else "no_response"})
        t0 = time.monotonic()
        deadline = t0 + self.cfg["fallback_timeout_seconds"]
        result, failure = None, None
        if not fb.acquire_local(deadline):
            failure = "local_busy"
        else:
            try:
                result = self._call_local(question, deadline)
            except UpstreamError as exc:
                failure = exc.category
            except Exception:  # defensive, as for the cloud call
                log.error("assistant: unexpected local fallback failure (details withheld)")
                failure = "internal_error"
            finally:
                fb.LOCAL_LOCK.release()
        entry.update({"local_ms": int((time.monotonic() - t0) * 1000), "duration_ms": self.clock() - started})
        if failure is not None:
            entry.update({"outcome": "local_error", "local_error": failure})
            self._log_outcome(entry)
            if skipped:
                return self._err(503, "local_unavailable",
                                 "Using the local fallback (the AI service was unreachable a moment ago), "
                                 "but it is unavailable right now. Try again shortly.")
            return self._err(503, "local_unavailable",
                             "The AI service didn't respond and the local fallback is unavailable.")
        entry.update({"outcome": "success_local", "http_status": 200})
        entry.update(self._usage_fields(result["usage"]))
        entry.update({"reported_cost_usd": None, "estimated_cost_usd": None})
        self._log_outcome(entry)
        return 200, {"ok": True, "answer": result["answer"], "truncated": result["truncated"],
                     "remaining": remaining, "source": "local"}

    def _log_outcome(self, entry):
        try:
            self.store.record(entry)
        except OSError:
            log.error("assistant: could not write usage log")

    def _usage_fields(self, usage):
        usage = usage if isinstance(usage, dict) else {}
        pt, ct, tt = (usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
        pt, ct, tt = (v if isinstance(v, int) and not isinstance(v, bool) else None for v in (pt, ct, tt))
        fields = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt}
        reported = usage.get("cost")
        fields["reported_cost_usd"] = reported if _num(reported) else None
        pin, pout = self.cfg["price_per_million_input_usd"], self.cfg["price_per_million_output_usd"]
        if pin is not None and pout is not None and pt is not None and ct is not None:
            fields["estimated_cost_usd"] = round((pt * pin + ct * pout) / 1e6, 8)
        else:
            fields["estimated_cost_usd"] = None
        return fields

    def _map_upstream_error(self, exc):
        cat = exc.category
        if cat == "quota_exhausted":
            self.store.block(self.cfg["quota_block_minutes"], "upstream_" + str(exc.http_status))
            wait = self.cfg["quota_block_minutes"] * 60
            return self._err(503, "quota_exhausted",
                             "The assistant is unavailable: the AI credit allowance is used up or the account "
                             "has no access. No further requests will be sent for about %s." % _fmt_wait(wait), wait)
        if cat == "access_denied":
            self.store.block(self.cfg["quota_block_minutes"], "upstream_403")
            return self._err(503, "unavailable",
                             "The AI service refused the request (account or model access). "
                             "No further requests will be sent for a while.",
                             self.cfg["quota_block_minutes"] * 60)
        if cat == "auth_error":
            return self._err(503, "key_rejected", "The AI service rejected the server's API key. "
                                                  "Replace the key on the server (see README).")
        if cat == "provider_rate_limited":
            wait = exc.retry_after or 30
            return self._err(429, "provider_busy", "The AI service is busy. Try again in about %s." % _fmt_wait(wait), wait)
        if cat == "timeout":
            return self._err(504, "timeout", "The AI service took too long to answer. Try again.")
        if cat == "network_error":
            return self._err(502, "network_error", "Couldn't reach the AI service from the server.")
        if cat == "empty_answer":
            return self._err(502, "empty_answer", "The AI service returned no answer. Try rephrasing.")
        return self._err(502, "upstream_error", "The AI service returned an error. Try again later.")

    def _build_payload(self, question):
        payload = {
            "model": self.cfg["model"],
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": question}],
            "max_tokens": self.cfg["max_output_tokens"],
            "stream": False,
        }
        if self.cfg["disable_thinking"]:
            # Documented AI Gateway control; for Gemini 2.5 models "none" maps to thinkingBudget 0.
            payload["reasoning"] = {"effort": "none"}
        return payload

    def _call_upstream(self, key, question):
        body = json.dumps(self._build_payload(question)).encode("utf-8")
        req = urllib.request.Request(
            self.cfg["base_url"].rstrip("/") + "/chat/completions", data=body, method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "Accept": "application/json", "User-Agent": "TabletDashboard-assistant"})
        no_response = self.cfg["no_response_seconds"] if self.cfg["fallback_enabled"] else None
        return self._parse_success(post_bounded(self._opener, self._cloud, req, self.cfg["timeout_seconds"],
                                                self.cfg["max_response_bytes"], no_response))

    def _call_local(self, question, deadline):
        payload = {
            "model": self.cfg["fallback_model"],
            "messages": [{"role": "system", "content": LOCAL_SYSTEM_PROMPT},
                         {"role": "user", "content": question}],
            "max_tokens": self.cfg["fallback_max_output_tokens"],
            "stream": False,
            "temperature": LOCAL_TEMPERATURE,
            "chat_template_kwargs": {"enable_thinking": False},  # Qwen: answer directly
        }
        req = urllib.request.Request(
            self.cfg["fallback_base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": "TabletDashboard-assistant"})
        timeout = max(0.1, deadline - time.monotonic())
        result = self._parse_success(post_bounded(self._opener, None, req, timeout, self.cfg["max_response_bytes"]))
        result["answer"] = fb.plain_text(result["answer"])
        if not result["answer"]:
            raise UpstreamError("empty_answer", 200)
        return result

    @staticmethod
    def _classify_http_error(exc):
        status = exc.code
        error_type = ""
        try:
            data = json.loads(exc.read(4096).decode("utf-8", "replace"))
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                error_type = _clean_token(err.get("type")) or _clean_token(err.get("code"))
        except Exception:
            pass
        retry_after = None
        header = exc.headers.get("retry-after") if exc.headers else None
        if header and header.strip().isdigit():
            retry_after = min(max(int(header.strip()), 1), 3600)
        if status == 401:
            return UpstreamError("auth_error", status, error_type=error_type)
        if status == 402:
            return UpstreamError("quota_exhausted", status, error_type=error_type)
        if status == 403:
            return UpstreamError("access_denied", status, error_type=error_type)
        if status == 429:
            return UpstreamError("provider_rate_limited", status, retry_after, error_type)
        if status >= 500:
            return UpstreamError("upstream_5xx", status, error_type=error_type)
        return UpstreamError("upstream_rejected", status, error_type=error_type)

    @staticmethod
    def _parse_success(raw):
        try:
            data = json.loads(raw.decode("utf-8"))
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (UnicodeDecodeError, ValueError, KeyError, IndexError, TypeError):
            raise UpstreamError("bad_response", 200)
        if not isinstance(text, str):
            raise UpstreamError("bad_response", 200)
        text = text.strip()
        if not text:
            raise UpstreamError("empty_answer", 200)
        truncated = choice.get("finish_reason") == "length"
        if len(text) > MAX_ANSWER_CHARS:
            text, truncated = text[:MAX_ANSWER_CHARS], True
        return {"answer": text, "truncated": truncated, "usage": data.get("usage")}


def _cli(argv):
    """Maintenance commands: clear-block (used by scripts/set-assistant-key.sh)."""
    if len(argv) == 2 and argv[1] == "clear-block":
        import server  # local import: config loading lives there
        cfg = server.load_config(os.path.join(server.BASE_DIR, "config.json"))
        a = Assistant(cfg["assistant"], ZoneInfo(cfg["timezone"]), server.BASE_DIR)
        a.store.clear_block()
        print("assistant block cleared")
        return 0
    print("usage: assistant.py clear-block", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
