"""AI models page: cloud model choice and tests, local models, the offline test switch and fallback tests."""
import json
import os
import time
import urllib.error
import urllib.request

import assistant as assistant_mod
import envfile
import localai as localai_mod
import netsim
import voice as voice_mod
from upstream import NoRedirect

from .core import AI_KEYS, ApiError, _clean_err, coerce, log, _tail


class AIMixin:
    """Mixed into AdminService (admin/service.py)."""

    # ======================================================================================
    # AI models (cloud + local)
    # ======================================================================================
    def _last_entry(self, store):
        lines = _tail(store.usage_path, 30, 64 * 1024)
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and not e.get("admin_test"):
                return {k: e.get(k) for k in ("ts_ms", "source", "model", "outcome", "duration_ms", "cloud",
                                              "simulated")}
        return None

    def api_get_ai(self, _data):
        a = self.app
        out = {"ok": True, "local": a.localai.status(),
               "offline_test": netsim.SIM.state() if netsim.SIM else {"active": False}}
        for sid, svc, keynames in (("assistant", a.assistant, (assistant_mod.KEY_ENV,)),
                                   ("voice", a.voice, voice_mod.KEY_NAMES)):
            key, problem = envfile.load_api_key(svc.env_path, keynames)
            c = svc.cfg
            out[sid] = {"settings": {k: c[k] for k in AI_KEYS[sid]}, "base_url": c["base_url"],
                        "configured": key is not None, "problem": problem,
                        "fallback_model": c["fallback_model"], "fallback_base_url": c["fallback_base_url"],
                        "cooling_down": svc.gate.cooling_down(), "last": self._last_entry(svc.store),
                        "cloud_models": self._model_cache.get(sid, (0, None))[1]}
        return out

    def _fetch_models(self, base_url, key):
        req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={
            "Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "TabletDashboard-admin"})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=10) as resp:
                raw = resp.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise ApiError(502, "The provider answered HTTP %d%s." % (
                exc.code, " (key rejected)" if exc.code == 401 else ""))
        except OSError as exc:
            raise ApiError(502, "Could not reach the provider (%s)." % _clean_err(exc))
        try:
            items = json.loads(raw.decode("utf-8"))["data"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise ApiError(502, "Unexpected model list from the provider.")
        return [i for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)]

    def api_post_ai_models(self, data):
        sid = data.get("service")
        if sid not in AI_KEYS:
            raise ApiError(400, "Unknown service.")
        cached = self._model_cache.get(sid)
        if cached and time.time() - cached[0] < 3600 and not data.get("force"):
            return {"ok": True, "models": cached[1]}
        svc = self.app.assistant if sid == "assistant" else self.app.voice
        key, problem = envfile.load_api_key(
            svc.env_path, (assistant_mod.KEY_ENV,) if sid == "assistant" else voice_mod.KEY_NAMES)
        if not key:
            raise ApiError(409, "Set the API key first (%s)." % problem)
        items = self._fetch_models(svc.cfg["base_url"], key)
        if sid == "assistant":
            items = [i for i in items if i.get("type") in (None, "language")]
            models = sorted({i["id"] for i in items if "/" in i["id"]})
        else:
            models = sorted({i["id"] for i in items if "whisper" in i["id"].lower()})
        self._model_cache[sid] = (time.time(), models)
        return {"ok": True, "models": models}

    def api_post_ai_save(self, data):
        sid, values = data.get("service"), data.get("values")
        if sid not in AI_KEYS or not isinstance(values, dict):
            raise ApiError(400, "Unknown service.")
        types = {"model": "str", "fallback_enabled": "bool", "max_output_tokens": "int",
                 "fallback_max_output_tokens": "int", "fallback_timeout_seconds": "num",
                 "no_response_seconds": "num", "cloud_cooldown_seconds": "num"}
        clean = {}
        for k, v in values.items():
            if k not in AI_KEYS[sid]:
                raise ApiError(400, "%s cannot be edited here." % k)
            try:
                clean[k] = coerce(types[k], v)
            except ValueError as exc:
                raise ApiError(400, "%s %s" % (k, exc))
        svc = self.app.assistant if sid == "assistant" else self.app.voice
        changed = {k: v for k, v in clean.items() if svc.cfg.get(k) != v}
        if not changed:
            return {"ok": True, "message": "No changes."}
        merged = self.save_config({sid: changed})
        svc.apply_settings({k: merged[sid][k] for k in changed})
        log.info("admin: %s settings changed live (%s)", sid, ", ".join(sorted(changed)))
        return {"ok": True, "message": "Saved and applied (no restart needed)."}

    def _cloud_test(self, sid):
        """One real cloud call, outside the usage limits (but recorded in the usage log)."""
        svc = self.app.assistant if sid == "assistant" else self.app.voice
        names = (assistant_mod.KEY_ENV,) if sid == "assistant" else voice_mod.KEY_NAMES
        key, problem = envfile.load_api_key(svc.env_path, names)
        if not key:
            return {"ok": False, "error": "API key: %s" % problem}
        t0 = time.monotonic()
        entry = {"ts_ms": int(time.time() * 1000), "model": svc.cfg["model"], "source": "api", "admin_test": True}
        try:
            if sid == "assistant":
                result = svc._call_upstream(key, "Reply with just the word OK.")
                text = result["answer"]
                entry.update(svc._usage_fields(result["usage"]))
            else:
                wav = self.app.localai.sample_wav()
                if wav is None:
                    return {"ok": False, "error": "sample audio %s not found" % localai_mod.SAMPLE_WAV}
                text = svc._call_upstream(key, wav, svc.cfg["timeout_seconds"])["text"]
        except Exception as exc:  # NoResponse, UpstreamError
            entry.update(outcome=getattr(exc, "category", None) or type(exc).__name__,
                         http_status=getattr(exc, "http_status", None))
            svc._log_outcome(entry)
            msg = ("no response (simulated offline?)" if type(exc).__name__ == "NoResponse"
                   else "%s%s" % (entry["outcome"], " HTTP %s" % entry["http_status"] if entry["http_status"] else ""))
            return {"ok": False, "error": msg, "ms": int((time.monotonic() - t0) * 1000)}
        entry.update(outcome="success", http_status=200, duration_ms=int((time.monotonic() - t0) * 1000))
        svc._log_outcome(entry)
        return {"ok": True, "text": text[:400], "ms": entry["duration_ms"], "model": svc.cfg["model"],
                "cost_usd": entry.get("reported_cost_usd") or entry.get("estimated_cost_usd"),
                "tokens": entry.get("total_tokens")}

    def api_post_ai_test(self, data):
        sid, target = data.get("service"), data.get("target")
        if sid not in AI_KEYS or target not in ("cloud", "local"):
            raise ApiError(400, "Unknown test.")
        la = self.app.localai
        if target == "cloud":
            fn = lambda: self._cloud_test(sid)  # noqa: E731
        elif sid == "assistant":
            q = data.get("question") or "What is the capital of France? Answer in one sentence."
            if not isinstance(q, str) or len(q) > 500:
                raise ApiError(400, "Question too long.")
            fn = lambda: la.test_llm(q)  # noqa: E731
        else:
            fn = la.test_stt
        return self._start_job("%s %s test" % (target, sid), fn)

    def api_post_ai_local_save(self, data):
        la = self.app.localai
        keys = ("llm_model", "llm_backend", "stt_model", "stt_backend", "threads")
        values = {}
        for k in keys:
            if k in data:
                try:
                    values[k] = coerce("int" if k == "threads" else "str", data[k])
                except ValueError as exc:
                    raise ApiError(400, "%s %s" % (k, exc))
        problem = la.check_choice(values)
        if problem:
            raise ApiError(400, problem)
        changes = {"local_ai": values}
        if "llm_model" in values:
            changes["assistant"] = {"fallback_model": os.path.splitext(values["llm_model"])[0]}
        if "stt_model" in values:
            changes["voice"] = {"fallback_model": os.path.splitext(values["stt_model"])[0]}
        merged = self.save_config(changes)
        la.cfg.update(merged["local_ai"])
        for sid in ("assistant", "voice"):
            if sid in changes:
                (self.app.assistant if sid == "assistant" else self.app.voice).apply_settings(changes[sid])
        if data.get("restart"):
            return self._start_job("restart local models", lambda: self._local_action("restart"))
        return {"ok": True, "message": "Saved. Restart the local models to load them. " + localai_mod.SWAP_NOTE}

    def _local_action(self, action):
        ok, output = self.app.localai.run(action)
        return {"ok": ok, "output": output, "servers": {k: self.app.localai.server(k) for k in ("llm", "stt")}}

    def api_post_ai_local_action(self, data):
        action = data.get("action")
        if action not in ("start", "stop", "restart"):
            raise ApiError(400, "Unknown action.")
        return self._start_job("%s local models" % action, lambda: self._local_action(action))

    # ======================================================================================
    # offline test switch
    # ======================================================================================
    def _offline_changed(self, active):
        if not active:
            # Back online: use the cloud again right away instead of waiting out a cooldown.
            self.app.assistant.gate.reset()
            self.app.voice.gate.reset()

    def _sim(self):
        if netsim.SIM is None:
            raise ApiError(503, "The offline test switch is not available in this process.")
        return netsim.SIM

    def api_get_offline(self, _data):
        a = self.app
        return {"ok": True, "state": self._sim().state(), "services": list(netsim.SERVICES),
                "modes": list(netsim.MODES), "max_minutes": netsim.MAX_MINUTES,
                "gates": {"ask": a.assistant.gate.cooling_down(), "voice": a.voice.gate.cooling_down()},
                "fallback_enabled": {"ask": a.assistant.cfg["fallback_enabled"],
                                     "voice": a.voice.cfg["fallback_enabled"]},
                "no_response_seconds": {"ask": a.assistant.cfg["no_response_seconds"],
                                        "voice": a.voice.cfg["no_response_seconds"]}}

    def api_post_offline(self, data):
        try:
            st = self._sim().turn_on(data.get("services"), data.get("mode", "realistic"),
                                     coerce("num", data.get("minutes", 15)))
        except ValueError as exc:
            raise ApiError(400, str(exc))
        self.app.health.wake()
        return {"ok": True, "state": st}

    def api_post_offline_off(self, _data):
        st = self._sim().turn_off()
        self.app.assistant.gate.reset()
        self.app.voice.gate.reset()
        self.app.health.wake()
        return {"ok": True, "state": st}

    def api_post_offline_fallback_test(self, data):
        which = data.get("services") or ["ask", "voice"]
        if not isinstance(which, list) or not set(which) <= {"ask", "voice"}:
            raise ApiError(400, "services must be ask and/or voice")
        return self._start_job("fallback test", lambda: self._fallback_test(which))

    def _fallback_test(self, which):
        """Real requests through the normal pipelines (they count toward the usage limits)."""
        out = {"ok": True, "offline_test": netsim.SIM.state() if netsim.SIM else None}
        if "ask" in which:
            t0 = time.monotonic()
            status, body = self.app.assistant.ask("Fallback test: what is 2 + 2? Answer in a few words.")
            out["ask"] = {"status": status, "source": body.get("source"), "ok": body.get("ok"),
                          "answer": (body.get("answer") or "")[:300], "error": body.get("error"),
                          "ms": int((time.monotonic() - t0) * 1000)}
        if "voice" in which:
            wav = self.app.localai.sample_wav()
            if wav is None:
                out["voice"] = {"ok": False, "error": {"message": "sample audio not found"}}
            else:
                t0 = time.monotonic()
                status, body = self.app.voice.transcribe(wav)
                out["voice"] = {"status": status, "source": body.get("source"), "ok": body.get("ok"),
                                "text": (body.get("text") or "")[:300], "error": body.get("error"),
                                "ms": int((time.monotonic() - t0) * 1000)}
        return out
