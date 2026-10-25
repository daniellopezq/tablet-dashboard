"""POST /api/assistant (Ask), POST /api/assistant/confirm, POST /api/transcribe (Voice), and
GET /api/tts/<id>.wav (spoken answers). LAN only; the POSTs are same-origin only."""
import re

import assistant as assistant_mod

AUDIO_TYPES = ("video/mp4", "video/3gpp", "video/webm", "video/ogg", "application/ogg",
               "application/octet-stream")


def post_assistant(h):
    """POST /api/assistant {"question": "..."} -> {"ok", "answer", ...}."""
    # Always close after a POST: early rejections leave the body unread.
    h.close_connection = True
    a = h.app.assistant
    cfg = a.cfg
    if not h.is_lan():
        return h.send_api_error(403, "forbidden", "The assistant is available on the local network only.")
    if h.cross_origin():
        return h.send_api_error(403, "forbidden_origin", "Cross-origin requests are not allowed.")
    if h.content_type() != "application/json":
        return h.send_api_error(415, "unsupported_media_type", "Send application/json.")
    raw = h.read_body(cfg["max_body_bytes"], "Empty request.", "Request is too large.", "Incomplete request.")
    if raw is None:
        return
    try:
        req = assistant_mod.parse_request(raw, cfg)
    except assistant_mod.RequestError as exc:
        return h.send_api_error(exc.status, exc.code, exc.message)
    h.send_result(*a.ask(req["question"], req["conversation"], req["speak"]))


def post_assistant_confirm(h):
    """POST /api/assistant/confirm {"token": "..."}: the person said yes to a held-back home action."""
    h.close_connection = True
    a = h.app.assistant
    if not h.is_lan():
        return h.send_api_error(403, "forbidden", "The assistant is available on the local network only.")
    if h.cross_origin():
        return h.send_api_error(403, "forbidden_origin", "Cross-origin requests are not allowed.")
    if h.content_type() != "application/json":
        return h.send_api_error(415, "unsupported_media_type", "Send application/json.")
    raw = h.read_body(1024, "Empty request.", "Request is too large.", "Incomplete request.")
    if raw is None:
        return
    try:
        req = assistant_mod.parse_confirm(raw)
    except assistant_mod.RequestError as exc:
        return h.send_api_error(exc.status, exc.code, exc.message)
    h.send_result(*a.confirm(req["token"], req["conversation"], req["speak"]))


TTS_PATH = re.compile(r"^/api/tts/([A-Za-z0-9_-]{16,32})\.wav$")


def get_tts(h, path, head):
    """GET /api/tts/<speech_id>.wav: the spoken answer (synthesized on the first fetch)."""
    m = TTS_PATH.match(path)
    if not m:
        return h.send_json({"error": "not_found"}, head, status=404)
    status, wav = h.app.speaker.wav(m.group(1))
    if wav is None:
        return h.send_json({"error": "speech_unavailable"}, head, status=status)
    return h.send_bytes(200, wav, "audio/wav", head, {"Cache-Control": "no-store"})


def post_transcribe(h):
    """POST /api/transcribe: raw audio body (one recording) -> {"ok", "text", ...}."""
    h.close_connection = True  # early rejections leave the body unread
    v = h.app.voice
    if not h.is_lan():
        return h.send_api_error(403, "forbidden", "Voice input is available on the local network only.")
    if h.cross_origin():
        return h.send_api_error(403, "forbidden_origin", "Cross-origin requests are not allowed.")
    ctype = h.content_type()
    if not (ctype.startswith("audio/") or ctype in AUDIO_TYPES):
        return h.send_api_error(415, "unsupported_media_type", "Send an audio recording.")
    raw = h.read_body(v.cfg["max_upload_bytes"], "Empty recording.",
                      "That recording is too large. Keep it under %d seconds." % v.cfg["max_audio_seconds"],
                      "Incomplete upload.")
    if raw is None:
        return
    h.send_result(*v.transcribe(raw))
