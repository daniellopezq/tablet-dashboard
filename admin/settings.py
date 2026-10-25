"""Settings page: the editable config.json fields, saved atomically, applied live where possible."""
import json
import os
import shutil
from datetime import datetime

from .core import ApiError, coerce, log, SETTINGS


class SettingsMixin:
    """Mixed into AdminService (admin/service.py)."""

    # ======================================================================================
    # settings (config.json)
    # ======================================================================================
    def _raw_config(self):
        try:
            with open(self.config_path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raw = {}
        if not isinstance(raw, dict):
            raise ApiError(500, "config.json is not a JSON object.")
        return raw

    def save_config(self, changes):
        """Write {section: {key: value}} ("general" = top level) to config.json after validating the
        whole result exactly as the server would load it. Backs up the old file."""
        with self._config_lock:
            raw = self._raw_config()
            for section, vals in changes.items():
                target = raw if section == "general" else raw.setdefault(section, {})
                target.update(vals)
            text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
            tmp = self.config_path + ".admin.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            try:
                merged = self.load_config(tmp)
            except Exception as exc:
                os.remove(tmp)
                raise ApiError(400, "Not saved: %s" % exc)
            if os.path.exists(self.config_path):
                bdir = os.path.join(os.path.dirname(os.path.abspath(self.config_path)), "backups")
                os.makedirs(bdir, exist_ok=True)
                shutil.copy2(self.config_path, os.path.join(
                    bdir, "config-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")))
                shutil.copymode(self.config_path, tmp)
            os.replace(tmp, self.config_path)
        log.info("admin: config.json updated (%s)", ", ".join(sorted(changes)))
        return merged

    def api_get_settings(self, _data):
        cfg = self.app.cfg
        sections = []
        for sid, (title, fields) in SETTINGS.items():
            src = cfg if sid == "general" else cfg[sid]
            sections.append({"id": sid, "title": title, "fields": [
                {"key": k, "type": t, "label": label, "restart": r, "value": src[k]} for k, t, label, r in fields]})
        return {"ok": True, "sections": sections}

    def api_post_settings(self, data):
        sid, values = data.get("section"), data.get("values")
        if sid not in SETTINGS or not isinstance(values, dict):
            raise ApiError(400, "Unknown settings section.")
        fields = {k: (t, r) for k, t, _, r in SETTINGS[sid][1]}
        clean, restart = {}, False
        src = self.app.cfg if sid == "general" else self.app.cfg[sid]
        for key, value in values.items():
            if key not in fields:
                raise ApiError(400, "%s cannot be edited here." % key)
            try:
                clean[key] = coerce(fields[key][0], value)
            except ValueError as exc:
                raise ApiError(400, "%s %s" % (key, exc))
        changed = {k: v for k, v in clean.items() if src.get(k) != v}
        if not changed:
            return {"ok": True, "message": "No changes.", "restart_required": False}
        merged = self.save_config({sid: changed})
        live = {k: merged[sid][k] for k in changed if not fields[k][1]} if sid != "general" else {}
        restart = any(fields[k][1] for k in changed)
        self._apply_live(sid, live)
        return {"ok": True, "restart_required": restart,
                "message": "Saved. Restart the dashboard to apply." if restart else "Saved and applied."}

    def _apply_live(self, sid, live):
        if not live:
            return
        a = self.app
        if sid == "assistant":
            a.assistant.apply_settings(live)
        elif sid == "voice":
            a.voice.apply_settings(live)
        elif sid == "tts":
            a.speaker.apply_settings(live)
        else:
            a.cfg[sid].update(live)
        if sid == "calendar":
            a.calendar.wake()
        if sid in ("health", "kuma"):
            a.health.wake()
