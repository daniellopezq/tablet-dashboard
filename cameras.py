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
small sub-stream: re-encoding 640x360 at 10 fps costs about a tenth of one CPU core.

Configured under "cameras" in config.json. Python 3 standard library plus the ffmpeg binary.
"""
import collections
import logging
import re
import subprocess
import threading
import time
from datetime import datetime

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
    "list": [],
}
MAX_CAMERAS = 6  # the Cameras tab shows 2x2, or 3x2 for 5-6 cameras
MAX_VIEWERS = 4  # live connections per camera
CAMERA_RE = re.compile(r"^camera\.[a-z0-9_]{1,128}$")
SENSOR_RE = re.compile(r"^binary_sensor\.[a-z0-9_]{1,128}$")
# No user:password@, no query string: credentials don't belong in config.json, and the URL goes to ffmpeg.
RTSP_RE = re.compile(r"^rtsp://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?$")


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
        fps = c.get("live_fps", 10)
        if not _num(fps) or not 1 <= fps <= 15:
            raise ValueError("camera %r: live_fps must be a number 1-15" % cid)
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
    if not isinstance(c["list"], list) or len(c["list"]) > MAX_CAMERAS:
        raise ValueError("cameras.list must be a list of at most %d cameras" % MAX_CAMERAS)
    ids = set()
    for cam in c["list"]:
        validate_camera(cam)
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

    def __init__(self, cam, cfg):
        self.cam = cam
        self.cfg = cfg
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
        return [resolve_ffmpeg(self.cfg["ffmpeg_path"]), "-hide_banner", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-stimeout", "5000000", "-i", self.cam["rtsp_url"],
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
        log.info("cameras: live feed for %s started (pid %d)", self.cam["id"], proc.pid)
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
                self.error = msg[:200]
                if not got_frame:
                    self._failed_at = time.monotonic()
                log.info("cameras: live feed for %s ended: %s", self.cam["id"], self.error)
            else:
                log.info("cameras: live feed for %s stopped (nobody watching)", self.cam["id"])
            self._cv.notify_all()

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
    def __init__(self, cfg, home, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.home = home
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._images = {}     # id -> (version, fetched_at, bytes, ctype)
        self._snapped = {}    # id -> time of the last "New snapshot"
        self._feeds = {c["id"]: LiveFeed(c, cfg) for c in cfg["list"] if c.get("type") == "rtsp"}

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

    def live(self, cid):
        """Generator of JPEG frames for one live viewer of an RTSP camera (see LiveFeed.frames)."""
        self._camera(cid)
        if cid not in self._feeds:
            raise ActionError("this camera has no live view", 404)
        return self._feeds[cid].frames(self.cfg["live_max_seconds"])

    def live_error(self, cid):
        feed = self._feeds.get(cid)
        return feed.error if feed else None

    def close(self):
        for feed in self._feeds.values():
            feed.stop()

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

