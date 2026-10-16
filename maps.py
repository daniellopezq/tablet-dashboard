"""Weather map slideshow images for the tablet dashboard.

A background thread fetches OpenWeatherMap weather tiles (Weather Maps 1.0 tile URLs) for a
few layers, composites them over a cached OpenStreetMap base map with Pillow, and writes one
PNG per layer into data/maps/. Requests only ever read those files.

The API key is read from the OPENWEATHER_API_KEY environment variable or the private .env
file (same rules as assistant.py) and is never logged, returned to clients or written anywhere.
"""
import io
import logging
import math
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageEnhance

log = logging.getLogger("dashboard.maps")

KEY_ENV = "OPENWEATHER_API_KEY"
KEY_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
TILE = 256
LAYER_TITLES = {
    "precipitation_new": "Precipitation",
    "clouds_new": "Clouds",
    "wind_new": "Wind",
    "temp_new": "Temperature",
    "pressure_new": "Pressure",
}
OWM_TILE_URL = "https://tile.openweathermap.org/map/%s/%d/%d/%d.png?appid=%s"
OSM_TILE_URL = "https://tile.openstreetmap.org/%d/%d/%d.png"

DEFAULT_MAPS_CONFIG = {
    "enabled": True,
    "layers": ["precipitation_new", "clouds_new", "wind_new", "temp_new"],
    "zoom": 13,
    "width": 1024,
    "height": 512,
    "small_width": 512,
    "small_height": 256,
    "small_colors": 64,
    "refresh_minutes": 30,
    "max_calls_per_minute": 30,
    "rotate_seconds": 10,
    "stale_after_minutes": 75,
    "timeout_seconds": 10,
    "max_tile_bytes": 262144,
    "overlay_opacity": 1.0,
    "base_brightness": 0.4,
    "contrast": 1.35,
    "saturation": 1.4,
    "env_file": ".env",
    "cache_dir": "data/maps",
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_maps_config(m):
    if not isinstance(m["enabled"], bool):
        raise ValueError("maps.enabled must be true or false")
    layers = m["layers"]
    if not isinstance(layers, list) or not layers or len(set(layers)) != len(layers):
        raise ValueError("maps.layers must be a non-empty list of distinct layer names")
    for name in layers:
        if name not in LAYER_TITLES:
            raise ValueError("maps.layers: unknown layer %r (use %s)" % (name, ", ".join(sorted(LAYER_TITLES))))
    for key, lo, hi in (("zoom", 3, 13), ("width", 256, 1024), ("height", 128, 768),
                        ("small_width", 64, 512), ("small_height", 32, 384), ("small_colors", 8, 256),
                        ("max_calls_per_minute", 1, 60), ("rotate_seconds", 3, 600),
                        ("max_tile_bytes", 1024, 2097152)):
        if not isinstance(m[key], int) or isinstance(m[key], bool) or not lo <= m[key] <= hi:
            raise ValueError("maps.%s must be an integer %d-%d" % (key, lo, hi))
    if not _num(m["refresh_minutes"]) or m["refresh_minutes"] < 10:
        raise ValueError("maps.refresh_minutes must be at least 10")
    if not _num(m["stale_after_minutes"]) or m["stale_after_minutes"] <= 0:
        raise ValueError("maps.stale_after_minutes must be positive")
    if not _num(m["timeout_seconds"]) or not 0 < m["timeout_seconds"] <= 60:
        raise ValueError("maps.timeout_seconds must be between 0 and 60")
    for key in ("overlay_opacity", "base_brightness"):
        if not _num(m[key]) or not 0 < m[key] <= 1:
            raise ValueError("maps.%s must be a number above 0 and at most 1" % key)
    for key in ("contrast", "saturation"):
        if not _num(m[key]) or not 0.5 <= m[key] <= 3:
            raise ValueError("maps.%s must be a number 0.5-3" % key)
    for key in ("env_file", "cache_dir"):
        if not isinstance(m[key], str) or not m[key]:
            raise ValueError("maps.%s must be a path string" % key)


def load_api_key(env_path):
    """Return (key, problem). Exactly one is None. Messages never contain the key."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        try:
            fd = os.open(env_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, "not_configured"
        except OSError:
            return None, "insecure_file"
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
                return None, "insecure_file"
            raw = os.read(fd, 8192).decode("utf-8", "replace")
        finally:
            os.close(fd)
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, sep, value = line.partition("=")
            if sep and name.strip() == KEY_ENV:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                key = value.strip()
        if not key:
            return None, "not_configured"
    if not KEY_RE.match(key):
        return None, "invalid_key_format"
    return key, None


def world_pixel(lat, lon, zoom):
    """Web-Mercator pixel coordinates of a point at a zoom level."""
    scale = TILE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * scale
    lat = max(-85.0511, min(85.0511, lat))
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


class FetchError(Exception):
    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal  # fatal: stop the whole cycle (bad key, rate limited)


class RateLimiter:
    """Spaces calls so no more than `per_minute` start in any minute."""

    def __init__(self, per_minute, clock=time.monotonic, sleep=time.sleep):
        self.gap = 60.0 / per_minute
        self._next = 0.0
        self._clock = clock
        self._sleep = sleep

    def wait(self):
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.gap


class MapService:
    def __init__(self, mcfg, lat, lon, label, base_dir, user_agent):
        self.cfg = mcfg
        self.user_agent = user_agent
        self.lat, self.lon, self.label = lat, lon, label
        self.configured = bool(mcfg["enabled"]) and lat is not None
        self.cache_dir = os.path.join(base_dir, mcfg["cache_dir"])
        self.base_dir_tiles = os.path.join(self.cache_dir, "base")
        self.env_path = os.path.join(base_dir, mcfg["env_file"])
        self._lock = threading.Lock()
        self._layers = {}  # name -> updated_ms
        self._last_attempt_ms = None
        self._last_error = None
        self._failures = 0
        self._calls = 0
        self._stop = threading.Event()
        self._thread = None
        self._limiter = RateLimiter(mcfg["max_calls_per_minute"])
        self._load_existing()

    # --- geometry ----------------------------------------------------------
    def crop_box(self):
        z = self.cfg["zoom"]
        cx, cy = world_pixel(self.lat, self.lon, z)
        left = cx - self.cfg["width"] / 2.0
        top = cy - self.cfg["height"] / 2.0
        return left, top, left + self.cfg["width"], top + self.cfg["height"]

    def tile_range(self):
        left, top, right, bottom = self.crop_box()
        return (int(left // TILE), int(top // TILE),
                int((right - 1) // TILE), int((bottom - 1) // TILE))

    # --- persistence -------------------------------------------------------
    def _png_path(self, layer, small=False):
        return os.path.join(self.cache_dir, layer + ("_small.png" if small else ".png"))

    def _load_existing(self):
        for layer in self.cfg["layers"]:
            try:
                os.stat(self._png_path(layer, small=True))
                self._layers[layer] = int(os.stat(self._png_path(layer)).st_mtime * 1000)
            except OSError:
                pass

    def path_for(self, name):
        """File path for a served map name like 'wind_new.png' or 'wind_new_small.png', or None."""
        small = name.endswith("_small.png")
        layer = name[:-10] if small else (name[:-4] if name.endswith(".png") else None)
        if layer in self.cfg["layers"] and layer in self._layers:
            path = self._png_path(layer, small)
            if os.path.isfile(path):
                return path
        return None

    # --- network -----------------------------------------------------------
    def _get(self, url, user_agent):
        req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "image/png"})
        limit = self.cfg["max_tile_bytes"]
        try:
            with urllib.request.urlopen(req, timeout=self.cfg["timeout_seconds"]) as resp:
                body = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise FetchError("HTTP %d: the API key was rejected or the plan lacks map access" % exc.code, True)
            if exc.code == 429:
                raise FetchError("HTTP 429: rate limited", True)
            raise FetchError("HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise FetchError("network error: %s" % getattr(exc, "reason", exc.__class__.__name__))
        if len(body) > limit:
            raise FetchError("tile larger than %d bytes" % limit)
        return body

    def _open_png(self, body):
        try:
            img = Image.open(io.BytesIO(body))
            img.load()
            return img.convert("RGBA")
        except Exception:
            raise FetchError("response was not a valid image")

    def _base_tile(self, z, x, y, ua):
        n = 2 ** z
        x %= n
        path = os.path.join(self.base_dir_tiles, "%d_%d_%d.png" % (z, x, y))
        try:
            with open(path, "rb") as f:
                return Image.open(io.BytesIO(f.read())).convert("RGBA")
        except (OSError, ValueError):
            pass
        self._limiter.wait()
        body = self._get(OSM_TILE_URL % (z, x, y), ua)
        img = self._open_png(body)
        os.makedirs(self.base_dir_tiles, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        return img

    # --- rendering ---------------------------------------------------------
    def build_layer(self, layer, key, ua):
        z = self.cfg["zoom"]
        x0, y0, x1, y1 = self.tile_range()
        n = 2 ** z
        base = Image.new("RGBA", ((x1 - x0 + 1) * TILE, (y1 - y0 + 1) * TILE))
        over = Image.new("RGBA", base.size, (0, 0, 0, 0))
        for ty in range(y0, y1 + 1):
            if not 0 <= ty < n:
                continue
            for tx in range(x0, x1 + 1):
                base.paste(self._base_tile(z, tx, ty, ua), ((tx - x0) * TILE, (ty - y0) * TILE))
                if self._stop.is_set():
                    raise FetchError("stopping")
                self._limiter.wait()
                self._count_call()
                tile = self._open_png(self._get(OWM_TILE_URL % (layer, z, tx % n, ty, key), ua))
                over.paste(tile, ((tx - x0) * TILE, (ty - y0) * TILE))
        left, top, right, bottom = self.crop_box()
        box = tuple(int(round(v)) for v in (left - x0 * TILE, top - y0 * TILE,
                                            right - x0 * TILE, bottom - y0 * TILE))
        base, over = base.crop(box), over.crop(box)
        base = ImageEnhance.Brightness(base.convert("RGB")).enhance(self.cfg["base_brightness"])
        if self.cfg["overlay_opacity"] < 1:
            alpha = over.getchannel("A").point(lambda a: int(a * self.cfg["overlay_opacity"]))
            over.putalpha(alpha)
        out = base.convert("RGBA")
        out.alpha_composite(over)
        draw = ImageDraw.Draw(out)
        mx, my = out.width // 2, out.height // 2
        draw.ellipse((mx - 6, my - 6, mx + 6, my + 6), outline=(255, 255, 255, 255), width=2)
        draw.ellipse((mx - 2, my - 2, mx + 2, my + 2), fill=(248, 81, 73, 255))
        full = out.convert("RGB")
        # the tablet's LCD is washed out: stretch contrast and colour of the finished map
        full = ImageEnhance.Contrast(full).enhance(self.cfg["contrast"])
        full = ImageEnhance.Color(full).enhance(self.cfg["saturation"])
        sw, sh = self.cfg["small_width"], self.cfg["small_height"]
        # crop the centre of the full image to the small image's aspect (e.g. square), then shrink
        cw = min(full.width, int(round(full.height * sw / float(sh))))
        ch = min(full.height, int(round(cw * sh / float(sw))))
        cl, ct = (full.width - cw) // 2, (full.height - ch) // 2
        small = full.crop((cl, ct, cl + cw, ct + ch)).resize((sw, sh), Image.LANCZOS)
        sd = ImageDraw.Draw(small)
        sx, sy = small.width // 2, small.height // 2
        sd.ellipse((sx - 4, sy - 4, sx + 4, sy + 4), outline=(255, 255, 255), width=1)
        sd.ellipse((sx - 1, sy - 1, sx + 1, sy + 1), fill=(248, 81, 73))
        return (full.quantize(colors=160, method=Image.Quantize.MEDIANCUT),
                small.quantize(colors=self.cfg["small_colors"], method=Image.Quantize.MEDIANCUT))

    def _count_call(self):
        with self._lock:
            self._calls += 1

    def _write_png(self, layer, img, small=False):
        os.makedirs(self.cache_dir, exist_ok=True)
        path = self._png_path(layer, small)
        tmp = path + ".tmp"
        img.save(tmp, "PNG", optimize=True)
        os.replace(tmp, path)

    def refresh_once(self):
        """Refresh every layer. Returns True if all layers were updated. Never raises."""
        if not self.configured:
            return False
        key, problem = load_api_key(self.env_path)
        started = int(time.time() * 1000)
        if problem:
            self._record(started, "API key problem: " + problem, ok=False)
            return False
        ok_count = 0
        error = None
        for layer in self.cfg["layers"]:
            if self._stop.is_set():
                return False
            try:
                full, small = self.build_layer(layer, key, self.user_agent)
                self._write_png(layer, small, small=True)
                self._write_png(layer, full)
            except FetchError as exc:
                error = "%s: %s" % (layer, exc)
                if exc.fatal:
                    break
                continue
            except Exception:
                log.exception("maps: unexpected error on %s", layer)
                error = "%s: internal error" % layer
                continue
            with self._lock:
                self._layers[layer] = int(time.time() * 1000)
            ok_count += 1
        all_ok = ok_count == len(self.cfg["layers"])
        self._record(started, error, ok=all_ok)
        if all_ok:
            log.info("maps: updated %d layers", ok_count)
        else:
            log.warning("maps: %d/%d layers updated; %s", ok_count, len(self.cfg["layers"]), error)
        return all_ok

    def _record(self, started, error, ok):
        with self._lock:
            self._last_attempt_ms = started
            self._last_error = None if ok else error
            self._failures = 0 if ok else self._failures + 1

    def next_delay(self):
        refresh = self.cfg["refresh_minutes"] * 60
        with self._lock:
            failures = self._failures
        if failures == 0:
            return refresh
        return min(refresh, 300 * (2 ** (failures - 1)))  # 5, 10, 20 min, then refresh

    def snapshot(self):
        now = int(time.time() * 1000)
        stale_ms = self.cfg["stale_after_minutes"] * 60000
        with self._lock:
            layers = []
            for name in self.cfg["layers"]:
                if name in self._layers:
                    layers.append({"name": name, "title": LAYER_TITLES[name],
                                   "url": "/maps/%s.png" % name,
                                   "small_url": "/maps/%s_small.png" % name, "updated_ms": self._layers[name],
                                   "stale": now - self._layers[name] > stale_ms})
            if not self.configured:
                status = "unconfigured"
            elif layers:
                status = "ok"
            else:
                status = "error" if self._last_error else "loading"
            return {
                "configured": self.configured,
                "status": status,
                "label": self.label,
                "rotate_seconds": self.cfg["rotate_seconds"],
                "layers": layers,
                "error": self._last_error,
                "last_attempt_ms": self._last_attempt_ms,
                "calls_since_start": self._calls,
                "attribution": [
                    {"text": "Weather maps © OpenWeather", "url": "https://openweathermap.org/"},
                    {"text": "Map © OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"},
                ],
            }

    # --- thread ------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            self.refresh_once()
            if self._stop.wait(self.next_delay()):
                break

    def start(self):
        if not self.configured:
            log.info("maps: disabled or location not configured; not fetching")
            return
        self._thread = threading.Thread(target=self._run, name="maps", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
