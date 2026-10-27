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
import logging
import re
import threading
import time
import urllib.parse
from datetime import datetime

import envfile
import onvif
from home import ActionError, ID_RE, SERVICE_RE, validate_aliases
from livefeed import LiveFeed
from util import num as _num

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
    # Background "New snapshot" for cameras with "ai_refresh_seconds", so the assistant's answers about
    # them are recent. Each one wakes the camera, so only use it on powered cameras.
    "ai_refresh_enabled": True,
    "list": [],
}
AI_REFRESH_MAX_BACKOFF = 600   # seconds between tries after repeated HA/Blink failures
MAX_CAMERAS = 6  # the Cameras tab shows 2x2, or 3x2 for 5-6 cameras
CAMERA_RE = re.compile(r"^camera\.[a-z0-9_]{1,128}$")
SENSOR_RE = re.compile(r"^binary_sensor\.[a-z0-9_]{1,128}$")
# No user:password@, no query string: credentials don't belong in config.json, and the URL goes to ffmpeg.
RTSP_RE = re.compile(r"^rtsp://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?$")
PROXY_RE = re.compile(r"^http://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")
SLUG_RE = re.compile(r"^[^\W_A-Z][^\WA-Z]{0,63}$")  # the proxy's names keep accents: "cámara_patio"
ONVIF_RE = re.compile(r"^http://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]{0,200})?$")



def validate_camera(c):
    if not isinstance(c, dict):
        raise ValueError("cameras.list entries must be objects")
    cid = c.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        raise ValueError("camera id must be 1-32 of a-z 0-9 _ - (got %r)" % (cid,))
    if not isinstance(c.get("label"), str) or not 1 <= len(c["label"]) <= 40:
        raise ValueError("camera %r: label must be 1-40 characters" % cid)
    if "aliases" in c:
        validate_aliases("camera %r" % cid, c["aliases"])
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
        for key in ("entity_id", "motion_entity", "battery_entity", "popup", "snapshot", "snapshot_service",
                    "ai_refresh_seconds"):
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
    if "ai_refresh_seconds" in c:
        r = c["ai_refresh_seconds"]
        if not isinstance(r, int) or isinstance(r, bool) or not 60 <= r <= 3600:
            raise ValueError("camera %r: ai_refresh_seconds must be an integer 60-3600" % cid)
        if not c.get("snapshot", True):
            raise ValueError("camera %r: ai_refresh_seconds needs snapshots turned on" % cid)


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
    if not isinstance(c["ai_refresh_enabled"], bool):
        raise ValueError("cameras.ai_refresh_enabled must be true or false")
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


def _with_host(url, ip):
    """The same URL with its host replaced (port and path kept)."""
    parts = urllib.parse.urlsplit(url)
    netloc = ip + (":%d" % parts.port if parts.port else "")
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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
        self._stop = threading.Event()
        self._refresh_thread = None
        self._refresh_next = {}   # id -> clock() time of the next background snapshot
        self._refresh_fails = {}  # id -> consecutive failures (backoff)
        self.last_person_move = {}  # id -> time.monotonic() of the last pan/tilt step from the tablet/assistant

    def _blink_url(self, cam):
        # The proxy ends its session after `seconds` whatever happens; a little longer than one viewer may watch.
        return "%s/cameras/%s/mpegts?seconds=%d" % (self.cfg["blink_proxy_url"].rstrip("/"),
                                                  urllib.parse.quote(cam["blink_live"], safe=""),
                                                  int(self.cfg["live_max_seconds"]) + 60)

    def _blink_token_from_env(self):
        key, _problem = envfile.load_api_key(self.home.env_path, (self.cfg["blink_proxy_token_name"],))
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

    def still(self, cid, hd=True):
        """The latest JPEG of an RTSP camera (HD when it has it), starting its feed if needed."""
        self._camera(cid)
        feeds = self._hd_feeds if hd and cid in self._hd_feeds else self._feeds
        if cid not in feeds:
            raise ActionError("this camera has no live view", 404)
        return feeds[cid].still()

    def live_error(self, cid, hd=False):
        feed = (self._hd_feeds if hd else self._feeds).get(cid) or self._feeds.get(cid) or self._blink_feeds.get(cid)
        return feed.error if feed else None

    def close(self):
        self._stop.set()
        for feed in list(self._feeds.values()) + list(self._hd_feeds.values()) + list(self._blink_feeds.values()):
            feed.stop()

    # --- assistant ----------------------------------------------------------------------

    def ai_cameras(self):
        """[{id, label, aliases, live}] for the assistant (which cameras it may look at)."""
        if not self.cfg["enabled"]:
            return []
        return [{"id": c["id"], "label": c["label"], "aliases": list(c.get("aliases", [])),
                 "live": c["id"] in self._feeds} for c in self.cfg["list"]]

    def _ha_updated(self, cam):
        rows = self.home.ha_rows(fresh=True) or {}
        return (rows.get(cam["entity_id"]) or {}).get("last_updated")

    def frame_for_ai(self, cid, max_age=60, wait_seconds=12):
        """(jpeg/png bytes, content type, age in seconds or None) of what the camera sees now.
        RTSP: a live frame (HD feed when there is one). Home Assistant: its snapshot, after asking for a
        new one first when it is older than max_age (and the camera allows it). Raises ActionError."""
        cam = self._camera(cid)
        if cid in self._feeds:
            if cid in self._hd_feeds:
                try:
                    return self._hd_feeds[cid].still(), "image/jpeg", 0
                except ActionError as exc:
                    log.info("cameras: HD frame for the assistant failed (%s), using SD: %s", cid, exc)
            return self._feeds[cid].still(), "image/jpeg", 0
        before = self._ha_updated(cam)
        age = self._age(before)
        if (age is None or age > max_age) and cam.get("snapshot", True):
            try:
                self.request_snapshot(cid)
            except ActionError as exc:
                if exc.code != 429:
                    log.info("cameras: snapshot for the assistant failed (%s): %s", cid, exc)
            else:
                for _ in range(max(1, int(wait_seconds / 1.5))):
                    self.sleep(1.5)
                    try:
                        self.home.ha_service("homeassistant.update_entity", cam["entity_id"])
                    except ActionError:
                        pass
                    now_updated = self._ha_updated(cam)
                    if now_updated and now_updated != before:
                        before = now_updated
                        break
                age = self._age(before)
        data, ctype = self.home.ha_image(cam["entity_id"])
        return data, ctype, age

    def _age(self, iso):
        ms = _iso_ms(iso)
        return None if ms is None else max(0, int(self.clock() - ms / 1000))

    def start_ai_refresh(self):
        """Background snapshots for cameras with ai_refresh_seconds (see DEFAULT_CAMERAS_CONFIG)."""
        if self._refresh_thread is not None or not any(c.get("ai_refresh_seconds") for c in self.cfg["list"]):
            return
        self._refresh_thread = threading.Thread(target=self._refresh_loop, name="camera-ai-refresh", daemon=True)
        self._refresh_thread.start()

    def _refresh_loop(self):
        while not self._stop.wait(5):
            try:
                self.refresh_due()
            except Exception:  # never let the loop die
                log.exception("cameras: background refresh crashed")

    def refresh_due(self):
        """One pass of the background refresh: snapshot every camera whose time has come."""
        if not (self.cfg["enabled"] and self.cfg["ai_refresh_enabled"]):
            return
        now = self.clock()
        for c in self.cfg["list"]:
            every = c.get("ai_refresh_seconds")
            if not every or not c.get("snapshot", True):
                continue
            cid = c["id"]
            if now < self._refresh_next.get(cid, 0.0):
                continue
            try:
                self.request_snapshot(cid, background=True)
            except ActionError as exc:
                if exc.code == 429:  # someone pressed New snapshot recently: that counts
                    self._refresh_next[cid] = now + every
                    continue
                fails = self._refresh_fails.get(cid, 0) + 1
                self._refresh_fails[cid] = fails
                delay = min(every * (2 ** fails), AI_REFRESH_MAX_BACKOFF)
                if fails == 1:
                    log.warning("cameras: background snapshot of %s failed (%s); retrying with backoff", cid, exc)
                self._refresh_next[cid] = now + delay
                continue
            if self._refresh_fails.pop(cid, 0):
                log.info("cameras: background snapshots of %s work again", cid)
            self._refresh_next[cid] = now + every

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

    def move(self, cid, direction, by_person=True):
        """One short pan/tilt step: left, right, up or down. by_person=False for automations (laundry.py),
        which watch last_person_move to stay out of the way of someone using the camera."""
        cam = self._control(cid, "ptz")
        if direction not in onvif.DIRECTIONS:
            raise ActionError("move must be left, right, up or down", 400)
        if by_person:
            self.last_person_move[cid] = time.monotonic()
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

    def request_snapshot(self, cid, background=False):
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
        # The background refresh asks every minute per camera: keep it out of the INFO log.
        log.log(logging.DEBUG if background else logging.INFO, "cameras: new snapshot requested for %s", cid)

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

