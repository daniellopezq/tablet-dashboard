"""Cameras tab: the latest snapshot of each Home Assistant camera (Blink today), live view of local RTSP
cameras (V380), plus motion pop-ups.

Blink in Home Assistant has no live view. HA keeps the last thumbnail of each camera, and reading
it costs nothing. "New snapshot" (blink.trigger_camera) wakes the camera, so it is rate limited per
camera. Images are fetched by this server through HA's /api/camera_proxy, so the HA token never
reaches the tablet.

The image "version" is the camera entity's last_updated, which changes when Blink brings a new
thumbnail. The tablet asks for /api/cameras/<id>.jpg?v=<version>, and the server fetches from HA
only when that version is new.

Motion pop-up: a camera with "popup": true whose motion sensor turned on within popup_seconds is
reported as the current alert. The tablet shows it once per alert key (camera + time of change).

RTSP cameras ("type": "rtsp"): one ffmpeg per camera turns the camera's H.264 stream into JPEG frames
(MJPEG), started when someone looks and stopped live_idle_seconds after the last viewer. The tablet's
Chrome 50 shows MJPEG in a plain <img>, which its video element can't be relied on to do for a live
H.264 stream. Every viewer, and the grid's still picture, shares that one ffmpeg. Use the camera's
small sub-stream: re-encoding 640x360 at 10 fps costs about a tenth of one CPU core. An optional
"rtsp_url_hd" (the main stream) gets its own ffmpeg, used only by viewers who pick HD.

Controls ("onvif_url", see onvif.py): pan/tilt steps and the day/night mode, LAN only like the rest.

Blink live view ("blink_live", 0.21.0): a Home Assistant camera can also have an on-demand live view through
a local Blink live-view proxy (github.com/Teethree89/ha-blink-live-view-proxy). The proxy streams MPEG-TS over
HTTP; the same LiveFeed turns it into MJPEG. Each start is a Blink cloud session, so the page starts it only
when asked, and ffmpeg ending (last viewer gone) ends the session. The proxy's token comes from .env.

Finding a camera again ("mac"): when its stream or controls stop answering, an ONVIF WS-Discovery probe (at
most every RELOCATE_SECONDS) looks for the device with that MAC. If it answers from a new address, every URL
of the camera moves there (in memory; config.json is not rewritten, so update it when convenient).

Configured under "cameras" in config.json. Python 3 standard library plus the ffmpeg binary.
"""
import collections
import logging
import re
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime

import assistant
import onvif
from home import ActionError, ID_RE, SERVICE_RE
from voice import resolve_ffmpeg

log = logging.getLogger("dashboard.cameras")

DEFAULT_CAMERAS_CONFIG = {
    "enabled": True,
    "popup_seconds": 60,
    "snapshot_min_interval_seconds": 60,
    "image_max_age_seconds": 600,   # refetch an unchanged image after this long anyway
    "ffmpeg_path": "ffmpeg",
    "still_seconds": 10,            # grid picture refresh for RTSP cameras
    "live_idle_seconds": 15,        # stop a camera's ffmpeg this long after its last viewer
    "live_max_seconds": 600,        # one live view ends after this; the tablet offers to resume
    "blink_proxy_url": "",          # e.g. http://127.0.0.1:8088 (Blink live view; empty = off)
    "blink_proxy_token_name": "BLINK_PROXY_TOKEN",
    "list": [],
}
MAX_CAMERAS = 6  # the Cameras tab shows 2x2, or 3x2 for 5-6 cameras
MAX_VIEWERS = 4  # live connections per camera
CAMERA_RE = re.compile(r"^camera\.[a-z0-9_]{1,128}$")
SENSOR_RE = re.compile(r"^binary_sensor\.[a-z0-9_]{1,128}$")
# No user:password@, no query string: credentials don't belong in config.json, and the URL goes to ffmpeg.
RTSP_RE = re.compile(r"^rtsp://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?$")
PROXY_RE = re.compile(r"^http://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")
SLUG_RE = re.compile(r"^[^\W_A-Z][^\WA-Z]{0,63}$")  # the proxy's names keep accents: "cámara_patio"
ONVIF_RE = re.compile(r"^http://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?$")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_camera(c):
    if not isinstance(c, dict):
        raise ValueError("cameras.list entries must be objects")
    cid = c.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise ValueError("camera id must be 1-32 of a-z 0-9 _ - (got %r)" % (cid,))
    if not isinstance(c.get("label"), str) or not 1 <= len(c["label"]) <= 40:
        raise ValueError("camera %r: label must be 1-40 characters" % cid)
    kind = c.get("type", "ha")
    if kind == "rtsp":
        if not isinstance(c.get("rtsp_url"), str) or not RTSP_RE.match(c["rtsp_url"]):
            raise ValueError("camera %r: rtsp_url must look like rtsp://192.168.1.20:554/live/ch00_1 "
                             "(no user:password, no query)" % cid)
        if "rtsp_url_hd" in c and not (isinstance(c["rtsp_url_hd"], str) and RTSP_RE.match(c["rtsp_url_hd"])):
            raise ValueError("camera %r: rtsp_url_hd must look like rtsp://192.168.1.20:554/live/ch00_0" % cid)
        fps = c.get("live_fps", 10)
        if not _num(fps) or not 1 <= fps <= 15:
            raise ValueError("camera %r: live_fps must be a number 1-15" % cid)
        if "onvif_url" in c and not (isinstance(c["onvif_url"], str) and ONVIF_RE.match(c["onvif_url"])):
            raise ValueError("camera %r: onvif_url must look like http://192.168.1.20:8899/onvif/device_service "
                             "(no user:password, no query)" % cid)
        if "mac" in c and not (isinstance(c["mac"], str) and onvif.normalize_mac(c["mac"])):
            raise ValueError("camera %r: mac must look like 00:00:5e:00:53:01" % cid)
        for key in ("ptz", "day_night"):
            if key in c and not isinstance(c[key], bool):
                raise ValueError("camera %r: %s must be true or false" % (cid, key))
            if c.get(key) and "onvif_url" not in c:
                raise ValueError("camera %r: %s needs onvif_url" % (cid, key))
        for key in ("entity_id", "motion_entity", "battery_entity", "popup", "snapshot", "snapshot_service"):
            if key in c:
                raise ValueError("camera %r: %s is for Home Assistant cameras, not rtsp" % (cid, key))
        return
    if kind != "ha":
        raise ValueError("camera %r: type must be \"ha\" or \"rtsp\"" % cid)
    if not isinstance(c.get("entity_id"), str) or not CAMERA_RE.match(c["entity_id"]):
        raise ValueError("camera %r: entity_id must look like camera.front_door" % cid)
    for key in ("motion_entity", "battery_entity"):
        if key in c and not (isinstance(c[key], str) and SENSOR_RE.match(c[key])):
            raise ValueError("camera %r: %s must look like binary_sensor.front_door_motion" % (cid, key))
    for key in ("popup", "snapshot"):
        if key in c and not isinstance(c[key], bool):
            raise ValueError("camera %r: %s must be true or false" % (cid, key))
    if c.get("popup") and not c.get("motion_entity"):
        raise ValueError("camera %r: popup needs motion_entity" % cid)
    if "blink_live" in c and not (isinstance(c["blink_live"], str) and SLUG_RE.match(c["blink_live"])):
        raise ValueError("camera %r: blink_live must be the proxy's camera name, like cámara_patio" % cid)
    svc = c.get("snapshot_service", "blink.trigger_camera")
    if not isinstance(svc, str) or not SERVICE_RE.match(svc):
        raise ValueError("camera %r: snapshot_service must look like blink.trigger_camera" % cid)


def validate_cameras_config(c):
    if not isinstance(c["enabled"], bool):
        raise ValueError("cameras.enabled must be true or false")
    for key, lo, hi in (("popup_seconds", 0, 600), ("snapshot_min_interval_seconds", 10, 3600),
                        ("image_max_age_seconds", 10, 86400), ("still_seconds", 2, 600),
                        ("live_idle_seconds", 2, 600), ("live_max_seconds", 10, 86400)):
        if not _num(c[key]) or not lo <= c[key] <= hi:
            raise ValueError("cameras.%s must be a number %s-%s" % (key, lo, hi))
    if not isinstance(c["ffmpeg_path"], str) or not c["ffmpeg_path"]:
        raise ValueError("cameras.ffmpeg_path must be a program name or path")
    if not isinstance(c["blink_proxy_url"], str) or (c["blink_proxy_url"] and not PROXY_RE.match(c["blink_proxy_url"])):
        raise ValueError("cameras.blink_proxy_url must be empty or look like http://127.0.0.1:8088")
    if not isinstance(c["blink_proxy_token_name"], str) or not re.match(r"^[A-Z][A-Z0-9_]{1,63}$", c["blink_proxy_token_name"]):
        raise ValueError("cameras.blink_proxy_token_name must be an env variable name such as BLINK_PROXY_TOKEN")
    if not isinstance(c["list"], list) or len(c["list"]) > MAX_CAMERAS:
        raise ValueError("cameras.list must be a list of at most %d cameras" % MAX_CAMERAS)
    ids = set()
    for cam in c["list"]:
        validate_camera(cam)
        if cam.get("blink_live") and not c["blink_proxy_url"]:
            raise ValueError("camera %r: blink_live needs cameras.blink_proxy_url" % cam["id"])
        if cam["id"] in ids:
            raise ValueError("camera ids must be unique (%r)" % cam["id"])
        ids.add(cam["id"])


def _iso_ms(value):
    try:
        return int(datetime.fromisoformat(str(value)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


class LiveFeed:
    """One RTSP camera: an ffmpeg that runs only while someone is looking, and its latest JPEG frame."""

    MAX_BUFFER = 4 * 1024 * 1024   # bytes without a complete frame before the buffer is dropped
    FIRST_FRAME_SECONDS = 10       # how long a still or a new viewer waits for ffmpeg's first frame
    RETRY_SECONDS = 5              # no new ffmpeg sooner than this after one failed

    def __init__(self, cam, cfg, url=None, name=None, on_failure=None, headers=None):
        self.cam = cam
        self.cfg = cfg
        self.url = url or cam["rtsp_url"]
        self.name = name or cam["id"]
        self.on_failure = on_failure  # called (no arguments) when an ffmpeg ends without a single frame
        self.headers = headers        # () -> {name: value} for an http:// input, read at each start
        self._cv = threading.Condition()
        self._proc = None
        self._frame = None
        self._frame_at = 0.0       # time.time() of the latest frame
        self._seq = 0
        self._viewers = 0
        self._last_use = 0.0       # time.monotonic() of the last still or viewer
        self._failed_at = None     # time.monotonic() of the last ffmpeg that ended without a frame
        self.error = None          # why the last ffmpeg ended, for the tablet

    def command(self):
        if self.url.startswith("http://"):
            # Headers on the command line, not in the URL: ffmpeg's errors (shown on the page) quote the URL.
            extra = "".join("%s: %s\r\n" % kv for kv in (self.headers() if self.headers else {}).items())
            # Blink sends 1080p; 720p is plenty for these screens and much cheaper to re-encode.
            source = (["-headers", extra] if extra else []) + ["-rw_timeout", "15000000", "-i", self.url,
                                                              "-vf", "scale=-2:'min(720,ih)'"]
        else:
            source = ["-rtsp_transport", "tcp", "-stimeout", "5000000", "-i", self.url]
        return [resolve_ffmpeg(self.cfg["ffmpeg_path"]), "-hide_banner", "-nostdin", "-loglevel", "error"] + source + [
                "-an", "-r", str(self.cam.get("live_fps", 10)), "-c:v", "mjpeg", "-q:v", "6",
                "-f", "image2pipe", "-"]

    def _start_locked(self):
        if self._proc is not None:
            return True
        if self._failed_at is not None and time.monotonic() - self._failed_at < self.RETRY_SECONDS:
            return False
        try:
            proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as exc:
            self._failed_at = time.monotonic()
            self.error = "ffmpeg did not start (%s)" % exc.strerror
            return False
        self._proc = proc
        self._last_use = time.monotonic()
        tail = collections.deque(maxlen=20)
        threading.Thread(target=self._drain, args=(proc, tail), name="camera-ffmpeg-err", daemon=True).start()
        threading.Thread(target=self._read, args=(proc, tail), name="camera-ffmpeg", daemon=True).start()
        log.info("cameras: live feed for %s started (pid %d)", self.name, proc.pid)
        return True

    @staticmethod
    def _drain(proc, tail):
        for line in proc.stderr:
            tail.append(line.decode("utf-8", "replace").strip())

    def _read(self, proc, tail):
        buf = b""
        got_frame = False
        idle = False
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            buf += chunk
            frames = []
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if end < 0:
                    break
                frames.append(buf[start:end + 2])
                buf = buf[end + 2:]
            if len(buf) > self.MAX_BUFFER:
                buf = b""
            with self._cv:
                if frames:
                    got_frame = True
                    self._frame = frames[-1]
                    self._frame_at = time.time()
                    self._seq += 1
                    self.error = None
                    self._cv.notify_all()
                if self._viewers == 0 and time.monotonic() - self._last_use > self.cfg["live_idle_seconds"]:
                    idle = True
                    break
        _stop_process(proc)
        with self._cv:
            if self._proc is not proc:
                idle = True  # stop() took it away on purpose
            else:
                self._proc = None
            if not idle:
                msg = next((x for x in reversed(tail) if x), "") or "the camera stopped sending video"
                self.error = self._explain(msg)[:200]
                if not got_frame:
                    self._failed_at = time.monotonic()
                log.info("cameras: live feed for %s ended: %s", self.name, self.error)
            else:
                log.info("cameras: live feed for %s stopped (nobody watching)", self.name)
            self._cv.notify_all()
        if not idle and not got_frame and self.on_failure:
            self.on_failure()

    # ffmpeg's words for what the Blink proxy answered, in words for the page (and without the URL).
    PROXY_ERRORS = (("4XX Client Error", "Blink needs about 30 s between live views; try again shortly"),
                    ("404 Not Found", "the Blink proxy doesn't know this camera (check blink_live)"),
                    ("401 Unauthorized", "the Blink proxy refused the token (check BLINK_PROXY_TOKEN in .env)"),
                    ("Connection refused", "the Blink proxy isn't running"),
                    ("5XX Server Error", "Blink didn't start the live view; try again in a minute"))

    def _explain(self, msg):
        if not self.url.startswith("http://"):
            return msg
        return next((text for key, text in self.PROXY_ERRORS if key in msg), "Blink live view ended: " + msg.split(": ")[-1])

    def status(self):
        """(available, time.time() of the latest frame or None) for the tablet."""
        with self._cv:
            failed = self._failed_at is not None and time.monotonic() - self._failed_at < 60
            return not failed, (self._frame_at or None)

    def still(self):
        """The latest frame, starting ffmpeg (and waiting for its first frame) when it isn't running."""
        deadline = time.monotonic() + self.FIRST_FRAME_SECONDS
        with self._cv:
            self._last_use = time.monotonic()
            fresh = self._proc is not None and self._frame is not None and time.time() - self._frame_at < 5
            if fresh:
                return self._frame
            seq = self._seq
            if not self._start_locked():
                raise ActionError(self.error or "camera not answering", 502)
            while self._seq == seq and self._proc is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            if self._seq == seq:
                raise ActionError(self.error or "camera not answering", 504)
            return self._frame

    def frames(self, max_seconds):
        """Yields JPEG frames for one viewer until max_seconds pass or the camera stops. Raises
        ActionError on the first next() when the viewer can't be served (start it before replying)."""
        with self._cv:
            if self._viewers >= MAX_VIEWERS:
                raise ActionError("too many live views of this camera", 429)
            running = self._proc is not None
            if not self._start_locked():
                raise ActionError(self.error or "camera not answering", 502)
            self._viewers += 1
            # A running feed's latest frame is current; one left from an earlier ffmpeg is not.
            seq = 0 if running else self._seq
        end = time.monotonic() + max_seconds
        try:
            while True:
                with self._cv:
                    while self._seq == seq and self._proc is not None and time.monotonic() < end:
                        self._cv.wait(min(1.0, max(0.0, end - time.monotonic())))
                    if self._seq == seq or time.monotonic() >= end:
                        return
                    seq, frame = self._seq, self._frame
                yield frame
        finally:
            self._leave()

    def _leave(self):
        with self._cv:
            self._viewers -= 1
            self._last_use = time.monotonic()

    def stop(self):
        with self._cv:
            proc, self._proc = self._proc, None
        if proc:
            _stop_process(proc)


def _with_host(url, ip):
    """The same URL with its host replaced (port and path kept)."""
    parts = urllib.parse.urlsplit(url)
    netloc = ip + (":%d" % parts.port if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    for f in (proc.stdout, proc.stderr):
        try:
            f.close()
        except (OSError, ValueError):
            pass


class Cameras:
    PTZ_SPEED = 0.5
    PTZ_STEP_SECONDS = 0.5

    RELOCATE_SECONDS = 120

    def __init__(self, cfg, home, clock=time.time, sleep=time.sleep, onvif_factory=onvif.Camera,
                 discover=onvif.discover, blink_token=None):
        self.cfg = cfg
        self.home = home
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._images = {}     # id -> (version, fetched_at, bytes, ctype)
        self._snapped = {}    # id -> time of the last "New snapshot"
        self.discover = discover
        self._relocated_at = {}   # id -> time.monotonic() of the last search
        rtsp = [c for c in cfg["list"] if c.get("type") == "rtsp"]
        self._feeds = {c["id"]: LiveFeed(c, cfg, on_failure=self._lost(c)) for c in rtsp}
        self._hd_feeds = {c["id"]: LiveFeed(c, cfg, c["rtsp_url_hd"], c["id"] + " hd", self._lost(c))
                          for c in rtsp if c.get("rtsp_url_hd")}
        self._onvif = {c["id"]: onvif_factory(c["onvif_url"]) for c in rtsp if c.get("onvif_url")}
        self._blink_token = blink_token or self._blink_token_from_env
        self._blink_feeds = {c["id"]: LiveFeed(c, cfg, self._blink_url(c), c["id"] + " blink", headers=self._blink_headers)
                             for c in cfg["list"] if c.get("type", "ha") == "ha" and c.get("blink_live")}

    def _blink_url(self, cam):
        # The proxy ends its session after `seconds` whatever happens; a little longer than one viewer may watch.
        return "%s/cameras/%s/mpegts?seconds=%d" % (self.cfg["blink_proxy_url"].rstrip("/"),
                                                  urllib.parse.quote(cam["blink_live"], safe=""),
                                                  int(self.cfg["live_max_seconds"]) + 60)

    def _blink_token_from_env(self):
        key, _problem = assistant.load_api_key(self.home.env_path, (self.cfg["blink_proxy_token_name"],))
        return key

    def _blink_headers(self):
        token = self._blink_token()
        return {"Authorization": "Bearer " + token} if token else {}

    def _can(self, c, key):
        """Pan/tilt or day/night for this camera: on by default when it has an onvif_url."""
        return c["id"] in self._onvif and c.get(key, True)

    def _camera(self, cid):
        cam = next((c for c in self.cfg["list"] if c["id"] == cid), None)
        if cam is None or not self.cfg["enabled"]:
            raise ActionError("unknown camera", 404)
        return cam

    def _live_item(self, c, now):
        feed = self._feeds[c["id"]]
        available, frame_at = feed.status()
        return {
            "id": c["id"], "label": c["label"], "live": True,
            "available": available,
            "error": None if available else feed.error,
            # The grid asks for a new still every still_seconds; the server shares the running feed.
            "version": "s%d" % int(now // self.cfg["still_seconds"]),
            "updated_ms": int(frame_at * 1000) if frame_at else None,
            "motion": None, "motion_ms": None, "battery_low": None, "motion_detection": None,
            "can_snapshot": False,
            "hd": c["id"] in self._hd_feeds,
            "ptz": self._can(c, "ptz"),
            "day_night": self._can(c, "day_night"),
        }

    def snapshot(self):
        """Status for the tablet: one entry per camera, plus the current motion alert (if any)."""
        out = {"enabled": self.cfg["enabled"], "cameras": [], "alert": None,
               "popup_seconds": self.cfg["popup_seconds"], "live_max_seconds": self.cfg["live_max_seconds"]}
        if not self.cfg["enabled"] or not self.cfg["list"]:
            return out
        now = self.clock()
        rows = {}
        if any(c.get("type", "ha") == "ha" for c in self.cfg["list"]):
            rows = self.home.ha_rows()
            out["ha_ok"] = rows is not None
            out["ha_error"] = None if rows is not None else self.home.ha_problem()
            rows = rows or {}
        now_ms = int(now * 1000)
        best = None
        for c in self.cfg["list"]:
            if c["id"] in self._feeds:
                out["cameras"].append(self._live_item(c, now))
                continue
            cam = rows.get(c["entity_id"])
            motion = rows.get(c.get("motion_entity") or "")
            battery = rows.get(c.get("battery_entity") or "")
            attrs = (cam or {}).get("attributes") or {}
            item = {
                "id": c["id"], "label": c["label"],
                "available": bool(cam) and cam.get("state") not in ("unavailable", "unknown"),
                "version": str((cam or {}).get("last_updated") or "")[:40],
                "updated_ms": _iso_ms((cam or {}).get("last_updated")),
                "motion": None if not motion else motion.get("state") == "on",
                "motion_ms": _iso_ms((motion or {}).get("last_changed")),
                "battery_low": None if not battery else battery.get("state") == "on",
                "motion_detection": attrs.get("motion_detection") if isinstance(attrs.get("motion_detection"), bool) else None,
                "can_snapshot": c.get("snapshot", True),
                "can_live": c["id"] in self._blink_feeds,
                "live_error": self._blink_feeds[c["id"]].error if c["id"] in self._blink_feeds else None,
            }
            out["cameras"].append(item)
            if c.get("popup") and item["motion"] and item["motion_ms"] and \
                    now_ms - item["motion_ms"] < self.cfg["popup_seconds"] * 1000:
                if best is None or item["motion_ms"] > best["since_ms"]:
                    best = {"id": c["id"], "since_ms": item["motion_ms"], "key": "%s:%d" % (c["id"], item["motion_ms"])}
        out["alert"] = best
        return out

    def image(self, cid, version):
        """(bytes, content type) of the camera's latest still. Fetched from HA only when `version` is new."""
        cam = self._camera(cid)
        if cid in self._feeds:
            return self._feeds[cid].still(), "image/jpeg"
        now = self.clock()
        with self._lock:
            hit = self._images.get(cid)
        if hit and hit[0] == version and now - hit[1] < self.cfg["image_max_age_seconds"]:
            return hit[2], hit[3]
        try:
            data, ctype = self.home.ha_image(cam["entity_id"])
        except ActionError:
            if hit:  # HA or Blink hiccup: keep showing the last image we had
                return hit[2], hit[3]
            raise
        with self._lock:
            self._images[cid] = (version, now, data, ctype)
        return data, ctype

    def live(self, cid, hd=False):
        """Generator of JPEG frames for one live viewer of an RTSP camera (see LiveFeed.frames).
        hd: the main stream when the camera has one, else the usual sub-stream."""
        self._camera(cid)
        if cid in self._blink_feeds:
            return self._blink_feeds[cid].frames(self.cfg["live_max_seconds"])
        if cid not in self._feeds:
            raise ActionError("this camera has no live view", 404)
        feeds = self._hd_feeds if hd and cid in self._hd_feeds else self._feeds
        return feeds[cid].frames(self.cfg["live_max_seconds"])

    def live_error(self, cid, hd=False):
        feed = (self._hd_feeds if hd else self._feeds).get(cid) or self._feeds.get(cid) or self._blink_feeds.get(cid)
        return feed.error if feed else None

    def close(self):
        for feed in list(self._feeds.values()) + list(self._hd_feeds.values()) + list(self._blink_feeds.values()):
            feed.stop()

    def _lost(self, cam):
        """on_failure hook for a camera's feeds: look for it in the background (if it has a "mac")."""
        if not cam.get("mac"):
            return None
        return lambda: threading.Thread(target=self.relocate, args=(cam["id"],), name="camera-relocate",
                                        daemon=True).start()

    def relocate(self, cid):
        """Search the LAN for a camera that stopped answering. Returns its new address, or None."""
        cam = self._camera(cid)
        mac = onvif.normalize_mac(cam.get("mac"))
        if not mac:
            return None
        with self._lock:
            last = self._relocated_at.get(cid)
            if last is not None and time.monotonic() - last < self.RELOCATE_SECONDS:
                return None
            self._relocated_at[cid] = time.monotonic()
        old = urllib.parse.urlsplit(cam["rtsp_url"]).hostname
        for ip, (xaddrs, found_mac) in self.discover().items():
            if found_mac is None:  # the endpoint address didn't carry it: ask the camera
                try:
                    found_mac = onvif.Camera(xaddrs[0], timeout=3).mac()
                except onvif.OnvifError:
                    continue
            if found_mac != mac:
                continue
            if ip == old:
                log.info("cameras: %s is still at %s", cid, ip)
                return None
            self._move_camera(cam, ip)
            log.warning("cameras: %s moved from %s to %s; now using the new address "
                        "(update its URLs in config.json)", cid, old, ip)
            return ip
        log.info("cameras: %s (%s) not found on the LAN", cid, mac)
        return None

    def _move_camera(self, cam, ip):
        cid = cam["id"]
        for key in ("rtsp_url", "rtsp_url_hd", "onvif_url"):
            if cam.get(key):
                cam[key] = _with_host(cam[key], ip)
        for feeds, key in ((self._feeds, "rtsp_url"), (self._hd_feeds, "rtsp_url_hd")):
            feed = feeds.get(cid)
            if feed:
                feed.url = cam[key]
                feed._failed_at = None  # try the new address right away
        if cid in self._onvif:
            self._onvif[cid].moved_to(cam["onvif_url"])

    def _onvif_failed(self, cid, exc):
        hook = self._lost(self._camera(cid)) if "not answering" in str(exc) else None
        if hook:
            hook()

    def _control(self, cid, key):
        cam = self._camera(cid)
        if not self._can(cam, key):
            raise ActionError("this camera has no %s control" % ("pan/tilt" if key == "ptz" else "day/night"), 404)
        return self._onvif[cid]

    def controls(self, cid):
        """What the camera page can offer, with the current day/night mode (None if unknown)."""
        cam = self._camera(cid)
        out = {"id": cid, "hd": cid in self._hd_feeds, "ptz": self._can(cam, "ptz"),
               "day_night": self._can(cam, "day_night"), "mode": None, "error": None}
        if out["day_night"]:
            try:
                out["mode"] = self._onvif[cid].mode()
            except onvif.OnvifError as exc:
                out["error"] = str(exc)
                self._onvif_failed(cid, exc)
        return out

    def move(self, cid, direction):
        """One short pan/tilt step: left, right, up or down."""
        cam = self._control(cid, "ptz")
        if direction not in onvif.DIRECTIONS:
            raise ActionError("move must be left, right, up or down", 400)
        try:
            cam.step(direction, self.PTZ_SPEED, self.PTZ_STEP_SECONDS)
        except onvif.OnvifError as exc:
            self._onvif_failed(cid, exc)
            raise ActionError(str(exc), 502)
        return {"ok": True}

    def set_mode(self, cid, mode):
        """Day (colour), night (infrared) or auto."""
        cam = self._control(cid, "day_night")
        if mode not in onvif.MODES:
            raise ActionError("mode must be day, night or auto", 400)
        try:
            cam.set_mode(mode)
        except onvif.OnvifError as exc:
            self._onvif_failed(cid, exc)
            raise ActionError(str(exc), 502)
        log.info("cameras: %s set to %s mode", cid, mode)
        return {"ok": True, "mode": mode}

    def request_snapshot(self, cid):
        """Ask the camera for a new picture (wakes it), then refresh HA's copy in the background."""
        cam = self._camera(cid)
        if cid in self._feeds or not cam.get("snapshot", True):
            raise ActionError("new snapshots are turned off for this camera", 403)
        now = self.clock()
        with self._lock:
            last = self._snapped.get(cid, 0.0)
            wait = self.cfg["snapshot_min_interval_seconds"] - (now - last)
            if wait > 0:
                raise ActionError("wait %d s before another snapshot of this camera" % (wait + 0.999), 429)
            self._snapped[cid] = now
        self.home.ha_service(cam.get("snapshot_service", "blink.trigger_camera"), cam["entity_id"])
        log.info("cameras: new snapshot requested for %s", cid)

        def refresh():
            # Blink stores the new picture in its cloud; HA picks it up on its next update.
            for delay in (5, 10):
                self.sleep(delay)
                try:
                    self.home.ha_service("homeassistant.update_entity", cam["entity_id"])
                except ActionError as exc:
                    log.info("cameras: refresh of %s failed: %s", cid, exc)

        threading.Thread(target=refresh, name="camera-refresh", daemon=True).start()
        return {"ok": True}

