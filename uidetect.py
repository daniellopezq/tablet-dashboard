"""Pick the classic (tablet, ES5) or modern (phone/PC, ES2020) front end for GET /.

Order: ?ui= override (remembered in a cookie unless &once=1), then the ui cookie, then the
User-Agent. Old engines get the classic UI; unknown ones get the modern UI,
whose boot script falls back to classic if the browser can't run it.
Python 3 standard library only.
"""

import re
import urllib.parse
from http.cookies import CookieError, SimpleCookie

CLASSIC, MODERN = "classic", "modern"
COOKIE = "ui"
COOKIE_MAX_AGE = 365 * 24 * 3600

# Minimum versions with ES2020 (optional chaining, ??) and ES modules.
MIN_CHROME = 80
MIN_FIREFOX = 78
MIN_SAFARI = 14

_CHROME = re.compile(r"(?:Chrome|Chromium|CriOS)/(\d+)")
_FIREFOX = re.compile(r"(?:Firefox|FxiOS)/(\d+)")
_SAFARI_VER = re.compile(r"Version/(\d+)")
_IOS = re.compile(r"(?:iPhone|iPad|iPod).*? OS (\d+)_")


def parse_ua(ua):
    """Return (engine, major_version or None) for a User-Agent string."""
    ua = ua or ""
    if "MSIE " in ua or "Trident/" in ua:
        return "ie", None
    ios = _IOS.search(ua)
    if ios:
        # Every iOS browser is WebKit, so the OS version is what counts.
        return "ios", int(ios.group(1))
    m = _CHROME.search(ua)
    if m:
        return "chrome", int(m.group(1))
    m = _FIREFOX.search(ua)
    if m:
        return "firefox", int(m.group(1))
    if "Safari/" in ua or "AppleWebKit/" in ua:
        m = _SAFARI_VER.search(ua)
        return "safari", int(m.group(1)) if m else None
    return "unknown", None


def ui_for_user_agent(ua):
    engine, ver = parse_ua(ua)
    if engine == "ie":
        return CLASSIC
    minimum = {"chrome": MIN_CHROME, "firefox": MIN_FIREFOX,
               "safari": MIN_SAFARI, "ios": MIN_SAFARI}.get(engine)
    if minimum is not None and ver is not None and ver < minimum:
        return CLASSIC
    return MODERN


def _cookie_value(cookie_header):
    if not cookie_header:
        return None
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel and morsel.value in (CLASSIC, MODERN) else None


def _set_cookie(value, max_age):
    return "%s=%s; Max-Age=%d; Path=/; SameSite=Lax; HttpOnly" % (COOKIE, value, max_age)


def choose_ui(user_agent, query, cookie_header, modern_enabled=True):
    """Return (ui, Set-Cookie header value or None)."""
    params = urllib.parse.parse_qs(query or "")
    wanted = params.get("ui", [""])[0].lower()
    if wanted in (CLASSIC, MODERN):
        ui = wanted if modern_enabled else CLASSIC
        # once=1: just this visit (links from unfinished modern sections), nothing remembered.
        if params.get("once", [""])[0] == "1":
            return ui, None
        return ui, _set_cookie(wanted, COOKIE_MAX_AGE)
    if wanted == "auto":
        ui = ui_for_user_agent(user_agent) if modern_enabled else CLASSIC
        return ui, _set_cookie("", 0)
    if not modern_enabled:
        return CLASSIC, None
    remembered = _cookie_value(cookie_header)
    if remembered:
        return remembered, None
    return ui_for_user_agent(user_agent), None
