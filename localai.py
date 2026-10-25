"""Local fallback model management for the admin page.

Lists the model files and llama.cpp / whisper.cpp builds under the local-ai directory, records the
chosen ones in config.json ("local_ai"), reports the running servers (PID, model, memory, health),
and starts/stops them through scripts/local-ai-start.sh and local-ai-stop.sh, which read the choice
with `python3 localai.py env`. Environment variables given to the scripts still win.

Python 3 standard library only.
"""
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import fallback as fb

log = logging.getLogger("dashboard.localai")

DEFAULT_LOCAL_AI_CONFIG = {
    # default: a "local-ai" directory next to this project (sibling folder)
    "dir": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local-ai"),
    "llm_model": "Qwen3.5-2B-Q4_K_M.gguf",
    "llm_backend": "cpu",
    "stt_model": "ggml-base.bin",
    "stt_backend": "vulkan",
    "threads": 2,
}
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(gguf|bin)$")
BACKEND_RE = re.compile(r"^[a-z0-9]{1,20}$")
KINDS = {
    # kind: (server binary, build prefix, pidfile, model test)
    "llm": ("llama-server", "llama-", "run/llama-server.pid", lambda n: n.endswith(".gguf")),
    "stt": ("whisper-server", "whisper-", "run/whisper-server.pid", lambda n: n.startswith("ggml-") and n.endswith(".bin")),
}
SAMPLE_WAV = os.path.join("src", "whisper.cpp", "samples", "jfk.wav")
SAMPLE_TEXT = "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country."
SWAP_NOTE = "Restarting the local models takes about 10-30 s; the fallback is unavailable meanwhile."


def validate_local_ai_config(c):
    if not isinstance(c["dir"], str) or not os.path.isabs(c["dir"]):
        raise ValueError("local_ai.dir must be an absolute path")
    for key in ("llm_model", "stt_model"):
        if not isinstance(c[key], str) or not MODEL_RE.match(c[key]):
            raise ValueError("local_ai.%s must be a model file name (no directories)" % key)
    for key in ("llm_backend", "stt_backend"):
        if not isinstance(c[key], str) or not BACKEND_RE.match(c[key]):
            raise ValueError("local_ai.%s must be a build name such as cpu or vulkan" % key)
    if not isinstance(c["threads"], int) or isinstance(c["threads"], bool) or not 1 <= c["threads"] <= 16:
        raise ValueError("local_ai.threads must be an integer 1-16")


def meminfo():
    """{"MemTotal": bytes, "MemAvailable": bytes} from /proc/meminfo (empty on failure)."""
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    out[name] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def _proc(pid):
    """(argv list, rss_bytes) for a PID, or (None, None)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        rss = None
        with open("/proc/%d/status" % pid) as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
        return argv, rss
    except (OSError, ValueError, IndexError):
        return None, None


class LocalAI:
    def __init__(self, cfg, base_dir, assistant, voice):
        self.cfg = cfg
        self.base_dir = base_dir
        self.assistant = assistant
        self.voice = voice

    @property
    def models_dir(self):
        return os.path.join(self.cfg["dir"], "models")

    # -- inventory ------------------------------------------------------------------------
    def models(self):
        listed = set()
        try:
            with open(os.path.join(self.models_dir, "SHA256SUMS")) as f:
                listed = {ln.split()[-1].lstrip("*") for ln in f if ln.strip()}
        except OSError:
            pass
        out = {"llm": [], "stt": []}
        try:
            names = sorted(os.listdir(self.models_dir), key=str.lower)
        except OSError:
            return out
        for name in names:
            path = os.path.join(self.models_dir, name)
            if not MODEL_RE.match(name) or os.path.islink(path) or not os.path.isfile(path):
                continue
            for kind, (_, _, _, test) in KINDS.items():
                if test(name):
                    out[kind].append({"name": name, "bytes": os.path.getsize(path), "checksum_listed": name in listed})
        return out

    def backends(self):
        out = {"llm": [], "stt": []}
        build = os.path.join(self.cfg["dir"], "build")
        for kind, (binary, prefix, _, _) in KINDS.items():
            try:
                names = sorted(os.listdir(build))
            except OSError:
                names = []
            for d in names:
                if d.startswith(prefix) and BACKEND_RE.match(d[len(prefix):]):
                    if os.access(os.path.join(build, d, "bin", binary), os.X_OK):
                        out[kind].append(d[len(prefix):])
        return out

    def model_path(self, name):
        """Absolute path of a model file inside the models directory, or None."""
        if not isinstance(name, str) or not MODEL_RE.match(name):
            return None
        path = os.path.join(self.models_dir, name)
        real = os.path.realpath(path)
        if os.path.dirname(real) != os.path.realpath(self.models_dir) or not os.path.isfile(real):
            return None
        return real

    # -- running servers ------------------------------------------------------------------
    def _health_url(self, kind):
        if kind == "llm":
            parts = urllib.parse.urlsplit(self.assistant.cfg["fallback_base_url"])
            return "http://%s/health" % parts.netloc
        parts = urllib.parse.urlsplit(self.voice.cfg["fallback_base_url"])
        return "http://%s/" % parts.netloc

    def server(self, kind, probe=True):
        binary, prefix, pidfile, _ = KINDS[kind]
        info = {"running": False, "pid": None, "model": None, "backend": None, "rss_bytes": None,
                "healthy": False, "health": None}
        try:
            with open(os.path.join(self.base_dir, pidfile)) as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None
        if pid:
            argv, rss = _proc(pid)
            if argv and os.path.basename(argv[0]) == binary:
                info.update(running=True, pid=pid, rss_bytes=rss)
                if "-m" in argv[:-1]:
                    info["model"] = os.path.basename(argv[argv.index("-m") + 1])
                parent = os.path.basename(os.path.dirname(os.path.dirname(argv[0])))
                if parent.startswith(prefix):
                    info["backend"] = parent[len(prefix):]
        if not probe:
            return info
        try:
            req = urllib.request.Request(self._health_url(kind), headers={"User-Agent": "TabletDashboard-admin"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                info["healthy"] = resp.status == 200
                info["health"] = "ok"
        except urllib.error.HTTPError as exc:
            info["health"] = "HTTP %d" % exc.code  # llama-server answers 503 while loading
        except (OSError, ValueError):
            info["health"] = "unreachable"
        return info

    def status(self):
        mem = meminfo()
        return {"config": dict(self.cfg), "models": self.models(), "backends": self.backends(),
                "servers": {"llm": self.server("llm"), "stt": self.server("stt")},
                "mem_available_bytes": mem.get("MemAvailable"), "mem_total_bytes": mem.get("MemTotal"),
                "note": SWAP_NOTE}

    # -- choosing ------------------------------------------------------------------------
    def check_choice(self, values):
        """Return an error message for an unusable choice (missing file/build, too big), else None."""
        merged = dict(self.cfg)
        merged.update(values)
        try:
            validate_local_ai_config(merged)
        except ValueError as exc:
            return str(exc)
        backends = self.backends()
        mem = meminfo().get("MemAvailable")
        for kind, mkey, bkey in (("llm", "llm_model", "llm_backend"), ("stt", "stt_model", "stt_backend")):
            path = self.model_path(merged[mkey])
            if not path or not KINDS[kind][3](merged[mkey]):
                return "%s is not a %s model in %s" % (merged[mkey], "chat" if kind == "llm" else "speech",
                                                       self.models_dir)
            if merged[bkey] not in backends[kind]:
                return "no %s%s build found (have: %s)" % (KINDS[kind][1], merged[bkey], ", ".join(backends[kind]) or "none")
            if mem is not None and merged[mkey] != self.cfg[mkey]:
                current = self.server(kind, probe=False)["rss_bytes"] or 0  # freed when the old server stops
                need = int(os.path.getsize(path) * 1.15) + 256 * 1024 * 1024
                if need > mem + current:
                    return ("%s needs about %d MB but only %d MB of memory is available"
                            % (merged[mkey], need // 2 ** 20, (mem + current) // 2 ** 20))
        return None

    def env_lines(self):
        c = self.cfg
        return ["LOCAL_AI_DIR=%s" % c["dir"],
                "LOCAL_AI_LLM=%s" % os.path.join(self.models_dir, c["llm_model"]),
                "LOCAL_AI_STT=%s" % os.path.join(self.models_dir, c["stt_model"]),
                "LOCAL_AI_LLM_BACKEND=%s" % c["llm_backend"],
                "LOCAL_AI_STT_BACKEND=%s" % c["stt_backend"],
                "LOCAL_AI_THREADS=%d" % c["threads"]]

    # -- actions (run from an admin job thread) --------------------------------------------
    def run(self, action):
        """start | stop | restart. Returns (ok, output text)."""
        scripts = {"start": ["local-ai-start.sh"], "stop": ["local-ai-stop.sh"],
                   "restart": ["local-ai-stop.sh", "local-ai-start.sh"]}[action]
        out, ok = [], True
        for name in scripts:
            path = os.path.join(self.base_dir, "scripts", name)
            try:
                r = subprocess.run([path], cwd=self.base_dir, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=180)
                out.append((r.stdout + r.stderr).strip())
                ok = ok and r.returncode == 0
            except (OSError, subprocess.TimeoutExpired) as exc:
                out.append("%s: %s" % (name, type(exc).__name__))
                ok = False
            if not ok:
                break
        log.info("local AI %s: %s", action, "ok" if ok else "failed")
        return ok, "\n".join(o for o in out if o)[-4000:]

    def sample_wav(self):
        try:
            with open(os.path.join(self.cfg["dir"], SAMPLE_WAV), "rb") as f:
                return f.read(4 * 1024 * 1024)
        except OSError:
            return None

    def test_llm(self, question):
        """Ask the local chat model directly (no cloud). Returns a result dict."""
        t0 = time.monotonic()
        deadline = t0 + self.assistant.cfg["fallback_timeout_seconds"]
        if not fb.acquire_local(deadline):
            return {"ok": False, "error": "the local model is busy"}
        try:
            result = self.assistant._call_local(question, deadline)
        except Exception as exc:
            return {"ok": False, "error": getattr(exc, "category", type(exc).__name__),
                    "ms": int((time.monotonic() - t0) * 1000)}
        finally:
            fb.LOCAL_LOCK.release()
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return {"ok": True, "answer": result["answer"], "ms": int((time.monotonic() - t0) * 1000),
                "completion_tokens": usage.get("completion_tokens")}

    def test_stt(self):
        wav = self.sample_wav()
        if wav is None:
            return {"ok": False, "error": "sample %s not found" % SAMPLE_WAV}
        t0 = time.monotonic()
        deadline = t0 + self.voice.cfg["fallback_timeout_seconds"]
        if not fb.acquire_local(deadline):
            return {"ok": False, "error": "the local model is busy"}
        try:
            result = self.voice._call_local(wav, deadline)
        except Exception as exc:
            return {"ok": False, "error": getattr(exc, "category", type(exc).__name__),
                    "ms": int((time.monotonic() - t0) * 1000)}
        finally:
            fb.LOCAL_LOCK.release()
        return {"ok": True, "text": result["text"], "expected": SAMPLE_TEXT,
                "ms": int((time.monotonic() - t0) * 1000)}


def _cli(argv):
    """python3 localai.py env   print the chosen models for scripts/local-ai-start.sh"""
    if argv != ["env"]:
        print(_cli.__doc__)
        return 2
    import config  # local import: the dashboard's config loader
    cfg = config.load_config(os.path.join(config.BASE_DIR, "config.json"))
    for line in LocalAI(cfg["local_ai"], config.BASE_DIR, None, None).env_lines():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
