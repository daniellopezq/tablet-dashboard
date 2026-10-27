"""Live MJPEG from a camera stream: one ffmpeg per feed that runs only while someone is looking, and its
latest JPEG frame for stills. Used by cameras.py for RTSP cameras (SD and HD) and the Blink live view proxy.
Python 3 standard library plus the ffmpeg binary.
"""
import collections
import logging
import re
import subprocess
import threading
import time

from home import ActionError
from voice import resolve_ffmpeg

log = logging.getLogger("dashboard.cameras")

MAX_VIEWERS = 4  # live connections per camera

_FLAG_CACHE = {}


def rtsp_timeout_flag(ffmpeg):
    """The RTSP socket timeout option: "-stimeout" up to ffmpeg 4.x, "-timeout" from 5.0 (which removed
    -stimeout; in 4.x -timeout meant "listen for a connection"). Ubuntu's ffmpeg 6.1 replaced the snap's 4.3
    on PATH on 2026-10-25."""
    if ffmpeg not in _FLAG_CACHE:
        major = 0
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, timeout=10).stdout
            m = re.match(rb"ffmpeg version n?(\d+)\.", out)
            major = int(m.group(1)) if m else 0
        except (OSError, subprocess.SubprocessError):
            pass
        _FLAG_CACHE[ffmpeg] = "-timeout" if major >= 5 else "-stimeout"
    return _FLAG_CACHE[ffmpeg]


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
        ffmpeg = resolve_ffmpeg(self.cfg["ffmpeg_path"])
        if self.url.startswith("http://"):
            # Headers on the command line, not in the URL: ffmpeg's errors (shown on the page) quote the URL.
            extra = "".join("%s: %s\r\n" % kv for kv in (self.headers() if self.headers else {}).items())
            # Blink sends 1080p; 720p is plenty for these screens and much cheaper to re-encode.
            source = (["-headers", extra] if extra else []) + ["-rw_timeout", "15000000", "-i", self.url,
                                                              "-vf", "scale=-2:'min(720,ih)'"]
        else:
            source = ["-rtsp_transport", "tcp", rtsp_timeout_flag(ffmpeg), "5000000", "-i", self.url]
        return [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"] + source + [
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
