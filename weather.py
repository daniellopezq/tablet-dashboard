"""Weather: Open-Meteo forecast, fetched in a background thread and served from a cache."""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import netsim

# WMO weather interpretation codes, per https://open-meteo.com/en/docs
WMO_CODES = {
    0: ("Clear sky", "clear"),
    1: ("Mainly clear", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Overcast", "cloud"),
    45: ("Fog", "fog"),
    48: ("Rime fog", "fog"),
    51: ("Light drizzle", "rain"),
    53: ("Drizzle", "rain"),
    55: ("Dense drizzle", "rain"),
    56: ("Freezing drizzle", "rain"),
    57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Rain showers", "rain"),
    81: ("Rain showers", "rain"),
    82: ("Violent showers", "rain"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "storm"),
    96: ("Thunderstorm, hail", "storm"),
    97: ("Heavy thunderstorm", "storm"),
    99: ("Thunderstorm, hail", "storm"),
}

log = logging.getLogger("dashboard.weather")


def now_ms():
    return int(time.time() * 1000)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nth(values, i):
    """values[i] for an optional daily series (None when missing or short)."""
    return values[i] if isinstance(values, list) and i < len(values) else None


class WeatherError(Exception):
    pass


class WeatherService:
    """Fetches Open-Meteo in a background thread; requests only ever read the cache."""

    def __init__(self, wcfg, user_agent):
        self.cfg = wcfg
        self.user_agent = user_agent
        self.configured = wcfg["latitude"] is not None
        self.unit = "F" if wcfg["temperature_unit"] == "fahrenheit" else "C"
        self._lock = threading.Lock()
        self._data = None
        self._updated_ms = None
        self._last_attempt_ms = None
        self._last_error = None
        self._failures = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.on_update = None  # called with each new normalized reading (the app saves it to the database)

    def build_url(self):
        w = self.cfg
        params = {
            "latitude": "%.4f" % w["latitude"],
            "longitude": "%.4f" % w["longitude"],
            "current": "temperature_2m,relative_humidity_2m,weather_code,is_day,precipitation,"
                       "wind_speed_10m,wind_gusts_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max",
            "timezone": w["timezone"],
            "temperature_unit": w["temperature_unit"],
            "forecast_days": "4",
        }
        return w["api_url"] + "?" + urllib.parse.urlencode(params, safe=",/")

    def _download(self):
        limit = self.cfg["max_response_bytes"]
        timeout = self.cfg["timeout_seconds"]
        deadline = time.monotonic() + timeout * 2
        req = urllib.request.Request(self.build_url(), headers={
            "User-Agent": self.user_agent, "Accept": "application/json"})
        if netsim.blocked("weather"):
            log.warning("weather: network error (offline test)")
            raise WeatherError("network error")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > limit:
                    raise WeatherError("response too large")
                chunks, total = [], 0
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise WeatherError("response too large")
                    if time.monotonic() > deadline:
                        raise WeatherError("timeout")
                    chunks.append(chunk)
        except urllib.error.HTTPError as exc:
            reason = ""
            try:
                body = json.loads(exc.read(4096).decode("utf-8", "replace"))
                reason = str(body.get("reason", ""))[:200]
            except Exception:
                pass
            log.warning("weather: HTTP %s %s", exc.code, reason)
            raise WeatherError("HTTP %d" % exc.code)
        except WeatherError:
            raise
        except (TimeoutError, OSError) as exc:
            # urllib.error.URLError is an OSError subclass; socket timeouts arrive here too.
            text = "timeout" if "timed out" in str(exc) else "network error"
            log.warning("weather: %s (%s)", text, exc)
            raise WeatherError(text)
        try:
            return json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise WeatherError("invalid response")

    def normalize(self, raw):
        try:
            cur, daily = raw["current"], raw["daily"]
            temp, hum, code = cur["temperature_2m"], cur["relative_humidity_2m"], cur["weather_code"]
            if not _is_number(temp) or not isinstance(code, int):
                raise WeatherError("invalid response")
            dates = daily["time"]
            days = []
            for i in range(min(4, len(dates))):
                dcode = daily["weather_code"][i]
                hi, lo = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
                if not isinstance(dates[i], str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", dates[i]):
                    raise WeatherError("invalid response")
                label, icon = WMO_CODES.get(dcode, ("Unknown", "unknown"))
                psum = _nth(daily.get("precipitation_sum"), i)
                pprob = _nth(daily.get("precipitation_probability_max"), i)
                days.append({
                    "date": dates[i],
                    "code": dcode if isinstance(dcode, int) else None,
                    "condition": label,
                    "icon": icon,
                    "max": hi if _is_number(hi) else None,
                    "min": lo if _is_number(lo) else None,
                    "precipitation": psum if _is_number(psum) else None,
                    "precipitation_probability": pprob if _is_number(pprob) else None,
                })
            if not days:
                raise WeatherError("invalid response")
        except (KeyError, TypeError, IndexError):
            raise WeatherError("invalid response")
        label, icon = WMO_CODES.get(code, ("Unknown", "unknown"))
        is_day = bool(cur.get("is_day", 1))
        if icon == "clear" and not is_day:
            icon = "clear-night"
        elif icon == "partly" and not is_day:
            icon = "partly-night"
        return {
            "current": {
                "temperature": temp,
                "humidity": hum if _is_number(hum) else None,
                "code": code,
                "condition": label,
                "icon": icon,
                "is_day": is_day,
                "observed": cur["time"] if isinstance(cur.get("time"), str) and len(cur["time"]) <= 20 else None,
                "precipitation": cur.get("precipitation") if _is_number(cur.get("precipitation")) else None,
                "wind_speed": cur.get("wind_speed_10m") if _is_number(cur.get("wind_speed_10m")) else None,
                "wind_gusts": cur.get("wind_gusts_10m") if _is_number(cur.get("wind_gusts_10m")) else None,
            },
            "today": days[0],
            "forecast": days[1:],
        }

    def fetch_once(self):
        """Fetch and cache weather. Returns True on success. Never raises."""
        if not self.configured:
            return False
        started = now_ms()
        try:
            data = self.normalize(self._download())
        except WeatherError as exc:
            with self._lock:
                self._last_attempt_ms = started
                self._last_error = str(exc)
                self._failures += 1
            return False
        except Exception:
            log.exception("weather: unexpected error")
            with self._lock:
                self._last_attempt_ms = started
                self._last_error = "internal error"
                self._failures += 1
            return False
        with self._lock:
            self._data = data
            self._updated_ms = now_ms()
            self._last_attempt_ms = started
            self._last_error = None
            self._failures = 0
        log.info("weather: updated")
        if self.on_update is not None:
            try:
                self.on_update(data)
            except Exception:  # saving history must never stop the weather
                log.exception("weather: saving the reading failed")
        return True

    def next_delay(self):
        refresh = self.cfg["refresh_minutes"] * 60
        with self._lock:
            failures = self._failures
        if failures == 0:
            return refresh
        return min(refresh, 60 * (2 ** (failures - 1)))  # 1, 2, 4, 8 min, then refresh

    def snapshot(self):
        with self._lock:
            snap = {
                "configured": self.configured,
                "label": self.cfg["label"],
                "unit": self.unit,
                "updated_ms": self._updated_ms,
                "last_attempt_ms": self._last_attempt_ms,
                "error": self._last_error,
                "stale": False,
                "data": self._data,
                "attribution": {"text": "Weather data by Open-Meteo.com",
                                "url": "https://open-meteo.com/",
                                "license": "CC BY 4.0"},
            }
            if not self.configured:
                snap["status"] = "unconfigured"
            elif self._data is None:
                snap["status"] = "error" if self._last_error else "loading"
            else:
                snap["status"] = "ok"
                age_ms = now_ms() - self._updated_ms
                snap["stale"] = (self._last_error is not None
                                 or age_ms > self.cfg["stale_after_minutes"] * 60000)
        return snap

    def _run(self):
        while not self._stop.is_set():
            self.fetch_once()
            self._wake.wait(self.next_delay())
            self._wake.clear()

    def wake(self):
        """Fetch now (admin page)."""
        self._wake.set()

    def start(self):
        if not self.configured:
            log.info("weather: location not configured; not fetching")
            return
        self._thread = threading.Thread(target=self._run, name="weather", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
