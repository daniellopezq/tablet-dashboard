"""Ask the house: the assistant's view of the Home tiles and cameras.

Three jobs:
  match()        Simple commands and questions are understood here, without the AI and without using the
                 Ask quota: "apaga el abanico", "turn on the recamara", "¿qué está prendido?", "what do you
                 see on the street?" (the last one only picks the camera; looking needs the cloud model).
  context_text() The house block for the cloud/local model: the buttons it may press (ids, names, current
                 state) and the cameras it may look at. Nothing else in Home Assistant is reachable.
  run_actions()  Carries out actions (from match() or the model's JSON reply) through Home.set_state, which
                 never toggles blindly. Buttons with "confirm": true (alarm, media switch) are NOT pressed:
                 a one-time token comes back and POST /api/assistant/confirm presses them after the person
                 says yes on screen. The model can't skip that step.

Python 3 standard library (Pillow, already used by maps.py, only to shrink camera images).
"""
import base64
import io
import logging
import re
import secrets
import threading
import time
import unicodedata

from home import ActionError, MEDIA, MEDIA_LABEL

log = logging.getLogger("dashboard.homeai")

CONFIRM_SECONDS = 60
MAX_IMAGE_SIDE = 1024
MAX_PENDING = 16

_ON = (r"prende", r"prender", r"prendan", r"prendele", r"enciende", r"encender", r"enciendan", r"activa",
       r"activar", r"arma", r"armar", r"turn on", r"switch on", r"power on", r"arm")
_OFF = (r"apaga", r"apagar", r"apaguen", r"apagale", r"desactiva", r"desactivar", r"desarma", r"desarmar",
        r"turn off", r"switch off", r"power off", r"shut off", r"disarm")
_TO_MEDIA = r"\b(?:cambia|cambiar|pon|switch|change)\b.*\b(plex|jellyfin)\b"
_SPLIT_VERB = r"\bturn\b.*\b(on|off)\b"   # "turn the fan off"
# "What's on?" questions about the whole house (no button named).
_STATE_ALL = (r"\bque (?:esta|estan|hay|quedo|se quedo) (?:prendid|encendid)", r"\bcuales? (?:esta|estan) (?:prendid|encendid)",
              r"\bwhat(?: s| is| are)? (?:still )?on\b", r"\banything (?:still )?on\b", r"\bque luces\b",
              r"\bwhich lights\b", r"\bcomo esta la casa\b", r"\bhow is the house\b", r"\bestado de la casa\b")
# Questions about named buttons ("¿está prendido el abanico?", "is the alarm armed?").
_STATE_ONE = (r"^(?:esta|estan|sigue|siguen) ", r"\b(?:esta|estan|sigue) (?:prendid|encendid|apagad|armad|desarmad|activad)",
              r"^(?:is|are) ", r"\bis (?:the )?.* (?:on|off|armed|disarmed)\b", r"\bcomo esta\b")
_LOOK = (r"\bque (?:ves|se ve|hay en|pasa en|esta pasando)\b", r"\bhay (?:alguien|algo|gente|personas?|un carro|carros)\b",
         r"\bquien esta\b", r"\bdescribe\b", r"\bdescribeme\b", r"\bmira\b", r"\bcomo se ve\b", r"\bsigue ahi\b",
         r"\bwhat (?:do|can) you see\b", r"\bis (?:there )?(?:anyone|anybody|someone|somebody)\b",
         r"\bare there (?:people|any)\b", r"\bwho is (?:at|in|on)\b", r"\blook at\b", r"\bwhat s happening\b",
         r"\bshow me\b", r"\bstill there\b",
         r"\b(?:puedes|podrias) (?:ver|mirar|revisar|checar)\b", r"\bver si\b", r"\brevisa\b",
         r"\bcheca\b", r"\bfijate\b", r"\bmirame\b", r"\bcan you (?:see|check)\b", r"\bcheck (?:if|whether|the)\b",
         r"\bdid i leave\b", r"\bdeje\b.*\b(?:afuera|colgad|abiert)")
_AND = r"\b(?:y|e|and|tambien|also)\b"
# Filler words ignored when matching names, so "la luz de la recámara principal" finds "Recámara principal".
_STOP = {"el", "la", "los", "las", "de", "del", "en", "mi", "the", "a", "an", "of", "in", "my", "plug", "light",
         "lights", "luz", "luces", "por", "favor", "please"}


def _bare(text):
    return " ".join(w for w in text.split() if w not in _STOP)


def fold(text):
    """Lowercase, no accents, only letters/digits/spaces: 'Recámara plug!' -> 'recamara plug'."""
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def lang_of(text):
    """'es' or 'en' (same rule as the voice picker)."""
    import tts  # local import keeps homeai importable without tts
    return tts.guess_lang(text)


def _any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def _verb(text):
    m = re.search(_TO_MEDIA, text)
    if m:
        return m.group(1)
    on = any(re.search(r"\b%s\b" % p, text) for p in _ON)
    off = any(re.search(r"\b%s\b" % p, text) for p in _OFF)
    if not (on or off):
        m = re.search(_SPLIT_VERB, text)
        if m:
            return m.group(1)
        return None
    if on and off:
        # "desarma" contains no "arma" word, but "turn on ... turn off" is two commands: leave it to the AI.
        return None
    return "on" if on else "off"


def _names(label, ident, aliases):
    out = {fold(label), fold(ident.replace("_", " "))}
    out.update(fold(a) for a in aliases)
    # Matched against the question without filler words ("Recámara plug" is just "recamara").
    return {b for b in (_bare(n) for n in out) if b}


def _find(text, things):
    """Things (dicts with 'names') whose name appears in text as whole words; longest names win, and a
    name inside a longer matched name ('recamara' in 'recamara principal') doesn't count separately."""
    padded = " %s " % text
    hits = []
    for t in things:
        best = None
        for n in t["names"]:
            i = padded.find(" %s " % n)
            if i >= 0 and (best is None or len(n) > len(best[1])):
                best = (i, n)
        if best:
            hits.append((best[0], best[0] + len(best[1]) + 1, t))
    hits.sort(key=lambda h: -(h[1] - h[0]))
    chosen = []
    for start, end, t in hits:
        if any(start < e and s < end for s, e, _ in chosen):
            continue
        chosen.append((start, end, t))
    return [t for _, _, t in sorted(chosen, key=lambda h: h[0])]


_PHRASES = {
    # Short "<name> <state>." replies: easier for the voice than "Listo, apagué <name>." (user, 2026-10-25).
    "on": {"es": "%s prendido.", "en": "%s is on."},
    "off": {"es": "%s apagado.", "en": "%s is off."},
    "press": {"es": "%s presionado.", "en": "%s pressed."},
    "already_on": {"es": "%s ya estaba prendido.", "en": "%s was already on."},
    "already_off": {"es": "%s ya estaba apagado.", "en": "%s was already off."},
    "media": {"es": "Cambiando el servidor a %s; tarda alrededor de un minuto.",
              "en": "Switching the media server to %s; it takes about a minute."},
    "media_same": {"es": "%s ya está activo.", "en": "%s is already running."},
    "failed": {"es": "No pude con %s: %s.", "en": "I couldn't change %s: %s."},
    "confirm": {"es": "¿%s? Confírmalo en la pantalla.", "en": "%s? Please confirm on the screen."},
    "nothing_on": {"es": "Todo está apagado.", "en": "Everything is off."},
    "are_on": {"es": "Prendido: %s.", "en": "On: %s."},
    "state_on": {"es": "%s está prendido.", "en": "%s is on."},
    "state_off": {"es": "%s está apagado.", "en": "%s is off."},
    "state_unknown": {"es": "No sé cómo está %s ahora.", "en": "I don't know the state of %s right now."},
    "media_now": {"es": "El servidor activo es %s.", "en": "The active media server is %s."},
    "ha_down": {"es": "Home Assistant no responde ahora.", "en": "Home Assistant isn't answering right now."},
    "expired": {"es": "Esa confirmación ya expiró. Pídelo otra vez.",
                "en": "That confirmation expired. Please ask again."},
    "offline_look": {"es": "Necesito internet para mirar las cámaras ahora mismo.",
                     "en": "I need the internet to look at the cameras right now."},
}


def phrase(key, lang, *args):
    text = _PHRASES[key]["es" if lang == "es" else "en"]
    return text % args if args else text


def _switchable(button):
    """True for buttons with an on/off state (not press-only, not the media switch, not read-only sensors)."""
    return (bool(button.get("state_entity")) and button["kind"] != "sensor") or button["kind"] == "away"


def _armed_word(button, lang, on):
    if button["kind"] == "sensor":
        words = button.get("words_es" if lang == "es" else "words")
        return words["on" if on else "off"].lower() if words else None
    if button["kind"] == "away":
        if lang == "es":
            return "activado" if on else "desactivado"
        return "on" if on else "off"
    if button.get("on_service", "").startswith("alarm_control_panel."):
        if lang == "es":
            return "armada" if on else "desarmada"
        return "armed" if on else "disarmed"
    return None


def shrink_image(data, ctype):
    """(bytes, 'image/jpeg') no larger than MAX_IMAGE_SIDE on its long side; the original if Pillow fails."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return data, ctype


def data_url(data, ctype):
    return "data:%s;base64,%s" % (ctype, base64.b64encode(data).decode("ascii"))


class House:
    def __init__(self, home, cameras, clock=time.time, weather=None, history=None):
        self.home = home
        self.cameras = cameras
        self.weather = weather  # callable -> weather.WeatherService.snapshot(), or None
        self.history = history  # callable -> pgstore.Database.weather_summary() (last 24 h), or None
        self.clock = clock
        self._lock = threading.Lock()
        self._pending = {}   # token -> (expires, conversation, [(button id, want)], lang)

    # --- what the house has -------------------------------------------------------------

    def buttons(self):
        if not self.home.cfg["enabled"]:
            return []
        return [dict(b, names=_names(b["label"], b["id"], b.get("aliases", []))) for b in self.home.cfg["buttons"]]

    def camera_list(self):
        return [dict(c, names=_names(c["label"], c["id"], c["aliases"])) for c in self.cameras.ai_cameras()]

    def _states(self):
        """{button id: 'on'|'off'|'plex'|...|None}, or None when Home Assistant is down."""
        out = {}
        buttons = self.buttons()
        need_ha = any(b["kind"] in ("ha", "sensor") and b.get("state_entity") for b in buttons)
        states = self.home.ha_states() if need_ha else {}
        for b in buttons:
            if b["kind"] == "media_switch":
                out[b["id"]] = self.home.media_status()["current"]
            elif b["kind"] == "away":
                out[b["id"]] = self.home.button_state(b)
            elif states is None:
                out[b["id"]] = None
            else:
                out[b["id"]] = self.home.button_state(b, states)
        return out, states is not None

    def context_text(self, now_text=""):
        """The HOUSE block for the model prompt (ids, names, states; no entity ids or addresses)."""
        states, ha_ok = self._states()
        lines = ["HOUSE%s:" % ((" (" + now_text + ")") if now_text else "")]
        lines.append("buttons (id \"name\" [kind]: state):")
        for b in self.buttons():
            kind = "media switch Plex/Jellyfin" if b["kind"] == "media_switch" else (
                "alarm" if b.get("on_service", "").startswith("alarm_control_panel.") else
                "away mode: random lights after dark while nobody is home" if b["kind"] == "away" else
                "read-only status, can't be switched%s" % (
                    " (on = %s, off = %s)" % (b["words"]["on"], b["words"]["off"]) if b.get("words") else "")
                if b["kind"] == "sensor" else
                "on/off" if b.get("state_entity") else "press only")
            extra = ", needs confirmation" if b.get("confirm") else ""
            also = (" also called: " + ", ".join(b["aliases"])) if b.get("aliases") else ""
            st = states.get(b["id"])
            lines.append('- %s "%s" [%s%s]: %s%s' % (b["id"], b["label"], kind, extra,
                                                     st if st else "unknown", also))
        if not ha_ok:
            lines.append("(Home Assistant is not answering, states are unknown)")
        w = self.weather_text()
        if w:
            lines.append(w)
        cams = self.camera_list()
        if cams:
            lines.append("cameras (id \"name\"):")
            for c in cams:
                also = (" also called: " + ", ".join(c["aliases"])) if c["aliases"] else ""
                lines.append('- %s "%s"%s%s' % (c["id"], c["label"], " [live]" if c["live"] else "", also))
        return "\n".join(lines)

    def weather_text(self):
        """One line from the dashboard's own Open-Meteo cache (no extra calls), or ''."""
        if self.weather is None:
            return ""
        try:
            snap = self.weather()
        except Exception:
            return ""
        d = (snap or {}).get("data") or {}
        cur, today, fc = d.get("current") or {}, d.get("today") or {}, d.get("forecast") or []
        if not cur:
            return ""
        u = snap.get("unit") or "C"
        parts = ["WEATHER at %s%s: now %s°%s, %s, humidity %s%%" % (
            snap.get("label") or "home", " (may be out of date)" if snap.get("stale") else "",
            cur.get("temperature"), u, (cur.get("condition") or "").lower(), cur.get("humidity"))]
        if today:
            parts.append("today %s, %s-%s°%s" % ((today.get("condition") or "").lower(), today.get("min"), today.get("max"), u))
        if fc:
            parts.append("tomorrow %s, %s-%s°%s" % ((fc[0].get("condition") or "").lower(), fc[0].get("min"), fc[0].get("max"), u))
            if fc[0].get("precipitation_probability") is not None:
                parts[-1] += ", rain chance %s%%" % fc[0]["precipitation_probability"]
        hist = None
        if self.history is not None:
            try:
                hist = self.history()
            except Exception:
                hist = None
        if hist:
            parts.append("last 24 h (own records, %s readings): %s-%s°%s, rain about %s mm%s" % (
                hist["readings"], hist.get("min"), hist.get("max"), u, hist.get("rain_mm"),
                (", last rain at %s" % hist["last_rain"]) if hist.get("last_rain") else ", no rain"))
        return "; ".join(parts)

    # --- understanding simple requests ----------------------------------------------------

    def match(self, question, last_camera=None):
        """An intent dict, or None when the AI should handle it:
        {"type": "actions", "actions": [(id, want)]}, {"type": "state", "ids": [...]} or {"type": "look", "camera": id}."""
        text = fold(question)
        if not text:
            return None
        bare = _bare(text)
        if _any(_LOOK, text):
            cams = _find(bare, self.camera_list())
            if len(cams) == 1:
                return {"type": "look", "camera": cams[0]["id"]}
            if not cams and last_camera and any(c["id"] == last_camera for c in self.cameras.ai_cameras()):
                return {"type": "look", "camera": last_camera}
            return None
        buttons = self.buttons()
        found = _find(bare, buttons)
        if not re.search(_TO_MEDIA, text):
            if found and len(found) <= 3 and (_any(_STATE_ONE, text) or _any(_STATE_ALL, text)):
                return {"type": "state", "ids": [b["id"] for b in found]}
            if not found and _any(_STATE_ALL, text):
                return {"type": "state", "ids": []}
        verb = _verb(text)
        if verb is None or not found:
            return None
        if len(found) > 1 and not re.search(_AND, text):
            return None
        if any(b["kind"] == "sensor" for b in found):
            return {"type": "state", "ids": [b["id"] for b in found]}  # "abre el garaje": say its state, never act
        actions = []
        for b in found:
            if b["kind"] == "media_switch":
                if verb not in MEDIA:
                    return None
                actions.append((b["id"], verb))
            elif verb in MEDIA:
                return None
            elif _switchable(b):
                actions.append((b["id"], verb))
            else:
                actions.append((b["id"], "press"))
        return {"type": "actions", "actions": actions}

    # --- answering and acting -----------------------------------------------------------

    def state_answer(self, ids, lang):
        states, ha_ok = self._states()
        buttons = {b["id"]: b for b in self.buttons()}
        if ids:
            parts = []
            for bid in ids:
                b, st = buttons[bid], states.get(bid)
                if b["kind"] == "media_switch":
                    parts.append(phrase("media_now", lang, MEDIA_LABEL.get(st, st or "?")))
                elif st is None:
                    parts.append(phrase("ha_down", lang) if not ha_ok else phrase("state_unknown", lang, b["label"]))
                else:
                    word = _armed_word(b, lang, st == "on")
                    if word:
                        parts.append("%s: %s." % (b["label"], word))
                    else:
                        parts.append(phrase("state_on" if st == "on" else "state_off", lang, b["label"]))
            return " ".join(parts)
        if not ha_ok:
            return phrase("ha_down", lang)
        on = [buttons[bid]["label"] for bid, st in states.items()
              if st == "on" and buttons[bid]["kind"] in ("ha", "away")]
        return phrase("are_on", lang, ", ".join(on)) if on else phrase("nothing_on", lang)

    def valid_action(self, bid, want):
        b = self.home.button(bid) if self.home.cfg["enabled"] else None
        if b is None or b["kind"] == "sensor":
            return None
        if b["kind"] == "media_switch":
            return (bid, want if want in MEDIA else "press")
        if want not in ("on", "off", "press"):
            return None
        if want != "press" and not _switchable(b):
            want = "press"
        return (bid, want)

    def run_actions(self, actions, lang, conversation=None, confirm_all=False):
        """Run actions, holding back the ones that need confirmation (all of them with confirm_all, used when
        the model only offered to act). Returns (say, actions_done, confirm or None)."""
        done, lines, held = [], [], []
        for bid, want in actions:
            b = self.home.button(bid)
            if confirm_all or b.get("confirm"):
                held.append((bid, want))
                continue
            line, item = self._do(b, want, lang)
            lines.append(line)
            done.append(item)
        confirm = None
        if held:
            token = secrets.token_urlsafe(12)
            now = self.clock()
            with self._lock:
                self._pending = {t: p for t, p in self._pending.items() if p[0] > now}
                while len(self._pending) >= MAX_PENDING:
                    self._pending.pop(next(iter(self._pending)))
                self._pending[token] = (now + CONFIRM_SECONDS, conversation, held, lang)
            title = "; ".join(self._describe(self.home.button(bid), want, lang) for bid, want in held)
            confirm = {"token": token, "title": title, "expires_in": CONFIRM_SECONDS}
            lines.append(phrase("confirm", lang, title))
        return " ".join(lines), done, confirm

    def confirm(self, token, conversation=None):
        """Carry out held actions after the person said yes. (say, actions_done) or raise ActionError."""
        now = self.clock()
        with self._lock:
            pending = self._pending.pop(token, None) if isinstance(token, str) else None
        if pending is None or pending[0] <= now or (pending[1] and conversation and pending[1] != conversation):
            raise ActionError(phrase("expired", "es") + " / " + phrase("expired", "en"), 410)
        _, _, held, lang = pending
        lines, done = [], []
        for bid, want in held:
            b = self.home.button(bid)
            if b is None:
                continue
            line, item = self._do(b, want, lang)
            lines.append(line)
            done.append(item)
        return " ".join(lines), done

    def _describe(self, b, want, lang):
        if b["kind"] == "media_switch":
            target = MEDIA_LABEL.get(want) or ("el otro servidor" if lang == "es" else "the other server")
            return ("Cambiar a %s" if lang == "es" else "Switch to %s") % target
        if b.get("on_service", "").startswith("alarm_control_panel.") and want in ("on", "off"):
            verb = {"on": ("Armar", "Arm"), "off": ("Desarmar", "Disarm")}[want]
            return "%s %s" % (verb[0] if lang == "es" else verb[1], b["label"])
        if b["kind"] == "away" and want in ("on", "off"):
            verb = {"on": ("Activar", "Turn on"), "off": ("Desactivar", "Turn off")}[want]
            return "%s %s" % (verb[0] if lang == "es" else verb[1], b["label"])
        if want == "press":
            return ("Presionar %s" if lang == "es" else "Press %s") % b["label"]
        verb = {"on": ("Prender", "Turn on"), "off": ("Apagar", "Turn off")}[want]
        return "%s %s" % (verb[0] if lang == "es" else verb[1], b["label"])

    def speech_phrases(self):
        """Every fixed reply _do, state_answer and run_actions can give for the current buttons, in es and en,
        for tts's phrase bank. "failed" replies (they carry the error text) and lists of several are left out."""
        out = []
        for lang in ("es", "en"):
            for key in ("nothing_on", "ha_down", "offline_look", "expired"):
                out.append(phrase(key, lang))
            for b in self.buttons():
                label = b["label"]
                if b["kind"] == "media_switch":
                    for name in MEDIA_LABEL.values():
                        out += [phrase(k, lang, name) for k in ("media", "media_same", "media_now")]
                    for want in list(MEDIA) + ["press"]:
                        out.append(phrase("confirm", lang, self._describe(b, want, lang)))
                    continue
                wants = ["press"]
                out += [phrase("press", lang, label), phrase("state_unknown", lang, label)]
                if _switchable(b):
                    wants += ["on", "off"]
                    out += [phrase(k, lang, label) for k in ("already_on", "already_off")]
                    for on in (True, False):
                        word = _armed_word(b, lang, on)
                        out.append("%s: %s." % (label, word) if word else
                                   phrase(("on" if on else "off"), lang, label))
                        if not word:
                            out.append(phrase("state_on" if on else "state_off", lang, label))
                    out.append(phrase("are_on", lang, label))
                for want in wants:
                    out.append(phrase("confirm", lang, self._describe(b, want, lang)))
        return list(dict.fromkeys(out))

    def _do(self, b, want, lang):
        label = b["label"]
        try:
            r = self.home.set_state(b["id"], want)
        except ActionError as exc:
            log.info("homeai: %s -> %s failed: %s", b["id"], want, exc)
            return phrase("failed", lang, label, str(exc)), {"id": b["id"], "label": label, "to": want,
                                                              "result": "error", "error": str(exc)}
        if b["kind"] == "media_switch":
            name = MEDIA_LABEL.get(r["state"], r["state"])
            line = phrase("media" if r["changed"] else "media_same", lang, name)
        elif not r["changed"]:
            line = phrase("already_" + want, lang, label) if want in ("on", "off") else phrase("press", lang, label)
        else:
            word = _armed_word(b, lang, want == "on")
            line = ("%s: %s." % (label, word)) if word else phrase(want, lang, label)
        return line, {"id": b["id"], "label": label, "to": want,
                      "result": "done" if r["changed"] else "unchanged"}

    # --- cameras --------------------------------------------------------------------------

    def look(self, cid, max_age=60):
        """(image data URL, camera label, age seconds or None). Raises ActionError."""
        cam = next((c for c in self.cameras.ai_cameras() if c["id"] == cid), None)
        if cam is None:
            raise ActionError("unknown camera", 404)
        data, ctype, age = self.cameras.frame_for_ai(cid, max_age=max_age)
        data, ctype = shrink_image(data, ctype)
        return data_url(data, ctype), cam["label"], age
