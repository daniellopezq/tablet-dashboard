"""Dashboard configuration: defaults, loading config.json, validation.

Each feature module owns its own section defaults and validator; this module merges them.
"""
import json
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import admin as admin_mod
import assistant as assistant_mod
import cameras as cameras_mod
import gcal as gcal_mod
import health as health_mod
import home as home_mod
import localai as localai_mod
import maps as maps_mod
import pgstore as pgstore_mod
import tts as tts_mod
import voice as voice_mod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_CONFIG = {
    "bind": "0.0.0.0",
    "port": 8099,
    "https_port": None,
    "tls_cert": "data/tls/server.crt",
    "tls_key": "data/tls/server.key",
    "timezone": "UTC",
    "clock_default_24h": True,
    "web_dir": "web",
    "modern_dir": "web-modern",
    "media_dir": "media",
    "ui": {
        "modern_enabled": True,
    },
    "weather": {
        "latitude": None,
        "longitude": None,
        "label": "",
        "timezone": "UTC",
        "temperature_unit": "celsius",
        "refresh_minutes": 15,
        "stale_after_minutes": 35,
        "timeout_seconds": 10,
        "max_response_bytes": 262144,
        "api_url": OPEN_METEO_URL,
    },
    "assistant": dict(assistant_mod.DEFAULT_ASSISTANT_CONFIG),
    "maps": dict(maps_mod.DEFAULT_MAPS_CONFIG),
    "voice": dict(voice_mod.DEFAULT_VOICE_CONFIG),
    "tts": json.loads(json.dumps(tts_mod.DEFAULT_TTS_CONFIG)),
    "calendar": dict(gcal_mod.DEFAULT_CALENDAR_CONFIG),
    "local_ai": dict(localai_mod.DEFAULT_LOCAL_AI_CONFIG),
    "health": dict(health_mod.DEFAULT_HEALTH_CONFIG),
    "kuma": dict(health_mod.DEFAULT_KUMA_CONFIG),
    "admin": dict(admin_mod.DEFAULT_ADMIN_CONFIG),
    "home": dict(home_mod.DEFAULT_HOME_CONFIG),
    "cameras": dict(cameras_mod.DEFAULT_CAMERAS_CONFIG),
    "database": dict(pgstore_mod.DEFAULT_DATABASE_CONFIG),
}


class ConfigError(Exception):
    pass


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_zone(name, key):
    if not isinstance(name, str) or not name:
        raise ConfigError("%s must be an IANA timezone name" % key)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("%s: unknown timezone %r" % (key, name))


def load_config(path):
    """Return the merged, validated configuration. Keys starting with '_' are comments."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                user = json.load(f)
            except json.JSONDecodeError as exc:
                raise ConfigError("%s: invalid JSON: %s" % (path, exc))
        if not isinstance(user, dict):
            raise ConfigError("config root must be a JSON object")
        for key, value in user.items():
            if key.startswith("_"):
                continue
            if key not in cfg:
                raise ConfigError("unknown config key: %s" % key)
            if isinstance(DEFAULT_CONFIG[key], dict):
                if not isinstance(value, dict):
                    raise ConfigError("%s must be a JSON object" % key)
                for wkey, wvalue in value.items():
                    if wkey.startswith("_"):
                        continue
                    if wkey not in cfg[key]:
                        raise ConfigError("unknown config key: %s.%s" % (key, wkey))
                    cfg[key][wkey] = wvalue
            else:
                cfg[key] = value
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    if not isinstance(cfg["port"], int) or not 1 <= cfg["port"] <= 65535:
        raise ConfigError("port must be an integer 1-65535")
    hp = cfg["https_port"]
    if hp is not None and (not isinstance(hp, int) or isinstance(hp, bool) or not 1 <= hp <= 65535 or hp == cfg["port"]):
        raise ConfigError("https_port must be null or an integer 1-65535 different from port")
    for key in ("tls_cert", "tls_key"):
        if not isinstance(cfg[key], str) or not cfg[key]:
            raise ConfigError("%s must be a path" % key)
    if not isinstance(cfg["bind"], str):
        raise ConfigError("bind must be a string")
    if not isinstance(cfg["clock_default_24h"], bool):
        raise ConfigError("clock_default_24h must be true or false")
    if not isinstance(cfg["modern_dir"], str) or not cfg["modern_dir"]:
        raise ConfigError("modern_dir must be a path")
    if not isinstance(cfg["ui"]["modern_enabled"], bool):
        raise ConfigError("ui.modern_enabled must be true or false")
    _load_zone(cfg["timezone"], "timezone")

    w = cfg["weather"]
    lat, lon = w["latitude"], w["longitude"]
    if (lat is None) != (lon is None):
        raise ConfigError("weather.latitude and weather.longitude must both be set or both be null")
    if lat is not None:
        if not _is_number(lat) or not -90 <= lat <= 90:
            raise ConfigError("weather.latitude must be a number between -90 and 90")
        if not _is_number(lon) or not -180 <= lon <= 180:
            raise ConfigError("weather.longitude must be a number between -180 and 180")
    if not isinstance(w["label"], str):
        raise ConfigError("weather.label must be a string")
    _load_zone(w["timezone"], "weather.timezone")
    if w["temperature_unit"] not in ("celsius", "fahrenheit"):
        raise ConfigError("weather.temperature_unit must be 'celsius' or 'fahrenheit'")
    if not _is_number(w["refresh_minutes"]) or w["refresh_minutes"] < 5:
        raise ConfigError("weather.refresh_minutes must be at least 5")
    if not _is_number(w["stale_after_minutes"]) or w["stale_after_minutes"] <= 0:
        raise ConfigError("weather.stale_after_minutes must be positive")
    if not _is_number(w["timeout_seconds"]) or not 0 < w["timeout_seconds"] <= 60:
        raise ConfigError("weather.timeout_seconds must be between 0 and 60")
    if not isinstance(w["max_response_bytes"], int) or not 1024 <= w["max_response_bytes"] <= 4194304:
        raise ConfigError("weather.max_response_bytes must be an integer 1024-4194304")
    if not isinstance(w["api_url"], str) or not re.match(r"^https?://", w["api_url"]):
        raise ConfigError("weather.api_url must be an http(s) URL")
    try:
        assistant_mod.validate_assistant_config(cfg["assistant"])
        maps_mod.validate_maps_config(cfg["maps"])
        voice_mod.validate_voice_config(cfg["voice"])
        tts_mod.validate_tts_config(cfg["tts"])
        gcal_mod.validate_calendar_config(cfg["calendar"])
        localai_mod.validate_local_ai_config(cfg["local_ai"])
        health_mod.validate_health_config(cfg["health"])
        health_mod.validate_kuma_config(cfg["kuma"])
        admin_mod.validate_admin_config(cfg["admin"])
        home_mod.validate_home_config(cfg["home"])
        cameras_mod.validate_cameras_config(cfg["cameras"])
        pgstore_mod.validate_database_config(cfg["database"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ConfigError(str(exc))

