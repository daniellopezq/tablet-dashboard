"""/api/admin/*: thin HTTP wrapper around AdminService (admin/service.py), https + LAN only."""
import urllib.parse

import httpcore


def allowed(h):
    """Admin is served only on the https listener and only to LAN clients (assistant.trusted_ips
    does not apply: remote devices get the dashboard, not the admin panel)."""
    return (getattr(h.server, "is_tls", False) and httpcore.is_lan_client(h.client_address[0])
            and h.app.cfg["admin"]["enabled"])


def get(h, path, head):
    _handle(h, head, "GET")


def post(h):
    _handle(h, False, "POST")


def _handle(h, head, method):
    parts = urllib.parse.urlsplit(h.path)
    if method == "POST":
        h.close_connection = True  # early rejections leave the body unread
    if not allowed(h):
        return h.send_json({"ok": False, "error": "not found"}, head, status=404)
    body = b""
    if method == "POST":
        length = h.headers.get("Content-Length", "")
        if h.content_type() != "application/json":
            return h.send_json({"ok": False, "error": "Send application/json."}, False, status=415)
        if h.headers.get("Transfer-Encoding") or not length.isdigit():
            return h.send_json({"ok": False, "error": "Content-Length is required."}, False, status=411)
        if int(length) > h.app.cfg["admin"]["max_body_bytes"]:
            return h.send_json({"ok": False, "error": "Request is too large."}, False, status=413)
        try:
            body = h.rfile.read(int(length))
        except OSError:
            return
    status, obj, extra = h.app.admin.handle(method, parts.path, parts.query, h.headers, body,
                                            h.client_address[0])
    h.send_json(obj, head, status=status, extra=extra)
