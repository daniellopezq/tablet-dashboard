"""Files: the index page (classic or modern view), the admin page, maps, media and static assets."""
import os
import urllib.parse

import uidetect
from httpcore import MEDIA_TYPES, STATIC_TYPES, resolve_under

from . import admin_api


def index(h, path, head):
    """Classic page for old browsers (the tablet), modern page for phones and PCs."""
    ui, cookie = uidetect.choose_ui(h.headers.get("User-Agent", ""),
                                    urllib.parse.urlsplit(h.path).query,
                                    h.headers.get("Cookie", ""),
                                    h.app.cfg["ui"]["modern_enabled"])
    root = h.app.modern_root if ui == uidetect.MODERN else h.app.web_root
    extra = {"Vary": "User-Agent, Cookie"}
    if cookie:
        extra["Set-Cookie"] = cookie
    return h.send_file(os.path.join(root, "index.html"), STATIC_TYPES[".html"], head,
                       ranges=False, extra=extra)


def admin_page(h, path, head):
    """/admin, /admin/ and /admin.* assets from web/: https + LAN only, otherwise a 404 hint."""
    if not admin_api.allowed(h):
        port = h.app.cfg["https_port"]
        return h.send_text(404, "Not found. The admin page is available on the local network at "
                           "https://<server>:%s/admin" % port if port else "Not found", head)
    rel = "admin.html" if path in ("/admin", "/admin/") else path[1:]
    return _send_from(h, h.app.web_root, rel, STATIC_TYPES, head)


def maps(h, path, head):
    full = h.app.maps.path_for(path[len("/maps/"):])
    if not full:
        return h.send_text(404, "Not found", head)
    return h.send_file(full, "image/png", head, ranges=False)


def media(h, path, head):
    return _send_from(h, h.app.media_root, path[len("/media/"):], MEDIA_TYPES, head, ranges=True)


def modern(h, path, head):
    return _send_from(h, h.app.modern_root, path[len("/m/"):], STATIC_TYPES, head)


def static(h, path, head):
    return _send_from(h, h.app.web_root, path[1:], STATIC_TYPES, head)


def _send_from(h, root, rel, types, head, ranges=False):
    full = resolve_under(root, rel)
    ctype = types.get(os.path.splitext(full)[1].lower()) if full else None
    if not ctype:
        return h.send_text(404, "Not found", head)
    return h.send_file(full, ctype, head, ranges=ranges)
