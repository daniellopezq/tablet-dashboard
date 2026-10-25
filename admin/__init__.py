"""Admin panel backend for the tablet dashboard (single admin, password login).

Served only on the https listener and only to LAN clients (routes/admin_api.py enforces both before
calling in). Every API call except login needs a session cookie (HttpOnly, Secure, SameSite=Strict); every
POST also needs the X-Admin: 1 header, a same-origin Origin/Host, and a JSON body.

Secrets are write-only here: the keys page shows whether a key is set and its last 4 characters,
never the value. Nothing secret is logged.

Command line:
  python3 -m admin set-password    set or change the admin password (scripts/set-admin-password.sh)

Python 3 standard library only.
"""
from .core import (  # noqa: F401  (re-exported for server.py, config.py and tests)
    AI_KEYS,
    ApiError,
    BACKUP_EXCLUDE,
    COOKIE,
    DEFAULT_ADMIN_CONFIG,
    KEYS,
    KEYS_BY_NAME,
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW,
    LOG_FILES,
    MIN_PASSWORD,
    SCRYPT,
    SETTINGS,
    coerce,
    hash_password,
    load_password,
    validate_admin_config,
    verify_password,
)
from .service import AdminService  # noqa: F401
