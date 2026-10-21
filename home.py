"""Home tab: buttons for home automations, plus the Plex <-> Jellyfin switch.

Two kinds of button, listed under "home" -> "buttons" in config.json:
  media_switch  optional, advanced: swap the active media server. Runs your own root-owned
                /usr/local/sbin/media-switch through `sudo -n` (needs a sudoers rule; see README); only one of Plex/Jellyfin runs at a
                time, and the stopped one is also disabled so the choice survives a reboot.
  ha            call a Home Assistant service, e.g. script.turn_on for script.movie_night. The
                automations themselves live in HA; this only presses the button. The HA token is
                read from .env (HA_TOKEN, or the name in home.ha_token_name), never from config.json.

Both work without the internet (LAN only), which is when Jellyfin is wanted.

Python 3 standard library only.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

import assistant as assistant_mod

log = logging.getLogger("dashboard.home")

MEDIA = {"plex": "plexmediaserver", "jellyfin": "jellyfin"}
MEDIA_LABEL = {"plex": "Plex", "jellyfin": "Jellyfin"}
SWITCH_CMD = "/usr/local/sbin/media-switch"
DEFAULT_HOME_CONFIG = {
    "enabled": True,
    "ha_url": "http://127.0.0.1:8123",
    "ha_token_name": "HA_TOKEN",
    "timeout_seconds": 5,
    "switch_timeout_seconds": 90,
    "min_interval_seconds": 2,
    "max_per_hour": 120,
    "env_file": ".env",
    "buttons": [
        {"id": "media", "label": "Media server", "kind": "media_switch", "confirm": True},
    ],
}
ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]{1,128}$")
SERVICE_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]{1,64}$")
ICON_RE = re.compile(r"^[a-z0-9-]{1,32}$")
KINDS = ("media_switch", "ha")
# States that count as "on" when a button has on_service/off_service (e.g. a Blink alarm panel).
ON_STATES = ("on", "armed_away", "armed_home", "armed_night", "armed_vacation", "armed_custom_bypass",
             "arming", "triggered", "open", "playing")
MAX_BUTTONS = 24
STATUS_TTL = 5


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_button(b):
    if not isinstance(b, dict):
        raise ValueError("home.buttons entries must be objects")
    bid = b.get("id")
    if not isinstance(bid, str) or not ID_RE.match(bid):
        raise ValueError("home button id must be 1-32 of a-z 0-9 _ - (got %r)" % (bid,))
    label = b.get("label")
    if not isinstance(label, str) or not 1 <= len(label) <= 40:
        raise ValueError("home button %r: label must be 1-40 characters" % bid)
    if b.get("kind") not in KINDS:
        raise ValueError("home button %r: kind must be media_switch or ha" % bid)
    if not isinstance(b.get("confirm", False), bool):
        raise ValueError("home button %r: confirm must be true or false" % bid)
    if "icon" in b and not (isinstance(b["icon"], str) and ICON_RE.match(b["icon"])):
        raise ValueError("home button %r: icon must be a short name like 'light'" % bid)
    if b["kind"] == "ha":
        pair = "on_service" in b or "off_service" in b
        if pair:
            # Two services picked by the current state, e.g. alarm_arm_away / alarm_disarm.
            for key in ("on_service", "off_service"):
                if not isinstance(b.get(key), str) or not SERVICE_RE.match(b[key]):
                    raise ValueError("home button %r: %s must look like alarm_control_panel.alarm_disarm" % (bid, key))
            if "service" in b:
                raise ValueError("home button %r: use service, or on_service + off_service, not both" % bid)
            if not b.get("state_entity"):
                raise ValueError("home button %r: on_service/off_service need state_entity" % bid)
        elif not isinstance(b.get("service"), str) or not SERVICE_RE.match(b["service"]):
            raise ValueError("home button %r: service must look like script.turn_on" % bid)
        if not isinstance(b.get("entity_id"), str) or not ENTITY_RE.match(b["entity_id"]):
            raise ValueError("home button %r: entity_id must look like script.movie_night" % bid)
        se = b.get("state_entity")
        if se is not None and not (isinstance(se, str) and ENTITY_RE.match(se)):
            raise ValueError("home button %r: state_entity must look like switch.desk_lamp" % bid)


def validate_home_config(h):
    if not isinstance(h["enabled"], bool):
        raise ValueError("home.enabled must be true or false")
    url = h["ha_url"]
    if not isinstance(url, str) or not re.match(r"^https?://[A-Za-z0-9.\-\[\]:]+/?$", url):
        raise ValueError("home.ha_url must be like http://127.0.0.1:8123")
    if not isinstance(h["ha_token_name"], str) or not re.match(r"^[A-Z][A-Z0-9_]{0,63}$", h["ha_token_name"]):
        raise ValueError("home.ha_token_name must be an env variable name such as HA_TOKEN")
    for key, lo, hi in (("timeout_seconds", 1, 30), ("switch_timeout_seconds", 10, 300),
                        ("min_interval_seconds", 0, 60), ("max_per_hour", 1, 3600)):
        if not _num(h[key]) or not lo <= h[key] <= hi:
            raise ValueError("home.%s must be a number %s-%s" % (key, lo, hi))
    if not isinstance(h["env_file"], str) or not h["env_file"]:
        raise ValueError("home.env_file must be a path string")
    if not isinstance(h["buttons"], list) or len(h["buttons"]) > MAX_BUTTONS:
        raise ValueError("home.buttons must be a list of at most %d buttons" % MAX_BUTTONS)
    ids = set()
    for b in h["buttons"]:
        validate_button(b)
        if b["id"] in ids:
            raise ValueError("home button ids must be unique (%r)" % b["id"])
        ids.add(b["id"])
    if sum(1 for b in h["buttons"] if b["kind"] == "media_switch") > 1:
        raise ValueError("home.buttons: at most one media_switch button")


class ActionError(Exception):
    """A user-facing failure; the message is safe to show on the tablet."""

    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.code = code


class Home:
    def __init__(self, cfg, base_dir, runner=subprocess.run, opener=None, clock=time.time):
        self.cfg = cfg
        self.env_path = os.path.join(base_dir, cfg["env_file"])
        self.runner = runner
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.clock = clock
        self._switch_lock = threading.Lock()
        self._lock = threading.Lock()
        self._media_cache = (0.0, None)
        self._ha_cache = (0.0, None, False)  # (time, rows, valid)
        self._last_action = {}   # button id -> time of its last press (debounces a poor touchscreen)
        self._recent = []
        self._switching = None   # target name while a switch is running
        self._last_error = None  # (time, message) of the last failed background switch
        self._ha_error = None    # why the last HA states fetch failed, for the tablet
        self._thread = None

    # --- media switch -------------------------------------------------------------------

    def media_status(self, fresh=False):
        """{"plex": "active"|..., "jellyfin": ..., "current": "plex"|"jellyfin"|"both"|"none",
        "switching": name|None}. `systemctl is-active` needs no privileges."""
        now = self.clock()
        with self._lock:
            ts, cached = self._media_cache
            if cached is not None and not fresh and now - ts < STATUS_TTL:
                return dict(cached, switching=self._switching)
        out = {}
        try:
            r = self.runner(["systemctl", "is-active"] + [MEDIA[k] for k in ("plex", "jellyfin")],
                            capture_output=True, text=True, timeout=5)
            states = (r.stdout or "").split()
        except (OSError, subprocess.SubprocessError):
            states = []
        for i, name in enumerate(("plex", "jellyfin")):
            out[name] = states[i] if i < len(states) else "unknown"
        up = [n for n in ("plex", "jellyfin") if out[n] == "active"]
        out["current"] = up[0] if len(up) == 1 else ("both" if up else "none")
        with self._lock:
            self._media_cache = (now, out)
        return dict(out, switching=self._switching)

    def _media_extra(self, st):
        err = self._last_error
        st["error"] = err[1] if err and self.clock() - err[0] < 300 else None
        return st

    def intentionally_stopped(self):
        """Display names of the media server that is off on purpose (for the health page)."""
        st = self.media_status()
        if st["current"] in MEDIA:
            other = "jellyfin" if st["current"] == "plex" else "plex"
            return {MEDIA_LABEL[other]}
        return set()

    def _pick_target(self, target):
        if target is None:
            target = "jellyfin" if self.media_status(fresh=True)["current"] == "plex" else "plex"
        if target not in MEDIA:
            raise ActionError("unknown media server", 400)
        return target

    def media_switch(self, target=None):
        """Switch to `target`, or to the other server when target is None. Blocks until done."""
        target = self._pick_target(target)
        if not self._switch_lock.acquire(blocking=False):
            raise ActionError("a switch is already running", 409)
        self._switching = target
        return self._switch_locked(target)

    def start_media_switch(self, target=None):
        """Start a switch in the background (it can take a minute); poll media_status() for the end."""
        target = self._pick_target(target)
        if not self._switch_lock.acquire(blocking=False):
            raise ActionError("a switch is already running", 409)
        self._switching = target
        self._last_error = None

        def work():
            try:
                self._switch_locked(target)
            except ActionError as exc:
                self._last_error = (self.clock(), str(exc))
            except Exception:  # never leave the lock held
                log.exception("home: media switch crashed")
                self._last_error = (self.clock(), "the switch failed (see the server log)")

        self._thread = threading.Thread(target=work, name="media-switch", daemon=True)
        self._thread.start()
        return target

    def _switch_locked(self, target):
        """Runs with _switch_lock held and _switching set; releases both."""
        try:
            log.info("home: switching media server to %s", target)
            try:
                r = self.runner(["sudo", "-n", SWITCH_CMD, target], capture_output=True, text=True,
                                timeout=self.cfg["switch_timeout_seconds"])
            except subprocess.TimeoutExpired:
                raise ActionError("the switch is taking too long; check again in a minute", 504)
            except OSError as exc:
                raise ActionError("could not run the switch (%s)" % type(exc).__name__)
            if r.returncode != 0:
                # sudo's own failures (no rule, password required, script missing) start with "sudo:".
                if (r.stderr or "").lstrip().startswith("sudo:"):
                    raise ActionError("the media switch is not installed (see README)", 503)
                log.warning("home: media-switch exited %s: %s", r.returncode, (r.stderr or "")[:200])
                raise ActionError("the switch failed (exit %d)" % r.returncode)
        finally:
            self._switching = None
            self._switch_lock.release()
        st = self.media_status(fresh=True)
        if st["current"] != target:
            raise ActionError("%s did not start; see the server log" % MEDIA_LABEL[target])
        return st

    # --- Home Assistant -----------------------------------------------------------------

    def _token(self):
        key, problem = assistant_mod.load_api_key(self.env_path, (self.cfg["ha_token_name"],))
        return key, problem

    def _ha(self, method, path, body=None):
        token, problem = self._token()
        if not token:
            raise ActionError("Home Assistant is not connected (%s)" % problem.replace("_", " "), 503)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.cfg["ha_url"].rstrip("/") + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.cfg["timeout_seconds"]) as resp:
                return json.loads(resp.read(1048576) or b"null")
        except urllib.error.HTTPError as exc:
            exc.close()
            hint =" (check the HA token)" if exc.code == 401 else " (unknown entity or service)" if exc.code in (400, 404) else ""
            raise ActionError("Home Assistant said HTTP %d%s" % (exc.code, hint))
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ActionError("Home Assistant is unreachable (%s)" % str(getattr(reason, "strerror", None) or reason)[:80], 503)

    def ha_rows(self, fresh=False):
        """{entity_id: state row} for every HA entity, or None if HA is unavailable. One GET /api/states,
        cached for STATUS_TTL and shared by the buttons and the cameras."""
        now = self.clock()
        with self._lock:
            ts, cached, ok = self._ha_cache
            if ok and not fresh and now - ts < STATUS_TTL:
                return cached
        try:
            rows = self._ha("GET", "/api/states")
            out = {r.get("entity_id"): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
            self._ha_error = None
        except ActionError as exc:
            out = None
            self._ha_error = str(exc)
        with self._lock:
            self._ha_cache = (now, out, True)
        return out

    def ha_states(self, fresh=False):
        """{entity_id: state} for the entities the buttons show, or None if HA is unavailable."""
        wanted = {b["state_entity"] for b in self.cfg["buttons"] if b["kind"] == "ha" and b.get("state_entity")}
        if not wanted:
            return {}
        rows = self.ha_rows(fresh)
        if rows is None:
            return None
        return {ent: str((rows.get(ent) or {}).get("state", "missing"))[:32] for ent in wanted}

    def ha_problem(self):
        """Short reason for the tablet when HA can't be used."""
        _, problem = self._token()
        if problem == "not_configured":
            return "no Home Assistant token yet (admin page, API keys)"
        if problem:
            return "Home Assistant token problem (%s)" % problem.replace("_", " ")
        return self._ha_error or "Home Assistant unreachable"

    def ha_service(self, service, entity_id):
        domain, name = service.split(".", 1)
        self._ha("POST", "/api/services/%s/%s" % (domain, name), {"entity_id": entity_id})
        with self._lock:
            self._ha_cache = (0.0, None, False)

    def ha_image(self, entity_id, limit=4194304):
        """(bytes, content type) of a camera's current still via /api/camera_proxy, or raise ActionError."""
        token, problem = self._token()
        if not token:
            raise ActionError("Home Assistant is not connected (%s)" % problem.replace("_", " "), 503)
        req = urllib.request.Request(self.cfg["ha_url"].rstrip("/") + "/api/camera_proxy/" + entity_id,
                                     headers={"Authorization": "Bearer " + token})
        try:
            with self.opener.open(req, timeout=max(self.cfg["timeout_seconds"], 10)) as resp:
                ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
                data = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ActionError("Home Assistant said HTTP %d for the image" % exc.code)
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ActionError("Home Assistant is unreachable (%s)" % str(getattr(reason, "strerror", None) or reason)[:80], 503)
        if len(data) > limit or ctype not in ("image/jpeg", "image/png"):
            raise ActionError("unexpected camera image (%s, %d bytes)" % (ctype, len(data)))
        return data, ctype


    def test_connection(self):
        """For the admin page's key test: is HA reachable and does it accept the token?"""
        try:
            r = self._ha("GET", "/api/")
        except ActionError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "message": str((r or {}).get("message", "connected"))[:80]}

    def ha_call(self, button):
        """Run the button's service; with on_service/off_service, pick by the current state."""
        service = button.get("service")
        if not service:
            st = (self.ha_states(fresh=True) or {}).get(button["state_entity"])
            if st is None:
                raise ActionError("Home Assistant is unreachable", 503)
            service = button["off_service"] if st in ON_STATES else button["on_service"]
        self.ha_service(service, button["entity_id"])
        return service

    # --- tablet API ---------------------------------------------------------------------

    def status(self):
        buttons = self.cfg["buttons"]
        out = {"enabled": self.cfg["enabled"], "buttons": []}
        if not self.cfg["enabled"]:
            return out
        media = self.media_status() if any(b["kind"] == "media_switch" for b in buttons) else None
        states = self.ha_states() if any(b["kind"] == "ha" for b in buttons) else {}
        for b in buttons:
            item = {"id": b["id"], "label": b["label"], "kind": b["kind"], "confirm": b.get("confirm", False),
                    "icon": b.get("icon", "")}
            if b["kind"] == "media_switch":
                item["media"] = self._media_extra(dict(media))
            elif b.get("state_entity"):
                item["state"] = None if states is None else states.get(b["state_entity"])
            out["buttons"].append(item)
        out["ha_ok"] = states is not None
        out["ha_error"] = None if states is not None else self.ha_problem()
        return out

    def _rate_check(self, button_id):
        now = self.clock()
        with self._lock:
            if now - self._last_action.get(button_id, 0.0) < self.cfg["min_interval_seconds"]:
                raise ActionError("too fast; wait a moment", 429)
            self._recent = [t for t in self._recent if now - t < 3600]
            if len(self._recent) >= self.cfg["max_per_hour"]:
                raise ActionError("hourly limit reached", 429)
            self._last_action[button_id] = now
            self._recent.append(now)

    def action(self, button_id):
        if not self.cfg["enabled"]:
            raise ActionError("home controls are turned off", 404)
        button = next((b for b in self.cfg["buttons"] if b["id"] == button_id), None)
        if button is None:
            raise ActionError("unknown button", 404)
        self._rate_check(button_id)
        if button["kind"] == "media_switch":
            target = self.start_media_switch()
            log.info("home: %s -> switching to %s", button_id, target)
            return {"ok": True, "pending": True, "target": target}
        service = self.ha_call(button)
        log.info("home: %s -> %s %s", button_id, service, button["entity_id"])
        return {"ok": True}
