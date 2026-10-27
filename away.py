"""Away mode: while it is on, the house looks lived in after dark.

Each night, once Home Assistant's sun.sun goes below the horizon, a plan is drawn at random:
  room lights   one at a time, each for room_minutes (40-60 by default), handing over to the next with a
                short gap or overlap (gap_minutes may be negative: the next room lights up before the last
                one goes dark, like someone walking between rooms). With overlap_chance, the next room
                lights up well before the last one goes dark (overlap_minutes), like two people at home
  short lights  bathroom-style visits of short_minutes every short_every minutes, plus one just before bed
  bedtime       a random time between bedtime_from and bedtime_to, when the last room light goes off
Every night's plan is different. Turning away mode off turns off whatever it turned on. The on/off choice
and the lights it has on survive a restart (state_file); the night's plan is redrawn after a restart.

The Home tab shows it as a button of kind "away" (home.py), so the voice assistant can switch it too.

Python 3 standard library only.
"""
import json
import logging
import os
import random
import re
import threading
import time

from util import num as _num

log = logging.getLogger("dashboard.away")

DEFAULT_AWAY_CONFIG = {
    "enabled": True,
    "lights": [],              # [{"entity_id": "light.x", "label": "...", "role": "room" | "short"}]
    "start_delay_minutes": [5, 25],
    "room_minutes": [40, 60],
    "gap_minutes": [-5, 12],
    "overlap_chance": 0.35,
    "overlap_minutes": [10, 30],
    "short_minutes": [3, 12],
    "short_every_minutes": [35, 80],
    "bedtime_from": "23:00",
    "bedtime_to": "24:00",
    "retry_minutes": 20,
    "tick_seconds": 20,
    "state_file": "data/away.json",
}
ENTITY_RE = re.compile(r"^(light|switch)\.[a-z0-9_]{1,128}$")
HHMM_RE = re.compile(r"^([01][0-9]|2[0-4]):([0-5][0-9])$")
MAX_LIGHTS = 12



def _minutes(hhmm):
    m = HHMM_RE.match(hhmm)
    return int(m.group(1)) * 60 + int(m.group(2))


def validate_away_config(a):
    if not isinstance(a["enabled"], bool):
        raise ValueError("away.enabled must be true or false")
    lights = a["lights"]
    if not isinstance(lights, list) or len(lights) > MAX_LIGHTS:
        raise ValueError("away.lights must be a list of at most %d lights" % MAX_LIGHTS)
    seen = set()
    for l in lights:
        if not isinstance(l, dict) or not isinstance(l.get("entity_id"), str) or not ENTITY_RE.match(l["entity_id"]):
            raise ValueError("away.lights: entity_id must look like light.bedroom or switch.lamp")
        if l["entity_id"] in seen:
            raise ValueError("away.lights: %s is listed twice" % l["entity_id"])
        seen.add(l["entity_id"])
        if not isinstance(l.get("label"), str) or not 1 <= len(l["label"]) <= 40:
            raise ValueError("away.lights %s: label must be 1-40 characters" % l["entity_id"])
        if l.get("role") not in ("room", "short"):
            raise ValueError("away.lights %s: role must be room or short" % l["entity_id"])
    if lights and not any(l["role"] == "room" for l in lights):
        raise ValueError("away.lights: at least one light needs role room")
    for key, lo, hi in (("start_delay_minutes", 0, 120), ("room_minutes", 5, 240), ("gap_minutes", -30, 120),
                        ("short_minutes", 1, 60), ("short_every_minutes", 5, 600), ("overlap_minutes", 1, 120)):
        v = a[key]
        if not (isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v) and lo <= v[0] <= v[1] <= hi):
            raise ValueError("away.%s must be [low, high] within %s-%s" % (key, lo, hi))
    if not _num(a["overlap_chance"]) or not 0 <= a["overlap_chance"] <= 1:
        raise ValueError("away.overlap_chance must be a number 0-1")
    for key in ("bedtime_from", "bedtime_to"):
        if not isinstance(a[key], str) or not HHMM_RE.match(a[key]) or _minutes(a[key]) > 1440:
            raise ValueError("away.%s must be HH:MM (00:00-24:00)" % key)
    if not 12 * 60 <= _minutes(a["bedtime_from"]) <= _minutes(a["bedtime_to"]):
        raise ValueError("away: bedtime_from must be after noon and not after bedtime_to")
    for key, lo, hi in (("retry_minutes", 1, 120), ("tick_seconds", 5, 300)):
        if not _num(a[key]) or not lo <= a[key] <= hi:
            raise ValueError("away.%s must be a number %s-%s" % (key, lo, hi))
    if not isinstance(a["state_file"], str) or not a["state_file"]:
        raise ValueError("away.state_file must be a path")


def _uniform(rng, pair):
    return rng.uniform(pair[0], pair[1]) * 60


def plan_night(cfg, start, bedtime, rng):
    """Sorted [(time, entity_id, "on"|"off")] between start and bedtime (epoch seconds)."""
    rooms = [l["entity_id"] for l in cfg["lights"] if l["role"] == "room"]
    shorts = [l["entity_id"] for l in cfg["lights"] if l["role"] == "short"]
    spans = []   # (entity, on, off)
    t, last = start, None
    free = {}    # room light -> when its last span ends
    while rooms and t < bedtime - 600:
        pick = rng.choice([e for e in rooms if e != last] or rooms)
        t = max(t, free.get(pick, t - 1) + 60)   # never on twice at once
        if t >= bedtime - 600:
            break
        end = min(t + _uniform(rng, cfg["room_minutes"]), bedtime)
        spans.append((pick, t, end))
        free[pick] = end
        if len(rooms) > 1 and rng.random() < cfg["overlap_chance"]:
            gap = -_uniform(rng, cfg["overlap_minutes"])
        else:
            gap = _uniform(rng, cfg["gap_minutes"])
        t, last = max(end + gap, t + 300), pick
    if shorts:
        visits = []
        last_on = bedtime - rng.uniform(8, 15) * 60   # brushing teeth
        visits.append((rng.choice(shorts), last_on, last_on + rng.uniform(3, 6) * 60))
        t = start + _uniform(rng, cfg["short_every_minutes"]) / 2
        while t < last_on - 600:
            e = rng.choice(shorts)
            visits.append((e, t, t + _uniform(rng, cfg["short_minutes"])))
            t += _uniform(rng, cfg["short_every_minutes"])
        for e, on, off in visits:
            if not any(s[0] == e and on < s[2] + 60 and s[1] < off + 60 for s in spans):
                spans.append((e, on, off))
    events = []
    for e, on, off in spans:
        events += [(on, e, "on"), (off, e, "off")]
    return sorted(events)


class AwayMode:
    def __init__(self, cfg, home, base_dir, clock=time.time, rng=None, localtime=time.localtime):
        self.cfg = cfg
        self.home = home
        self.path = os.path.join(base_dir, cfg["state_file"])
        self.clock = clock
        self.rng = rng or random.Random()
        self.localtime = localtime
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self.on = False
        self.since = None
        self.lit = []            # entities this mode turned on and hasn't turned off yet
        self.plan = None         # {"night": "YYYY-MM-DD", "start", "bedtime", "events": [[t, e, want, done]]}
        self.error = None
        self._load()

    # --- state file ---------------------------------------------------------------------

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            self.on = d.get("on") is True
            self.since = d.get("since") if _num(d.get("since")) else None
            known = {l["entity_id"] for l in self.cfg["lights"]}
            self.lit = [e for e in d.get("lit", []) if e in known]

    def _save(self):
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"on": self.on, "since": self.since, "lit": self.lit}, f)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("away: could not save %s: %s", self.path, exc)

    # --- control ------------------------------------------------------------------------

    def start(self):
        if self._thread is None and self.cfg["enabled"]:
            self._thread = threading.Thread(target=self._loop, name="away-mode", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def set(self, on):
        """Turn away mode on or off; returns True if that changed anything. Off also turns off its lights."""
        with self._lock:
            if on == self.on:
                return False
            self.on = on
            self.since = self.clock() if on else None
            self.plan = None
            self.error = None
            log.info("away: mode %s", "on" if on else "off")
            if not on:
                self._all_off()
            self._save()
        self._wake.set()
        return True

    def state(self):
        return "on" if self.on else "off"

    def status(self):
        with self._lock:
            out = {"enabled": self.cfg["enabled"], "on": self.on, "since": self.since, "error": self.error,
                   "lit": [self._label(e) for e in self.lit], "tonight": None}
            p = self.plan
            if p:
                pending = [ev for ev in p["events"] if not ev[3]]
                out["tonight"] = {"start": p["start"], "bedtime": p["bedtime"], "changes": len(p["events"]),
                                  "next": {"at": pending[0][0], "label": self._label(pending[0][1]),
                                           "to": pending[0][2]} if pending else None}
            return out

    def hint(self):
        """One short line for the Home tab tile."""
        st = self.status()
        if not st["on"]:
            return ""
        if st["error"]:
            return st["error"]
        t = st["tonight"]
        if t and t["next"]:
            return "Next: %s %s at %s" % (t["next"]["label"], t["next"]["to"], self._hhmm(t["next"]["at"]))
        if t:
            return "Done for tonight"
        return "Waits for sunset"

    # --- loop ---------------------------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("away: tick failed")
            self._wake.wait(self.cfg["tick_seconds"])
            self._wake.clear()

    def tick(self):
        with self._lock:
            if not self.on:
                if self.lit:
                    self._all_off()        # turning off failed earlier (HA down); keep trying
                return
            if not self.cfg["lights"]:
                return
            now = self.clock()
            lt = self.localtime(now)
            night = time.strftime("%Y-%m-%d", lt)
            if self.plan is None or self.plan["night"] != night:
                if self.plan is not None and self.lit:
                    self._all_off()        # last night's plan never finished (say, HA was down at bedtime)
                self.plan = None
                if lt.tm_hour >= 12 and self._dark():
                    self._make_plan(now, lt, night)
            if self.plan:
                self._run_due(now)

    def _dark(self):
        rows = self.home.ha_rows()
        if rows is None:
            self.error = "Home Assistant unreachable"
            return False
        self.error = None
        return (rows.get("sun.sun") or {}).get("state") == "below_horizon"

    def _make_plan(self, now, lt, night):
        midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        lo, hi = _minutes(self.cfg["bedtime_from"]), _minutes(self.cfg["bedtime_to"])
        bedtime = midnight + self.rng.uniform(lo, hi) * 60
        start = now + _uniform(self.rng, self.cfg["start_delay_minutes"])
        events = plan_night(self.cfg, start, bedtime, self.rng) if start < bedtime - 600 else []
        self.plan = {"night": night, "start": start, "bedtime": bedtime,
                     "events": [[t, e, want, False] for t, e, want in events]}
        log.info("away: plan for %s: %d changes from %s to %s", night, len(events),
                 self._hhmm(start), self._hhmm(bedtime))

    def _run_due(self, now):
        for ev in self.plan["events"]:
            t, entity, want, done = ev
            if done or t > now:
                continue
            if want == "on" and now - t > self.cfg["retry_minutes"] * 60:
                ev[3] = True        # too late for this one; its "off" will still run
                continue
            if want == "off" and entity not in self.lit:
                ev[3] = True
                continue
            if self._switch(entity, want):
                ev[3] = True

    def _switch(self, entity, want):
        domain = entity.split(".", 1)[0]
        try:
            self.home.ha_service("%s.turn_%s" % (domain, want), entity)
        except Exception as exc:     # ActionError, or anything odd from HA: try again next tick
            self.error = "%s: %s" % (self._label(entity), exc)
            log.warning("away: %s %s failed: %s", entity, want, exc)
            return False
        self.error = None
        log.info("away: %s %s", entity, want)
        if want == "on" and entity not in self.lit:
            self.lit.append(entity)
        elif want == "off" and entity in self.lit:
            self.lit.remove(entity)
        self._save()
        return True

    def _all_off(self):
        for entity in list(self.lit):
            self._switch(entity, "off")

    def _label(self, entity):
        return next((l["label"] for l in self.cfg["lights"] if l["entity_id"] == entity), entity)

    def _hhmm(self, t):
        return time.strftime("%H:%M", self.localtime(t))
