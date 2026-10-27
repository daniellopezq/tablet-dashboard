"""Global usage limits for the cloud services (Ask, Voice): min interval, per-hour and per-day counts.

Counters persist in a small JSON store outside the web directory, protected by a thread lock and an flock,
so concurrent requests and restarts cannot bypass them. Python 3 standard library only.
"""
import contextlib
import fcntl
import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timedelta

from util import now_ms

log = logging.getLogger("dashboard.assistant")

HOUR_MS = 3600 * 1000
KEEP_MS = 50 * HOUR_MS


def fmt_wait(seconds):
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
                                          % fmt_wait(wait)}
            c = self._counts(state, now, limits)
            denials = []
            attempts = state["attempts"]
            interval = limits["min_interval_seconds"] * 1000
            if attempts and now - attempts[-1] < interval:
                wait = (attempts[-1] + interval - now) / 1000.0
                denials.append((wait, "rate_limited", "Please wait %s before asking again." % fmt_wait(wait)))
            if c["hour_used"] >= limits["max_per_hour"]:
                hour = c["hour_list"]
                release = hour[len(hour) - limits["max_per_hour"]] + HOUR_MS
                wait = (release - now) / 1000.0
                denials.append((wait, "hourly_limit", "Hourly limit of %d %s reached. Try again in about %s."
                                % (limits["max_per_hour"], limits.get("noun", "questions"), fmt_wait(wait))))
            if c["day_used"] >= limits["max_per_day"]:
                wait = (c["day_end"] - now) / 1000.0
                denials.append((wait, "daily_limit", "Daily limit of %d %s reached. It resets at midnight "
                                "(%s time), in about %s."
                                % (limits["max_per_day"], limits.get("noun", "questions"), self.tz.key, fmt_wait(wait))))
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
