"""Overview, health checks, logs, usage, and maintenance (backup, restart)."""
import collections
import email.utils
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timedelta

import gcal as gcal_mod
import health as health_mod
import localai as localai_mod
import netsim

from .core import _age, ApiError, BACKUP_EXCLUDE, log, LOG_FILES, _tail


class OpsMixin:
    """Mixed into AdminService (admin/service.py)."""

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
