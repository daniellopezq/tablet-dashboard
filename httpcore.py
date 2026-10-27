"""HTTP building blocks shared by the dashboard handler and the route modules.

Content types, the page CSP, safe path resolution, Range parsing, and ResponseMixin: the
send_* helpers and request-body checks that every route uses.
"""
import email.utils
import ipaddress
import json
import logging
import os
import re
import urllib.parse


STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".crt": "application/x-x509-ca-cert",  # public CA certificate for optional install on the tablet
}
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
}

CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'")

log = logging.getLogger("dashboard")


def resolve_under(root, rel):
    """Map a URL path (still percent-encoded) to a regular file inside root, or None.

    Rejects traversal, hidden segments, backslashes, NUL, directories and symlinks
    that resolve outside root.
    """
    path = urllib.parse.unquote(rel)
    if "\x00" in path or "\\" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or any(p.startswith(".") for p in parts):
        return None
    real = os.path.realpath(os.path.join(root, *parts))
    if not real.startswith(root + os.sep) or not os.path.isfile(real):
        return None
    return real


def parse_range(header, size):
    """Return (start, end) inclusive, None to serve the whole file, or 'unsatisfiable'."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None  # absent, malformed or multi-range: ignore and send 200
    if m.group(1) == "":
        suffix = int(m.group(2))
        if suffix == 0 or size == 0:
            return "unsatisfiable"
        return max(0, size - suffix), size - 1
    start = int(m.group(1))
    if start >= size:
        return "unsatisfiable"
    end = int(m.group(2)) if m.group(2) else size - 1
    if end < start:
        return None
    return start, min(end, size - 1)



# --- request checks -----------------------------------------------------------------------

def is_lan_client(ip, trusted=()):
    """True for loopback and private-range clients, plus the exact addresses in `trusted`
    (assistant.trusted_ips, e.g. Tailscale devices)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    return any(ipaddress.ip_address(t) == addr for t in trusted)


def _host_only(value):
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:].split("]")[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def check_origin(headers, allowed_hosts):
    """Return an error string for cross-origin or rebinding-suspect requests, else None."""
    host = headers.get("Host", "")
    hostname = _host_only(host)
    if not hostname:
        return "missing host"
    is_ip = True
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip = False
    if not is_ip and hostname != "localhost" and hostname not in [h.lower() for h in allowed_hosts]:
        return "host not allowed"
    origin = headers.get("Origin")
    if origin is not None:
        if origin == "null":
            return "cross-origin"
        parts = urllib.parse.urlsplit(origin)
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != host.strip().lower():
            return "cross-origin"
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return "cross-site"
    return None


class ResponseMixin:
    """Mixed into the BaseHTTPRequestHandler subclass. Expects self.server.app."""

    @property
    def app(self):
        return self.server.app

    # --- request helpers ---------------------------------------------------
    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def content_type(self):
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    def is_lan(self):
        return is_lan_client(self.client_address[0], self.app.assistant.cfg["trusted_ips"])

    def cross_origin(self):
        return check_origin(self.headers, self.app.assistant.cfg["allowed_hosts"])

    def read_body(self, max_bytes, empty_msg, too_large_msg, incomplete_msg):
        """Read a Content-Length body for the assistant-style API. Returns the bytes, or None
        after sending the error response (or when the client went away)."""
        if self.headers.get("Transfer-Encoding"):
            return self.send_api_error(411, "length_required", "Chunked bodies are not accepted.")
        length = self.headers.get("Content-Length", "")
        if not length.isdigit():
            return self.send_api_error(411, "length_required", "Content-Length is required.")
        length = int(length)
        if length == 0:
            return self.send_api_error(400, "bad_request", empty_msg)
        if length > max_bytes:
            return self.send_api_error(413, "too_large", too_large_msg)
        try:
            raw = self.rfile.read(length)
        except OSError:
            return None
        if len(raw) != length:
            return self.send_api_error(400, "bad_request", incomplete_msg)
        return raw

    # --- responses ---------------------------------------------------------
    def method_not_allowed(self):
        # The request body (if any) is never read, so the connection cannot be reused.
        self.close_connection = True
        self.send_bytes(405, b"Method not allowed\n", "text/plain; charset=utf-8",
                        head=False, extra={"Allow": "GET, HEAD"})

    def common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_bytes(self, status, body, ctype, head, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def send_text(self, status, text, head):
        self.send_bytes(status, (text + "\n").encode("utf-8"), "text/plain; charset=utf-8",
                        head, {"Cache-Control": "no-store"})

    def send_json(self, obj, head, status=200, extra=None):
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        headers.update(extra or {})
        self.send_bytes(status, body, "application/json; charset=utf-8", head, headers)

    def send_api_error(self, status, code, message):
        """Error in the {"ok": false, "error": {"code", "message"}} shape the tablet page reads."""
        self.send_json({"ok": False, "error": {"code": code, "message": message}}, False, status=status)

    def send_result(self, status, body):
        """A service's (status, body) result, with Retry-After when the body asks for one."""
        extra = {}
        retry = body.get("error", {}).get("retry_after") if not body.get("ok") else None
        if retry:
            extra["Retry-After"] = str(retry)
        self.send_json(body, False, status=status, extra=extra)

    def send_file(self, full, ctype, head, ranges, extra=None):
        try:
            f = open(full, "rb")
        except OSError:
            return self.send_text(404, "Not found", head)
        with f:
            st = os.fstat(f.fileno())
            size = st.st_size
            span = parse_range(self.headers.get("Range"), size) if ranges else None
            if span == "unsatisfiable":
                return self.send_bytes(416, b"", "text/plain", head,
                                       {"Content-Range": "bytes */%d" % size, "Accept-Ranges": "bytes"})
            start, end = span if span else (0, size - 1)
            length = end - start + 1 if size else 0
            self.send_response(206 if span else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Last-Modified", email.utils.formatdate(st.st_mtime, usegmt=True))
            self.common_headers()
            if ranges:
                self.send_header("Accept-Ranges", "bytes")
                if span:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            else:
                self.send_header("Cache-Control", "no-cache")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
                self.send_header("X-Frame-Options", "DENY")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if head:
                return
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    # --- logging -----------------------------------------------------------
    def log_request(self, code="-", size="-"):
        # Routine successful API polls would dominate the log; record everything else.
        path = getattr(self, "path", "")
        if path.startswith("/api/") and isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)

    def log_error(self, fmt, *args):
        # Idle keep-alive connections time out all the time; that's not an error.
        if fmt.startswith("Request timed out"):
            log.debug("%s %s", self.address_string(), fmt % args)
            return
        super().log_error(fmt, *args)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)
