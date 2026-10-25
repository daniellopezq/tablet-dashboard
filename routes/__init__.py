"""URL routing for the dashboard's HTTP handler.

GET/HEAD: exact paths first, then prefixes in the order listed, then static files from web/.
Routes marked LAN-only answer 403 {"error": "forbidden"} to other clients.
POST: /api/admin/* or an exact path; anything else is 405, like PUT and DELETE.

To add an endpoint, write a small function in the matching module and add one line here.
GET handlers take (handler, path, head); POST handlers take (handler).
"""
import urllib.parse

from . import admin_api, ai, devices, files, public

# path: (handler, LAN only)
GET = {
    "/": (files.index, False),
    "/admin": (files.admin_page, False),
    "/admin/": (files.admin_page, False),
    "/api/time": (public.get_time, False),
    "/api/weather": (public.get_weather, False),
    "/api/maps": (public.get_maps, False),
    "/api/calendar": (public.get_calendar, True),  # personal data
    "/api/uptime": (public.get_uptime, True),  # names of home services
    "/api/home": (public.get_home, True),  # home controls
    "/api/cameras": (devices.get_camera_list, True),  # pictures of the home
    "/api/config": (public.get_config, False),
    "/api/assistant/status": (public.get_assistant_status, False),
    "/api/voice/status": (public.get_voice_status, False),
}

# (prefix, handler, LAN only), checked in order
GET_PREFIXES = [
    ("/api/cameras/", devices.get_camera_path, True),
    ("/api/tts/", ai.get_tts, True),  # spoken answers
    ("/api/admin/", admin_api.get, False),
    ("/admin.", files.admin_page, False),
    ("/api/", public.not_found, False),
    ("/maps/", files.maps, False),
    ("/media/", files.media, False),
    ("/m/", files.modern, False),
]

POST = {
    "/api/assistant": ai.post_assistant,
    "/api/assistant/confirm": ai.post_assistant_confirm,
    "/api/transcribe": ai.post_transcribe,
    "/api/home/action": devices.post_home_action,
    "/api/cameras/snapshot": devices.post_camera_snapshot,
    "/api/cameras/control": devices.post_camera_control,
}


def dispatch(h, method):
    path = urllib.parse.urlsplit(h.path).path
    if method == "POST":
        if path.startswith("/api/admin/"):
            return admin_api.post(h)
        fn = POST.get(path)
        return fn(h) if fn else h.method_not_allowed()
    head = method == "HEAD"
    if not path.startswith("/"):
        return h.send_text(400, "Bad request", head)
    route = GET.get(path)
    if route is None:
        route = next(((fn, lan) for prefix, fn, lan in GET_PREFIXES if path.startswith(prefix)),
                     (files.static, False))
    fn, lan_only = route
    if lan_only and not h.is_lan():
        return h.send_json({"error": "forbidden"}, head, status=403)
    return fn(h, path, head)
