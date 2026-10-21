"""Offline test switch: make the dashboard behave as if the internet were down, for testing the
local fallbacks and the stale-data display. Only this process is affected; the server's real
network (and every other service on it) is untouched.

State lives in data/offline-test.json, {"services": [...], "mode": "realistic"|"instant",
"until": <unix seconds>}, so the admin page and scripts/offline-test.sh can both flip it. It always
expires: at most MAX_MINUTES after it was turned on.

Modes, for the cloud AI calls (Ask, Voice):
  realistic  the request "hangs" like a black-holed connection, so the real no-response deadline,
             the straggler guard and the cooldown-then-probe all run exactly as in an outage;
  instant    the request fails at once with no response (quick checks of the local path).
Weather, maps and calendar fetches fail with their normal network error in either mode.

Python 3 standard library only.
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("dashboard.netsim")

SERVICES = ("ask", "voice", "weather", "maps", "calendar")
MODES = ("realistic", "instant")
MAX_MINUTES = 120
MAX_FILE_BYTES = 4096


class OfflineSim:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._sig = None
        self._state = None       # parsed file contents, or None
        self._was_active = False
        self._listeners = []

    def on_change(self, fn):
        """fn(active: bool) runs whenever the switch turns on or off (including expiry)."""
        self._listeners.append(fn)

    def _read(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return None, None
        except OSError:
            return "error", None
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return sig, self._state
        state = None
        try:
            with open(self.path, encoding="utf-8") as f:
                state = parse(f.read(MAX_FILE_BYTES))
        except (OSError, ValueError):
            log.warning("offline test: %s unreadable; treating as off", self.path)
        return sig, state

    def state(self):
        """{"active", "services", "mode", "until", "remaining_seconds"}."""
        fire = None
        with self._lock:
            sig, state = self._read()
            self._sig, self._state = sig, state
            now = self.clock()
            active = bool(state) and state["until"] > now
            if state and not active:
                self._remove_locked()
            if active != self._was_active:
                self._was_active = active
                fire = active
                log.warning("offline test %s%s", "ON: " if active else "off",
                            "%s (%s)" % (",".join(state["services"]), state["mode"]) if active else "")
            out = {"active": active, "services": state["services"] if active else [],
                   "mode": state["mode"] if active else None, "until": state["until"] if active else None,
                   "remaining_seconds": int(state["until"] - now) if active else 0}
        if fire is not None:
            for fn in self._listeners:
                try:
                    fn(fire)
                except Exception:
                    log.exception("offline test listener failed")
        return out

    def blocked(self, service):
        """The mode ("realistic"/"instant") if `service` is simulated offline right now, else None."""
        st = self.state()
        return st["mode"] if st["active"] and service in st["services"] else None

    def turn_on(self, services, mode="realistic", minutes=15):
        services, mode, minutes = validate(services, mode, minutes)
        text = json.dumps({"services": services, "mode": mode, "until": int(self.clock() + minutes * 60)})
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp, self.path)
        return self.state()

    def turn_off(self):
        with self._lock:
            self._remove_locked()
        return self.state()

    def _remove_locked(self):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("offline test: could not remove %s", self.path)


def validate(services, mode, minutes):
    if isinstance(services, str):
        services = [s.strip() for s in services.split(",") if s.strip()]
    if (not isinstance(services, list) or not services
            or not all(isinstance(s, str) and s in SERVICES for s in services)):
        raise ValueError("services must be some of: %s" % ", ".join(SERVICES))
    if mode not in MODES:
        raise ValueError("mode must be realistic or instant")
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or not 1 <= minutes <= MAX_MINUTES:
        raise ValueError("minutes must be 1-%d" % MAX_MINUTES)
    return sorted(set(services), key=SERVICES.index), mode, minutes


def parse(text):
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    services, mode, _ = validate(data.get("services"), data.get("mode"), 1)
    until = data.get("until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        raise ValueError("bad until")
    return {"services": services, "mode": mode, "until": until}


# The server installs one instance; modules call blocked() without passing it around.
SIM = None


def install(path, clock=time.time):
    global SIM
    SIM = OfflineSim(path, clock)
    return SIM


def blocked(service):
    sim = SIM
    return sim.blocked(service) if sim is not None else None


def _cli(argv):
    """scripts/offline-test.sh on [services] [minutes] [instant] | off | status"""
    base = os.path.dirname(os.path.abspath(__file__))
    sim = OfflineSim(os.path.join(base, "data", "offline-test.json"))
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "on":
            services = argv[1] if len(argv) > 1 else "ask,voice"
            minutes = float(argv[2]) if len(argv) > 2 else 15
            mode = argv[3] if len(argv) > 3 else "realistic"
            st = sim.turn_on(services, mode, minutes)
        elif cmd == "off":
            st = sim.turn_off()
        elif cmd == "status":
            st = sim.state()
        else:
            print(_cli.__doc__)
            return 2
    except ValueError as exc:
        print("error: %s" % exc)
        return 2
    if st["active"]:
        print("OFFLINE TEST ON: %s, %s mode, %d min %02d s left"
              % (", ".join(st["services"]), st["mode"], st["remaining_seconds"] // 60, st["remaining_seconds"] % 60))
    else:
        print("offline test is off")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
