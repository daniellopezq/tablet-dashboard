"""Admin panel: constants, password hashing, the keys and settings schemas, small helpers."""
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime

import envfile
import gcal as gcal_mod
import maps as maps_mod

log = logging.getLogger("dashboard.admin")

DEFAULT_ADMIN_CONFIG = {
    "enabled": True,
    "password_file": "data/admin.json",
    "session_idle_hours": 8,
    "session_max_days": 7,
    "max_body_bytes": 65536,
}
COOKIE = "dash_admin"
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 5
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
MIN_PASSWORD = 10
LOG_FILES = {"dashboard": "logs/dashboard.log", "llama": "logs/llama-server.log", "whisper": "logs/whisper-server.log"}
BACKUP_EXCLUDE = {".env", "data", "backups", "logs", "media", "run", "__pycache__", ".git"}


def validate_admin_config(a):
    if not isinstance(a["enabled"], bool):
        raise ValueError("admin.enabled must be true or false")
    if not isinstance(a["password_file"], str) or not a["password_file"]:
        raise ValueError("admin.password_file must be a path string")
    for key, lo, hi in (("session_idle_hours", 0.1, 168), ("session_max_days", 0.1, 90),
                        ("max_body_bytes", 1024, 1048576)):
        v = a[key]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError("admin.%s must be a number %s-%s" % (key, lo, hi))


# --- password -------------------------------------------------------------------------------

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=64 * 1024 * 1024, **SCRYPT)
    return {"scheme": "scrypt", "n": SCRYPT["n"], "r": SCRYPT["r"], "p": SCRYPT["p"],
            "salt": salt.hex(), "hash": dk.hex(),
            "set_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def verify_password(password, rec):
    try:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(rec["salt"]), n=int(rec["n"]),
                            r=int(rec["r"]), p=int(rec["p"]), dklen=len(rec["hash"]) // 2,
                            maxmem=64 * 1024 * 1024)
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), rec["hash"])


def load_password(path):
    """The stored hash record, or None if no password is set (or the file is unsafe)."""
    try:
        raw, _ = envfile.read_private(path)
        rec = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.error("admin: %s is unreadable or not owner-only (mode 600); login disabled", path)
        return None
    return rec if isinstance(rec, dict) and rec.get("scheme") == "scrypt" else None


# --- keys -----------------------------------------------------------------------------------

KEYS = [
    {"name": "AI_GATEWAY_API_KEY", "label": "Vercel AI Gateway", "used_by": "Ask (cloud model)", "test": "gateway"},
    {"name": "GROQ_API_KEY", "aliases": ["GROQ"], "label": "Groq", "used_by": "Voice (cloud speech-to-text)",
     "test": "groq"},
    {"name": "OPENWEATHER_API_KEY", "label": "OpenWeatherMap", "used_by": "Maps (weather tiles)", "test": "owm",
     "regex": maps_mod.KEY_RE},
    {"name": "GOOGLE_CLIENT_ID", "label": "Google OAuth client ID", "used_by": "Calendar", "secret": False,
     "regex": gcal_mod.CLIENT_ID_RE},
    {"name": "GOOGLE_CLIENT_SECRET", "label": "Google OAuth client secret", "used_by": "Calendar", "test": "google"},
    {"name": "KUMA_API", "aliases": ["KUMA_API_KEY"], "label": "Uptime Kuma API key", "used_by": "Health (/metrics)",
     "test": "kuma"},
    {"name": "KUMA_PUSH_URL", "label": "Uptime Kuma push URL", "used_by": "Health (heartbeat)", "kind": "url",
     "test": "kuma_push"},
    {"name": "HA_TOKEN", "label": "Home Assistant long-lived token", "used_by": "Home tab", "test": "ha"},
]
KEYS_BY_NAME = {k["name"]: k for k in KEYS}


def _mask(value, secret=True):
    if not secret:
        return value
    return "…" + value[-4:] if len(value) >= 12 else "set"


# --- settings schema (what the Settings page may edit) --------------------------------------
# (key, type, label, restart_needed). Types: bool, int, num, numnull, str, strnull, enum:a|b, list, json

SETTINGS = collections.OrderedDict([
    ("general", ("General", [
        ("timezone", "str", "Time zone (IANA name)", True),
        ("clock_default_24h", "bool", "24-hour clock by default", True)])),
    ("weather", ("Weather", [
        ("label", "str", "Place name", True),
        ("latitude", "numnull", "Latitude", True),
        ("longitude", "numnull", "Longitude", True),
        ("timezone", "str", "Forecast time zone", True),
        ("temperature_unit", "enum:celsius|fahrenheit", "Temperature unit", True),
        ("refresh_minutes", "num", "Refresh every (minutes, ≥ 5)", True)])),
    ("assistant", ("Ask (assistant)", [
        ("enabled", "bool", "Enabled", False),
        ("max_output_tokens", "int", "Max answer tokens (cloud)", False),
        ("disable_thinking", "bool", "Turn model thinking off", False),
        ("min_interval_seconds", "num", "Seconds between questions", False),
        ("max_per_hour", "int", "Questions per hour", False),
        ("max_per_day", "int", "Questions per day", False),
        ("price_per_million_input_usd", "numnull", "Price per million input tokens (USD, for estimates)", False),
        ("price_per_million_output_usd", "numnull", "Price per million output tokens (USD)", False),
        ("home_control", "bool", "Ask the house: Home tiles and camera questions", False),
        ("memory_turns", "int", "Remember this many messages per conversation (0 = none)", False),
        ("memory_seconds", "int", "Forget a conversation after (seconds idle)", False)])),
    ("tts", ("Spoken answers", [
        ("enabled", "bool", "Enabled", False),
        ("length_scale_es", "num", "Spanish speaking speed (1 = normal, 0.85 = 15% faster)", False),
        ("length_scale_en", "num", "English speaking speed (1 = normal)", False),
        ("sentence_silence", "num", "Pause after each sentence (seconds)", False),
        ("max_chars", "int", "Speak at most this many characters", False),
        ("phrase_bank", "bool", "Saved phrases (instant replies to home commands)", False),
        ("engine", "str", "Voice engine: google (Piper as fallback) or piper", False),
        ("google_voice_es", "str", "Google Spanish voice", False),
        ("google_voice_en", "str", "Google English voice", False),
        ("google_speaking_rate_es", "num", "Google Spanish speed (1 = normal, 1.15 = 15% faster)", False),
        ("google_speaking_rate_en", "num", "Google English speed (1 = normal)", False),
        ("google_monthly_chars", "int", "Google characters per month (free tier is 1,000,000)", False)])),
    ("voice", ("Voice", [
        ("enabled", "bool", "Enabled", False),
        ("auto_ask", "bool", "Ask automatically after transcribing", False),
        ("language", "strnull", "Language code (empty = auto)", False),
        ("min_interval_seconds", "num", "Seconds between recordings", False),
        ("max_per_hour", "int", "Recordings per hour", False),
        ("max_per_day", "int", "Recordings per day", False)])),
    ("cameras", ("Cameras", [
        ("ai_refresh_enabled", "bool", "Background snapshots for the assistant (cameras with ai_refresh_seconds)",
         False)])),
    ("maps", ("Maps", [
        ("enabled", "bool", "Enabled", True),
        ("zoom", "int", "Zoom (3-13)", True),
        ("rotate_seconds", "int", "Rotate every (seconds)", True),
        ("refresh_minutes", "num", "Refresh every (minutes, ≥ 10)", True),
        ("base_brightness", "num", "Base map brightness (0-1)", True),
        ("contrast", "num", "Contrast (0.5-3)", True),
        ("saturation", "num", "Saturation (0.5-3)", True),
        ("overlay_opacity", "num", "Overlay opacity (0-1)", True)])),
    ("calendar", ("Calendar", [
        ("enabled", "bool", "Enabled", True),
        ("calendar_ids", "list", "Calendar IDs (one per line)", False),
        ("days_ahead", "int", "Days ahead (1-31)", False),
        ("show_location", "bool", "Show locations", False),
        ("hide_declined", "bool", "Hide declined events", False)])),
    ("health", ("Health checks", [
        ("enabled", "bool", "Enabled", True),
        ("interval_seconds", "num", "Check every (seconds)", False),
        ("timeout_seconds", "num", "Timeout per check (seconds)", False),
        ("google_token_max_age_days", "numnull", "Google sign-in lifetime in days (7 while the app is in Testing; "
                                                 "empty once published)", False),
        ("disk_warn_gb", "num", "Warn below this much free disk (GB)", False),
        ("targets", "json", "Targets: [{\"name\",\"type\":\"tcp\",\"host\",\"port\"} or "
                            "{\"name\",\"type\":\"http\",\"url\"}]", False)])),
    ("kuma", ("Uptime Kuma", [
        ("enabled", "bool", "Enabled", False),
        ("base_url", "str", "Kuma URL, e.g. http://192.168.1.10:3001", False),
        ("status_page_slug", "str", "Status page slug (/status/<slug>)", False),
        ("use_metrics", "bool", "Read /metrics instead (needs KUMA_API)", False),
        ("push_enabled", "bool", "Send a heartbeat to KUMA_PUSH_URL", False)])),
])
AI_KEYS = {
    "assistant": ("model", "max_output_tokens", "fallback_enabled", "fallback_max_output_tokens",
                  "fallback_timeout_seconds", "no_response_seconds", "cloud_cooldown_seconds"),
    "voice": ("model", "fallback_enabled", "fallback_timeout_seconds", "no_response_seconds",
              "cloud_cooldown_seconds"),
}


def coerce(ftype, value):
    """Convert a form value to the config type. Raises ValueError."""
    if ftype == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be true or false")
        return value
    if ftype in ("int", "num", "numnull"):
        if ftype == "numnull" and (value is None or value == ""):
            return None
        if isinstance(value, str):
            value = value.strip()
            try:
                value = int(value) if re.match(r"^-?\d+$", value) else float(value)
            except ValueError:
                raise ValueError("must be a number")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("must be a number")
        if ftype == "int":
            if value != int(value):
                raise ValueError("must be a whole number")
            value = int(value)
        return value
    if ftype in ("str", "strnull"):
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > 500:
            raise ValueError("must be text")
        value = value.strip()
        return None if ftype == "strnull" and not value else value
    if ftype.startswith("enum:"):
        if value not in ftype[5:].split("|"):
            raise ValueError("must be one of " + ftype[5:].replace("|", ", "))
        return value
    if ftype == "list":
        if isinstance(value, str):
            value = [v.strip() for v in value.splitlines() if v.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("must be a list")
        return value
    if ftype == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError("invalid JSON: %s" % exc)
        return value
    raise ValueError("unknown field type")


class ApiError(Exception):
    def __init__(self, status, message, extra=None):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra or {}


def _tail(path, max_lines, max_bytes=512 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]  # first line is probably cut
    return lines[-max_lines:]


def _clean_err(exc):
    reason = getattr(exc, "reason", None) or exc
    return str(getattr(reason, "strerror", None) or getattr(exc, "category", None) or reason)[:160]


def _age(ms):
    if not ms:
        return "never updated"
    s = time.time() - ms / 1000.0
    if s < 90:
        return "updated %d s ago" % s
    if s < 5400:
        return "updated %d min ago" % (s / 60)
    return "updated %.1f h ago" % (s / 3600)
