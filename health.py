"""Health and uptime checks for the admin page.

A background thread runs every `interval_seconds`:
  - built-in checks supplied by the server (listeners, weather/maps/calendar freshness, API keys,
    Google sign-in age, local models, disk, TLS certificate, offline test switch);
  - TCP / HTTP checks of other services on the network, listed under "targets" in config.json;
  - optionally Uptime Kuma: reads the monitors of a status page (no key needed) or, with
    KUMA_API in .env, the /metrics endpoint (all monitors), and can send a heartbeat to a
    Kuma "Push" monitor (KUMA_PUSH_URL in .env) so Kuma alerts when the dashboard is down.

Results stay in memory with ~24 h of history per check. Python 3 standard library only.
"""
import base64
import collections
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import assistant as assistant_mod

log = logging.getLogger("dashboard.health")

DEFAULT_HEALTH_CONFIG = {
    "enabled": True,
    "interval_seconds": 60,
    "timeout_seconds": 3,
    "google_token_max_age_days": 7,  # Google "Testing" apps: refresh tokens die after 7 days; null when published
    "disk_warn_gb": 5,
    "targets": [],
}
DEFAULT_KUMA_CONFIG = {
    "enabled": False,
    "base_url": "",
    "status_page_slug": "",
    "use_metrics": False,     # needs KUMA_API in .env
    "push_enabled": False,    # needs KUMA_PUSH_URL in .env
    "timeout_seconds": 5,
    "max_response_bytes": 2097152,
    "env_file": ".env",
}
KUMA_KEY_ENV = "KUMA_API"
KUMA_KEY_NAMES = (KUMA_KEY_ENV, "KUMA_API_KEY")  # KUMA_API_KEY: older name, still accepted
KUMA_PUSH_ENV = "KUMA_PUSH_URL"
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATES = ("ok", "warn", "fail", "unknown")
HISTORY = 1440
KUMA_STATUS = {0: "down", 1: "up", 2: "pending", 3: "maintenance"}
PUBLIC_GROUPS = (("Uptime Kuma", "kuma"), ("Services", "service"))  # tablet Uptime view, in this order
PUBLIC_RECENT = 30
_MS_RE = re.compile(r"(\d+) ms")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _http_url(v):
    parts = urllib.parse.urlsplit(v) if isinstance(v, str) else None
    return bool(parts and parts.scheme in ("http", "https") and parts.hostname and not parts.username)


def validate_target(t):
    if not isinstance(t, dict):
        raise ValueError("each health target must be an object")
    name = t.get("name")
    if not isinstance(name, str) or not 0 < len(name) <= 40:
        raise ValueError("health target name must be 1-40 characters")
    if t.get("type") == "tcp":
        if set(t) - {"name", "type", "host", "port"}:
            raise ValueError("health target %r: tcp takes name, host, port" % name)
        if not isinstance(t.get("host"), str) or not HOST_RE.match(t["host"]):
            raise ValueError("health target %r: host must be a host name or IP" % name)
        port = t.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError("health target %r: port must be 1-65535" % name)
    elif t.get("type") == "http":
        if set(t) - {"name", "type", "url"}:
            raise ValueError("health target %r: http takes name, url" % name)
        if not _http_url(t.get("url")):
            raise ValueError("health target %r: url must be http(s) without a user name" % name)
    else:
        raise ValueError("health target %r: type must be tcp or http" % name)


def validate_health_config(h):
    if not isinstance(h["enabled"], bool):
        raise ValueError("health.enabled must be true or false")
    for key, lo, hi in (("interval_seconds", 15, 3600), ("timeout_seconds", 0.5, 30), ("disk_warn_gb", 0, 10000)):
        if not _num(h[key]) or not lo <= h[key] <= hi:
            raise ValueError("health.%s must be a number %s-%s" % (key, lo, hi))
    g = h["google_token_max_age_days"]
    if g is not None and (not _num(g) or not 1 <= g <= 365):
        raise ValueError("health.google_token_max_age_days must be null or 1-365")
    if not isinstance(h["targets"], list) or len(h["targets"]) > 40:
        raise ValueError("health.targets must be a list of at most 40 targets")
    names = set()
    for t in h["targets"]:
        validate_target(t)
        if t["name"] in names:
            raise ValueError("health target names must be unique (%r)" % t["name"])
        names.add(t["name"])


def validate_kuma_config(k):
    for key in ("enabled", "use_metrics", "push_enabled"):
        if not isinstance(k[key], bool):
            raise ValueError("kuma.%s must be true or false" % key)
    if k["base_url"] and not _http_url(k["base_url"]):
        raise ValueError("kuma.base_url must be an http(s) URL such as http://192.168.1.10:3001")
    if k["enabled"] and not k["base_url"]:
        raise ValueError("kuma.base_url is required when kuma.enabled is true")
    if k["status_page_slug"] and not SLUG_RE.match(k["status_page_slug"]):
        raise ValueError("kuma.status_page_slug must be the slug from the status page URL (/status/<slug>)")
    if k["enabled"] and not k["use_metrics"] and not k["status_page_slug"]:
        raise ValueError("kuma: set status_page_slug, or use_metrics with KUMA_API in .env")
    if not _num(k["timeout_seconds"]) or not 1 <= k["timeout_seconds"] <= 30:
        raise ValueError("kuma.timeout_seconds must be 1-30")
    if not isinstance(k["max_response_bytes"], int) or not 4096 <= k["max_response_bytes"] <= 16777216:
        raise ValueError("kuma.max_response_bytes must be an integer 4096-16777216")
    if not isinstance(k["env_file"], str) or not k["env_file"]:
        raise ValueError("kuma.env_file must be a path string")


# --- probes ---------------------------------------------------------------------------------

def tcp_check(host, port, timeout):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        return "fail", "%s" % (exc.strerror or type(exc).__name__)
    return "ok", "%d ms" % ((time.monotonic() - t0) * 1000)


_opener = urllib.request.build_opener(assistant_mod._NoRedirect)


def http_check(url, timeout):
    """Up = any HTTP status below 500 (a login page or 401 still means the server answers)."""
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "TabletDashboard-health"}, method="GET")
    try:
        with _opener.open(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return "fail", str(getattr(reason, "strerror", None) or reason)[:80]
    ms = (time.monotonic() - t0) * 1000
    return ("ok" if status < 500 else "fail"), "HTTP %d, %d ms" % (status, ms)


def _get(url, timeout, limit, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "TabletDashboard-health",
                                                    "Accept": "application/json, text/plain"}, **(headers or {})))
    with _opener.open(req, timeout=timeout) as resp:
        body = resp.read(limit + 1)
    if len(body) > limit:
        raise ValueError("response too large")
    return body


# --- Uptime Kuma ----------------------------------------------------------------------------

_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')
_METRIC_RE = re.compile(r"^(monitor_status|monitor_response_time)\{(.*)\}\s+(\S+)$")


def parse_kuma_metrics(text):
    """Monitors from Kuma's Prometheus /metrics text."""
    mons = collections.OrderedDict()
    for line in text.splitlines():
        m = _METRIC_RE.match(line.strip())
        if not m:
            continue
        labels = dict((k, v.replace('\\"', '"').replace("\\\\", "\\")) for k, v in _LABEL_RE.findall(m.group(2)))
        name = labels.get("monitor_name")
        if not name:
            continue
        mon = mons.setdefault(name, {"name": name, "type": labels.get("monitor_type", ""),
                                     "target": next((labels[k] for k in ("monitor_url", "monitor_hostname")
                                                     if labels.get(k) not in (None, "", "null", "https://")), ""),
                                     "status": "unknown", "ping_ms": None, "uptime_24h": None, "msg": ""})
        try:
            value = float(m.group(3))
        except ValueError:
            continue
        if m.group(1) == "monitor_status":
            mon["status"] = KUMA_STATUS.get(int(value), "unknown")
        elif value >= 0:
            mon["ping_ms"] = int(value)
    return list(mons.values())


def parse_kuma_status_page(page, beats):
    """Monitors from /api/status-page/<slug> and /api/status-page/heartbeat/<slug> JSON."""
    mons = []
    hb = beats.get("heartbeatList") if isinstance(beats, dict) else None
    up = beats.get("uptimeList") if isinstance(beats, dict) else None
    hb, up = (hb if isinstance(hb, dict) else {}), (up if isinstance(up, dict) else {})
    for group in page.get("publicGroupList") or []:
        if not isinstance(group, dict):
            continue
        for m in group.get("monitorList") or []:
            if not isinstance(m, dict) or "id" not in m:
                continue
            mid = str(m["id"])
            last = hb.get(mid)[-1] if isinstance(hb.get(mid), list) and hb.get(mid) else {}
            last = last if isinstance(last, dict) else {}
            uptime = up.get(mid + "_24")
            mons.append({"name": str(m.get("name", mid))[:80], "type": str(m.get("type", ""))[:20],
                         "group": str(group.get("name", ""))[:60], "target": "",
                         "status": KUMA_STATUS.get(last.get("status"), "unknown"),
                         "ping_ms": last.get("ping") if _num(last.get("ping")) else None,
                         "uptime_24h": round(uptime * 100, 2) if _num(uptime) else None,
                         "msg": str(last.get("msg") or "")[:120], "time": str(last.get("time") or "")[:30]})
    return mons


class KumaClient:
    def __init__(self, kcfg, base_dir):
        self.cfg = kcfg
        self.env_path = os.path.join(base_dir, kcfg["env_file"])

    def _secret(self, names):
        val, _ = assistant_mod.load_api_key(self.env_path, names)
        return val

    def _push_url(self):
        # A push URL holds a token and a query string, so it is read without the key-format check.
        try:
            import envfile
            val = os.environ.get(KUMA_PUSH_ENV) or envfile.read_values(self.env_path).get(KUMA_PUSH_ENV)
        except OSError:
            return None
        return val if val and _http_url(val) else None

    def fetch(self):
        c = self.cfg
        base = c["base_url"].rstrip("/")
        out = {"ok": False, "source": None, "monitors": [], "error": None, "checked_ms": int(time.time() * 1000)}
        try:
            key = self._secret(KUMA_KEY_NAMES) if c["use_metrics"] else None
            if c["use_metrics"] and not key:
                raise ValueError("use_metrics is on but KUMA_API is not set")
            if key:
                auth = base64.b64encode((":" + key).encode()).decode()
                text = _get(base + "/metrics", c["timeout_seconds"], c["max_response_bytes"],
                            {"Authorization": "Basic " + auth}).decode("utf-8", "replace")
                out.update(source="metrics", monitors=parse_kuma_metrics(text))
            else:
                slug = urllib.parse.quote(c["status_page_slug"])
                page = json.loads(_get(base + "/api/status-page/" + slug, c["timeout_seconds"], c["max_response_bytes"]))
                beats = json.loads(_get(base + "/api/status-page/heartbeat/" + slug, c["timeout_seconds"],
                                        c["max_response_bytes"]))
                if not isinstance(page, dict):
                    raise ValueError("unexpected status page response")
                out.update(source="status_page", monitors=parse_kuma_status_page(page, beats))
            out["ok"] = True
        except urllib.error.HTTPError as exc:
            out["error"] = "HTTP %d%s" % (exc.code, " (check KUMA_API)" if exc.code == 401 else
                                          " (check the status page slug)" if exc.code == 404 else "")
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            out["error"] = str(getattr(reason, "strerror", None) or reason)[:120]
        return out

    def push(self, ok_state, msg):
        url = self._push_url()
        if not url:
            return "KUMA_PUSH_URL is not set"
        parts = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        q.update(status="up" if ok_state else "down", msg=msg[:100], ping="")
        try:
            _get(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q))),
                 self.cfg["timeout_seconds"], 65536)
        except urllib.error.HTTPError as exc:
            return "HTTP %d" % exc.code
        except (OSError, ValueError) as exc:
            return type(exc).__name__
        return None


# --- checker --------------------------------------------------------------------------------

class HealthChecker:
    def __init__(self, hcfg, kcfg, base_dir, builtin):
        """builtin() -> list of {"id", "name", "group", "state", "detail"} computed by the server."""
        self.cfg = hcfg
        self.kcfg = kcfg
        self.builtin = builtin
        self.kuma = KumaClient(kcfg, base_dir)
        self._lock = threading.Lock()
        self._results = []
        self._kuma = None
        self._push_error = None
        self._history = {}      # id -> deque of state letters, one per run
        self._since = {}        # id -> (state, ms of last change)
        self._last_run_ms = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.stopped_hint = None  # () -> set of target names that are off on purpose (Home tab media switch)

    def _targets(self):
        out = []
        try:
            stopped = self.stopped_hint() if self.stopped_hint else set()
        except Exception:
            stopped = set()
        for t in self.cfg["targets"]:
            if t["name"] in stopped:
                continue  # e.g. Jellyfin while Plex is the active media server: not a fault
            if t["type"] == "tcp":
                state, detail = tcp_check(t["host"], t["port"], self.cfg["timeout_seconds"])
                where = "%s:%d" % (t["host"], t["port"])
            else:
                state, detail = http_check(t["url"], self.cfg["timeout_seconds"])
                where = t["url"]
            ms = _MS_RE.search(detail) if state == "ok" else None
            out.append({"id": "target:" + t["name"], "name": t["name"], "group": "Services",
                        "state": state, "detail": "%s · %s" % (where, detail),
                        "ping_ms": int(ms.group(1)) if ms else None})
        return out

    def run_once(self):
        try:
            results = list(self.builtin())
        except Exception:
            log.exception("health: built-in checks failed")
            results = [{"id": "internal", "name": "Built-in checks", "group": "Dashboard", "state": "fail",
                        "detail": "internal error (see log)"}]
        results += self._targets()
        kuma = None
        if self.kcfg["enabled"]:
            kuma = self.kuma.fetch()
            for m in kuma["monitors"]:
                results.append({"id": "kuma:" + m["name"], "name": m["name"], "group": "Uptime Kuma",
                                "ping_ms": m.get("ping_ms"),
                                "state": {"up": "ok", "down": "fail", "pending": "warn",
                                          "maintenance": "warn"}.get(m["status"], "unknown"),
                                "detail": " · ".join(x for x in (
                                    m.get("target"), "%d ms" % m["ping_ms"] if m.get("ping_ms") is not None else "",
                                    "%.2f%% 24 h" % m["uptime_24h"] if m.get("uptime_24h") is not None else "",
                                    m.get("msg")) if x)})
            if not kuma["ok"]:
                results.append({"id": "kuma", "name": "Uptime Kuma", "group": "Uptime Kuma", "state": "fail",
                                "detail": kuma["error"]})
        push_error = None
        if self.kcfg["enabled"] and self.kcfg["push_enabled"]:
            own = [r for r in results if r["group"] == "Dashboard"]
            bad = [r["name"] for r in own if r["state"] == "fail"]
            push_error = self.kuma.push(not bad, "problems: " + ", ".join(bad) if bad else "OK")
        now = int(time.time() * 1000)
        with self._lock:
            for r in results:
                h = self._history.setdefault(r["id"], collections.deque(maxlen=HISTORY))
                h.append(r["state"][0])
                prev = self._since.get(r["id"])
                if not prev or prev[0] != r["state"]:
                    if prev and r["group"] != "Uptime Kuma":
                        log.info("health: %s %s -> %s", r["name"], prev[0], r["state"])
                    self._since[r["id"]] = (r["state"], now)
                r["since_ms"] = self._since[r["id"]][1]
            self._results, self._kuma, self._push_error, self._last_run_ms = results, kuma, push_error, now
        return results

    def snapshot(self):
        with self._lock:
            results = [dict(r, history="".join(self._history.get(r["id"], ()))) for r in self._results]
            counts = collections.Counter(r["state"] for r in results)
            return {"enabled": self.cfg["enabled"], "interval_seconds": self.cfg["interval_seconds"],
                    "last_run_ms": self._last_run_ms, "checks": results,
                    "counts": {s: counts.get(s, 0) for s in STATES},
                    "kuma": {"enabled": self.kcfg["enabled"], "source": (self._kuma or {}).get("source"),
                             "error": (self._kuma or {}).get("error"), "push_enabled": self.kcfg["push_enabled"],
                             "push_error": self._push_error}}

    def public_snapshot(self):
        """Kuma monitors, then service targets, for the tablet's Uptime view. Problems sort first in each
        group. No details, hostnames or URLs: the tablet is a shared screen and this route needs no login."""
        rank = {"fail": 0, "warn": 1, "unknown": 2, "ok": 3}
        with self._lock:
            checks = []
            for group, key in PUBLIC_GROUPS:
                rows = [r for r in self._results if r["group"] == group]
                rows.sort(key=lambda r: rank.get(r["state"], 2))
                for r in rows:
                    h = "".join(self._history.get(r["id"], ()))
                    checks.append({"name": r["name"], "group": key, "state": r["state"],
                                   "ping_ms": r.get("ping_ms"), "since_ms": r.get("since_ms"),
                                   "uptime_pct": round(100.0 * h.count("o") / len(h), 1) if h else None,
                                   "history_minutes": len(h) * self.cfg["interval_seconds"] // 60,
                                   "recent": h[-PUBLIC_RECENT:]})
            counts = collections.Counter(c["state"] for c in checks)
            return {"enabled": self.cfg["enabled"], "last_run_ms": self._last_run_ms,
                    "interval_seconds": self.cfg["interval_seconds"], "checks": checks,
                    "counts": {st: counts.get(st, 0) for st in STATES},
                    "kuma_ok": None if not self.kcfg["enabled"] or self._kuma is None else bool(self._kuma["ok"])}

    def _run(self):
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(self.cfg["interval_seconds"])
            self._wake.clear()

    def wake(self):
        self._wake.set()

    def start(self):
        if not self.cfg["enabled"]:
            return
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

