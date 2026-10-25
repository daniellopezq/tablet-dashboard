"""API keys page: list (masked), set, remove and test the keys in .env."""
import os
import re
import time

import assistant as assistant_mod
import envfile
import gcal as gcal_mod
import health as health_mod
import maps as maps_mod
import voice as voice_mod

from .core import ApiError, KEYS, KEYS_BY_NAME, log, _mask


class KeysMixin:
    """Mixed into AdminService (admin/service.py)."""

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
