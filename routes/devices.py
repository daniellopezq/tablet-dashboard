"""Home buttons and cameras: snapshot pictures, the MJPEG live view, pan/tilt and day/night.

All of these are LAN only (the GET routes are gated in routes/__init__.py).
"""
import json
import re

import home as home_mod

CAMERA_ID = r"([a-z0-9_-]{1,32})"


def get_camera_list(h, path, head):
    body = h.app.cameras.snapshot()
    body["laundry"] = h.app.laundry.status()
    h.send_json(body, head)


def get_camera_path(h, path, head):
    """/api/cameras/<id>/live.mjpg, /api/cameras/<id>/controls or /api/cameras/<id>.jpg."""
    query = h.query()
    m = re.match(r"^/api/cameras/%s/live\.mjpg$" % CAMERA_ID, path)
    if m:
        return stream_live(h, m.group(1), head, query.get("q", [""])[0] == "hd")
    m = re.match(r"^/api/cameras/%s/controls$" % CAMERA_ID, path)
    if m:
        try:
            return h.send_json(h.app.cameras.controls(m.group(1)), head)
        except home_mod.ActionError as exc:
            return h.send_json({"error": str(exc)}, head, status=exc.code)
    m = re.match(r"^/api/cameras/%s\.jpg$" % CAMERA_ID, path)
    if not m:
        return h.send_json({"error": "not found"}, head, status=404)
    version = query.get("v", [""])[0][:40]
    try:
        data, ctype = h.app.cameras.image(m.group(1), version)
    except home_mod.ActionError as exc:
        return h.send_json({"error": str(exc)}, head, status=exc.code)
    return h.send_bytes(200, data, ctype, head, {"Cache-Control": "private, max-age=300"})


def stream_live(h, cid, head, hd=False):
    """MJPEG (multipart/x-mixed-replace) live view of an RTSP camera, for an <img>. LAN only."""
    h.close_connection = True
    try:
        frames = h.app.cameras.live(cid, hd)
        first = next(frames)
    except home_mod.ActionError as exc:
        return h.send_json({"error": str(exc)}, head, status=exc.code)
    except StopIteration:
        feed_error = h.app.cameras.live_error(cid, hd)
        return h.send_json({"error": feed_error or "camera not answering"}, head, status=502)
    try:
        h.send_response(200)
        h.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "close")
        h.common_headers()
        h.end_headers()
        if head:
            return
        frame = first
        while True:
            h.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame))
            h.wfile.write(frame)
            h.wfile.write(b"\r\n")
            h.wfile.flush()
            frame = next(frames, None)
            if frame is None:
                return
    except OSError:  # the viewer went away (or stalled past the socket timeout)
        pass
    finally:
        frames.close()


def read_lan_json(h, what, need_id=True):
    """Checks for the small LAN-only JSON POST routes. Returns the body (a dict, with a string
    "id" unless need_id is False), or None after sending an error response."""
    h.close_connection = True  # early rejections leave the body unread
    if not h.is_lan():
        return h.send_api_error(403, "forbidden", "%s are available on the local network only." % what)
    if h.cross_origin():
        return h.send_api_error(403, "forbidden_origin", "Cross-origin requests are not allowed.")
    if h.content_type() != "application/json":
        return h.send_api_error(415, "unsupported_media_type", "Send application/json.")
    length = h.headers.get("Content-Length", "")
    if h.headers.get("Transfer-Encoding") or not length.isdigit():
        return h.send_api_error(411, "length_required", "Content-Length is required.")
    if int(length) > 1024:
        return h.send_api_error(413, "too_large", "Request is too large.")
    try:
        raw = h.rfile.read(int(length))
    except OSError:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        body = None
    if need_id and not (isinstance(body, dict) and isinstance(body.get("id"), str)):
        return h.send_api_error(400, "bad_request", "Send {\"id\": \"...\"}.")
    if not isinstance(body, dict):
        return h.send_api_error(400, "bad_request", "Send a JSON object.")
    return body


def post_home_action(h):
    """POST /api/home/action {"id": "<button id>"}: press one Home-tab button. LAN only."""
    req = read_lan_json(h, "Home controls")
    if req is None:
        return
    try:
        body = h.app.home.action(req["id"])
    except home_mod.ActionError as exc:
        return h.send_api_error(exc.code, "home_error", str(exc))
    h.send_json(body, False)


def post_camera_snapshot(h):
    """POST /api/cameras/snapshot {"id": "<camera id>"}: ask one camera for a new picture. LAN only."""
    req = read_lan_json(h, "Cameras")
    if req is None:
        return
    try:
        body = h.app.cameras.request_snapshot(req["id"])
    except home_mod.ActionError as exc:
        return h.send_api_error(exc.code, "camera_error", str(exc))
    h.send_json(body, False)


def post_laundry_check(h):
    """POST /api/laundry/check {}: look at the clotheslines now, whatever the weather. LAN only."""
    if read_lan_json(h, "Laundry check", need_id=False) is None:
        return
    if not h.app.cfg["laundry"]["lines"]:
        return h.send_api_error(404, "not_configured", "No clotheslines in config.json (laundry.lines).")
    if not h.app.laundry.start("check asked from the dashboard"):
        return h.send_api_error(409, "busy", "A laundry check is already running.")
    h.send_json({"ok": True, "started": True}, False)


def post_camera_control(h):
    """POST /api/cameras/control {"id", "move": "left|right|up|down"} or {"id", "mode": "day|night|auto"}:
    one pan/tilt step or a day/night change. LAN only."""
    req = read_lan_json(h, "Camera controls")
    if req is None:
        return
    try:
        if "move" in req:
            body = h.app.cameras.move(req["id"], req["move"])
        elif "mode" in req:
            body = h.app.cameras.set_mode(req["id"], req["mode"])
        else:
            return h.send_api_error(400, "bad_request", "Send \"move\" or \"mode\".")
    except home_mod.ActionError as exc:
        return h.send_api_error(exc.code, "camera_error", str(exc))
    h.send_json(body, False)
