"""Laundry check: when rain is coming, look at the clotheslines with the V380 and warn if laundry is out.

Runs after every weather update (weather.py, about every 15 min). Rain is "coming" when it rains now or
the hourly chance reaches rain_probability within rain_hours. Then, in daylight only (the night picture is
infrared grey), a background thread:
  1. drives the camera to each clothesline: every recipe starts at the right and top end stops, which are
     the only fixed points (the camera has no position feedback), then counts nudges
     (data/ptz-map/map.json, mapped 2026-10-25). Reach the stops blind ("right 28", more than the whole
     range; nudging against a stop does nothing): "right stop" detection (two unchanged frames) was fooled
     by the flat roof and the eave, which look the same from one nudge to the next,
  2. finds how far the picture is shifted from a reference picture of the empty line; nudges are not
     exact (one or two off after a long recipe), so it nudges back while the shift is over half a
     nudge (up to 3 times), then moves the boxes by what is left,
  3. measures the mean colour saturation in each box: clothes roughly double it, walls stay grey,
  4. goes back to the street view and, if laundry is out, warns: Home Assistant notify services (phone
     push) plus a banner and a spoken line on the tablet (in /api/cameras as "laundry").
A person moving the camera (Cameras.last_person_move) delays or interrupts the check. Frames are decoded
by ffmpeg into raw RGB, so there is no imaging library.
"""
import logging
import os
import re
import subprocess
import threading
import time

from home import ActionError

log = logging.getLogger("dashboard")

MOVE = re.compile(r"^(left|right|up|down) (stop|[1-9][0-9]?)$")
SMALL_W, SMALL_H = 320, 180    # frames for difference scores (end stops, alignment)
# One pan / tilt nudge in small pixels near the clotheslines (2026-10-25). Looking steeply down, a tilt
# nudge moves the picture about 100 px, so a tilt correction only happens when it's a full nudge off
# (half of 100); a smaller one overshot.
NUDGE_PX = (48, 100)
FULL_W, FULL_H = 1280, 720     # the V380 HD stream; boxes are in these pixels

DEFAULT_LAUNDRY_CONFIG = {
    "enabled": False,
    "camera": "v380",
    "rain_probability": 50,   # % in any of the next rain_hours hours (or raining now)
    "rain_hours": 3,
    "repeat_hours": 6,        # one check per rain spell; again after this, or after the rain went away
    "min_gap_minutes": 90,    # never closer than this (a forecast flapping around the threshold)
    "person_idle_seconds": 120,
    "settle_seconds": 2.5,    # after a nudge, before the frame (RTSP is 1-2 s behind)
    "nudge_pause_seconds": 1.0,  # between counted nudges (0.3 made 24 nudges go about 2 too far)
    "max_shift": 64,          # alignment search, in 320x180 pixels (one pan nudge is about 48)
    "stop_diff": 3.0,         # two nudges in a row under this = end stop
    "max_stop_nudges": 32,
    "alert_hours": 3,         # the tablet banner goes away after this
    "notify_services": [],    # e.g. ["notify.mobile_app_pixel_7"] once the HA companion app is installed
    "speak": True,
    "save_frames_dir": "data/ptz-map/checks",  # private; "" to keep nothing
    "keep_frames": 60,
    "home": ["right 28", "up 14", "down 3", "left 18"],
    "lines": [],              # [{"id", "label", "moves", "ref", "boxes": [{"box": [x0,y0,x1,y1], "saturation"}]}]
}


def validate_laundry_config(c):
    for key in ("rain_probability", "rain_hours", "repeat_hours", "min_gap_minutes", "person_idle_seconds",
                "settle_seconds", "nudge_pause_seconds", "max_shift", "stop_diff", "max_stop_nudges", "alert_hours", "keep_frames"):
        v = c[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise ValueError("laundry.%s must be a number >= 0" % key)
    if not 1 <= c["rain_hours"] <= 6:
        raise ValueError("laundry.rain_hours must be 1-6")
    if not isinstance(c["notify_services"], list) or not all(
            isinstance(s, str) and re.match(r"^notify\.[a-z0-9_]+$", s) for s in c["notify_services"]):
        raise ValueError("laundry.notify_services must be a list like [\"notify.mobile_app_phone\"]")
    for m in c["home"]:
        _check_move(m, "laundry.home")
    ids = set()
    for line in c["lines"]:
        if not isinstance(line, dict) or not re.match(r"^[a-z0-9_]{1,32}$", str(line.get("id"))):
            raise ValueError("laundry.lines: each needs an id like line1")
        if line["id"] in ids:
            raise ValueError("laundry.lines: duplicate id %s" % line["id"])
        ids.add(line["id"])
        if not isinstance(line.get("label"), str) or not line["label"]:
            raise ValueError("laundry line %s: label missing" % line["id"])
        if not isinstance(line.get("moves"), list) or not line["moves"]:
            raise ValueError("laundry line %s: moves missing" % line["id"])
        for m in line["moves"]:
            _check_move(m, "laundry line %s" % line["id"])
        if line.get("ref") is not None and not isinstance(line["ref"], str):
            raise ValueError("laundry line %s: ref must be a file path" % line["id"])
        boxes = line.get("boxes")
        if not isinstance(boxes, list) or not boxes:
            raise ValueError("laundry line %s: boxes missing" % line["id"])
        for b in boxes:
            box = b.get("box") if isinstance(b, dict) else None
            if (not isinstance(box, list) or len(box) != 4 or not all(isinstance(v, int) for v in box)
                    or not (0 <= box[0] < box[2] <= FULL_W and 0 <= box[1] < box[3] <= FULL_H)):
                raise ValueError("laundry line %s: box must be [x0, y0, x1, y1] inside 1280x720" % line["id"])
            if not isinstance(b.get("saturation"), (int, float)) or not 0 < b["saturation"] < 255:
                raise ValueError("laundry line %s: saturation must be 1-254" % line["id"])


def _check_move(m, where):
    if not isinstance(m, str) or not MOVE.match(m):
        raise ValueError("%s: move %r must look like \"left 9\" or \"up stop\"" % (where, m))


def rain_coming(data, probability, hours):
    """(True/False, reason for the log, reason in Spanish for the warning) from a normalized weather
    reading (weather.WeatherService.normalize)."""
    if not data:
        return False, "no weather", ""
    cur = data.get("current") or {}
    if cur.get("icon") in ("rain", "storm") or (cur.get("precipitation") or 0) > 0:
        return True, "raining now (%s)" % cur.get("condition", "rain"), "ya está lloviendo"
    hourly = data.get("next_hours")
    for h in (hourly if isinstance(hourly, list) else [])[:int(hours)]:
        p = h.get("precipitation_probability") if isinstance(h, dict) else None
        if p is not None and p >= probability:
            at = str(h.get("time", ""))[11:16]
            return True, "%d%% at %s" % (p, at), "va a llover, %d%% a las %s" % (p, at)
    return False, "no rain in the next %d h" % hours, ""


def decode(jpeg, width, height, runner=subprocess.run):
    """Raw RGB bytes (width*height*3) of a JPEG, scaled by ffmpeg."""
    proc = runner(["ffmpeg", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-i", "-",
                   "-vf", "scale=%d:%d" % (width, height), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                  input=jpeg, capture_output=True, timeout=20)
    if proc.returncode != 0 or len(proc.stdout) != width * height * 3:
        raise ActionError("could not decode the camera picture", 502)
    return proc.stdout


def grey_diff(a, b, width=SMALL_W, height=SMALL_H, skip_top=20):
    """Mean absolute grey difference of two RGB frames, leaving out the timestamp strip at the top."""
    start = skip_top * width * 3
    total, n = 0, 0
    for i in range(start, width * height * 3, 6):  # every other pixel is plenty
        ga = a[i] * 299 + a[i + 1] * 587 + a[i + 2] * 114
        gb = b[i] * 299 + b[i + 1] * 587 + b[i + 2] * 114
        total += abs(ga - gb)
        n += 1
    return total / (n * 1000.0) if n else 0.0


def shifted_diff(a, b, dx, dy, width=SMALL_W, height=SMALL_H, skip_top=20, step=3):
    """grey_diff of frame a against frame b moved by (dx, dy), over the part they share."""
    total, n = 0, 0
    for y in range(max(skip_top, skip_top + dy), min(height, height + dy), step):
        for x in range(max(0, dx), min(width, width + dx), step):
            i, j = (y * width + x) * 3, ((y - dy) * width + x - dx) * 3
            total += abs(a[i] * 299 + a[i + 1] * 587 + a[i + 2] * 114 - b[j] * 299 - b[j + 1] * 587 - b[j + 2] * 114)
            n += 1
    return total / (n * 1000.0) if n else 1e9


def best_shift(cur, ref, max_shift):
    """(dx, dy, diff, at_edge): how far cur is moved against ref, in small pixels; coarse, then fine."""
    my = max_shift * 3 // 4
    # ties (a featureless picture) go to the smallest shift
    _, _, bx, by = min((shifted_diff(cur, ref, dx, dy), abs(dx) + abs(dy), dx, dy)
                       for dx in range(-max_shift, max_shift + 1, 4) for dy in range(-my, my + 1, 4))
    d, _, dx, dy = min((shifted_diff(cur, ref, x, y), abs(x) + abs(y), x, y)
                       for x in range(bx - 3, bx + 4) for y in range(by - 3, by + 4))
    return dx, dy, d, abs(bx) >= max_shift - 3 or abs(by) >= my - 3


def shift_box(box, dx, dy):
    """A full-size box moved by a small-pixel shift, kept inside the frame."""
    sx, sy = dx * FULL_W // SMALL_W, dy * FULL_H // SMALL_H
    x0, y0 = min(max(0, box[0] + sx), FULL_W - 1), min(max(0, box[1] + sy), FULL_H - 1)
    return [x0, y0, max(x0 + 1, min(FULL_W, box[2] + sx)), max(y0 + 1, min(FULL_H, box[3] + sy))]


def saturation(rgb, box, width=FULL_W):
    """Mean HSV saturation (0-255) inside box [x0, y0, x1, y1] of an RGB frame."""
    x0, y0, x1, y1 = box
    total, n = 0, 0
    for y in range(y0, y1):
        row = y * width * 3
        for x in range(x0, x1):
            i = row + x * 3
            hi = max(rgb[i], rgb[i + 1], rgb[i + 2])
            if hi:
                total += (hi - min(rgb[i], rgb[i + 1], rgb[i + 2])) * 255 // hi
            n += 1
    return total / n if n else 0.0


class Interrupted(Exception):
    """Someone moved the camera during the check."""


class LaundryWatch:
    def __init__(self, cfg, cameras, home, base_dir, speaker=None, runner=subprocess.run,
                 clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.cfg = cfg
        self.cameras = cameras
        self.home = home
        self.base_dir = base_dir
        self.speaker = speaker
        self.runner = runner
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self._lock = threading.Lock()
        self._thread = None
        self._started_mono = None
        self._last_trigger = None   # clock() of the last automatic check
        self._rearmed = False       # a reading without rain since then
        self._refs = {}             # line id -> small RGB of its reference picture
        self.alert = None           # {"key", "text", "speech_id", "at_ms"} for the tablet
        self.last = None            # result of the last check

    # --- trigger ---------------------------------------------------------------------------
    def on_weather(self, data):
        """Called with each new weather reading. Never raises, never blocks."""
        if not self.cfg["enabled"] or not self.cfg["lines"]:
            return
        try:
            coming, why, why_es = rain_coming(data, self.cfg["rain_probability"], self.cfg["rain_hours"])
            now = self.clock()
            with self._lock:
                if not coming:
                    self._rearmed = True
                    return
                last = self._last_trigger
                gap = None if last is None else now - last
                if gap is not None and (gap < self.cfg["min_gap_minutes"] * 60 or (
                        gap < self.cfg["repeat_hours"] * 3600 and not self._rearmed)):
                    return
                if not (data.get("current") or {}).get("is_day", True):
                    log.info("laundry: rain coming (%s) but it is dark; not checking", why)
                    return
            if self.start(why, why_es):
                with self._lock:
                    self._last_trigger = now
                    self._rearmed = False
        except Exception:
            log.exception("laundry: weather hook failed")

    def start(self, why, why_es=""):
        """Run a check in the background. False if one is already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._thread = threading.Thread(target=self._run, args=(why, why_es), name="laundry", daemon=True)
            self._thread.start()
        return True

    def status(self):
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            alert = self.alert
            if alert and self.clock() * 1000 - alert["at_ms"] > self.cfg["alert_hours"] * 3600000:
                alert = self.alert = None
            return {"enabled": self.cfg["enabled"], "running": running, "alert": alert, "last": self.last,
                    "lines": [{"id": l["id"], "label": l["label"]} for l in self.cfg["lines"]]}

    # --- the check -------------------------------------------------------------------------
    def _run(self, why, why_es):
        cid = self.cfg["camera"]
        idle = self.cfg["person_idle_seconds"]
        log.info("laundry: checking the clotheslines (%s)", why)
        for _ in range(10):  # wait for a person to finish (up to 10 x idle)
            since = self.monotonic() - self.cameras.last_person_move.get(cid, -1e9)
            if since >= idle:
                break
            self.sleep(idle - since)
        else:
            self._finish(why, why_es, "skipped", "someone kept using the camera", [])
            return
        self._started_mono = self.monotonic()
        lines, result, detail = [], "ok", ""
        try:
            for line in self.cfg["lines"]:
                lines.append(self._check_line(line))
        except Interrupted:
            result, detail = "interrupted", "someone moved the camera"
        except ActionError as exc:
            result, detail = "error", str(exc)
        except Exception as exc:  # never leave the camera pointing at a clothesline silently
            log.exception("laundry: check failed")
            result, detail = "error", "internal error: %s" % exc.__class__.__name__
        if result != "interrupted":
            try:
                self._drive(self.cfg["home"])
            except (Interrupted, ActionError) as exc:
                log.warning("laundry: could not go back to the street view: %s", exc)
        self._finish(why, why_es, result, detail, lines)

    def _finish(self, why, why_es, result, detail, lines):
        out = [l for l in lines if l["laundry"]]
        with self._lock:
            self.last = {"at_ms": int(self.clock() * 1000), "why": why, "result": result, "detail": detail,
                         "lines": lines}
        log.info("laundry: %s%s; %s", result, " (%s)" % detail if detail else "",
                 ", ".join("%s %s %s" % (l["id"], "LAUNDRY" if l["laundry"] else "empty",
                                         "/".join("%.0f" % s for s in l["saturation"])) for l in lines) or "-")
        if out:
            self._warn(why_es, out)

    def _warn(self, why_es, lines):
        where = " y ".join(l["label"] for l in lines)
        text = "Hay ropa tendida en %s%s." % (where, " y " + why_es if why_es else "")
        sid = None
        if self.cfg["speak"] and self.speaker is not None:
            try:
                sid = self.speaker.prepare(text, "es")
            except Exception:
                log.exception("laundry: preparing the spoken warning failed")
        now_ms = int(self.clock() * 1000)
        with self._lock:
            self.alert = {"key": "l%d" % now_ms, "text": text, "speech_id": sid, "at_ms": now_ms}
        for service in self.cfg["notify_services"]:
            try:
                self.home.ha_notify(service, "Ropa tendida", text)
                log.info("laundry: warned through %s", service)
            except ActionError as exc:
                log.warning("laundry: %s failed: %s", service, exc)

    def _check_line(self, line):
        self._drive(line["moves"])
        self.sleep(self.cfg["settle_seconds"])
        ref = self._ref(line)
        dx = dy = 0
        aligned = None
        if ref is None:
            jpeg = self._frame_jpeg()
        else:
            jpeg, dx, dy, edge = self._align(ref)
            aligned = not edge
            if edge:
                log.warning("laundry: %s is far from its reference (shift %d,%d); boxes may miss", line["id"], dx, dy)
        self._save(jpeg, line["id"])
        rgb = decode(jpeg, FULL_W, FULL_H, self.runner)
        sats = [saturation(rgb, shift_box(b["box"], dx, dy)) for b in line["boxes"]]
        found = any(s >= b["saturation"] for s, b in zip(sats, line["boxes"]))
        return {"id": line["id"], "label": line["label"], "laundry": found,
                "saturation": [round(s, 1) for s in sats], "shift": [dx, dy], "aligned": aligned}

    # --- moving ----------------------------------------------------------------------------
    def _guard(self):
        moved = self.cameras.last_person_move.get(self.cfg["camera"])
        if moved is not None and self._started_mono is not None and moved > self._started_mono:
            raise Interrupted()

    def _nudge(self, direction, settle=True):
        self._guard()
        self.cameras.move(self.cfg["camera"], direction, by_person=False)
        self.sleep(self.cfg["settle_seconds"] if settle else self.cfg["nudge_pause_seconds"])

    def _frame_jpeg(self):
        return self.cameras.still(self.cfg["camera"], hd=True)

    def _small(self):
        return decode(self._frame_jpeg(), SMALL_W, SMALL_H, self.runner)

    def _drive(self, moves):
        for m in moves:
            direction, count = MOVE.match(m).groups()
            if count != "stop":
                for _ in range(int(count)):
                    self._nudge(direction, settle=False)
                continue
            self.sleep(self.cfg["settle_seconds"])  # the moves before may not have settled
            prev, still = self._small(), 0
            for _ in range(int(self.cfg["max_stop_nudges"])):
                self._nudge(direction)
                cur = self._small()
                still = still + 1 if grey_diff(prev, cur) < self.cfg["stop_diff"] else 0
                prev = cur
                if still >= 2:
                    break
            else:
                log.warning("laundry: no %s stop after %d nudges", direction, self.cfg["max_stop_nudges"])

    def _align(self, ref):
        """(jpeg, dx, dy, at_edge) after up to 3 corrective nudges. A picture moved right/down means
        the camera points left/up of the reference."""
        for attempt in range(4):
            jpeg = self._frame_jpeg()
            dx, dy, _d, edge = best_shift(decode(jpeg, SMALL_W, SMALL_H, self.runner), ref,
                                          int(self.cfg["max_shift"]))
            steps = []
            if edge:  # a match at the edge of the search is a guess; don't nudge on it
                return jpeg, dx, dy, edge
            if abs(dx) > NUDGE_PX[0] // 2:
                steps.append("right" if dx > 0 else "left")
            if abs(dy) > NUDGE_PX[1] // 2:
                steps.append("down" if dy > 0 else "up")
            if not steps or attempt == 3:
                return jpeg, dx, dy, edge
            for direction in steps:
                self._nudge(direction)

    def _ref(self, line):
        if line["id"] not in self._refs:
            path = line.get("ref")
            ref = None
            if path:
                try:
                    with open(os.path.join(self.base_dir, path), "rb") as f:
                        ref = decode(f.read(), SMALL_W, SMALL_H, self.runner)
                except (OSError, ActionError) as exc:
                    log.warning("laundry: reference %s unusable (%s); no fine-tuning", path, exc)
            self._refs[line["id"]] = ref
        return self._refs[line["id"]]

    def _save(self, jpeg, name):
        folder = self.cfg["save_frames_dir"]
        if not folder:
            return
        folder = os.path.join(self.base_dir, folder)
        try:
            os.makedirs(folder, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
            with open(os.path.join(folder, "%s_%s.jpg" % (stamp, name)), "wb") as f:
                f.write(jpeg)
            old = sorted(n for n in os.listdir(folder) if n.endswith(".jpg"))
            for n in old[:max(0, len(old) - int(self.cfg["keep_frames"]))]:
                os.remove(os.path.join(folder, n))
        except OSError as exc:
            log.warning("laundry: could not save the frame: %s", exc)
