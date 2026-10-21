"""Local-model fallback helpers shared by assistant.py and voice.py.

Policy (the user's decision): the local model is used ONLY when the cloud API gives no response,
meaning no HTTP response headers arrive within `no_response_seconds` of starting the attempt
(DNS, connect, TLS and send included) or the connection fails before any headers. Any HTTP status
(401/402/403/429/5xx...) is a response and never falls back, and neither is a failure after the
headers have arrived. After a no-response the cloud is skipped for a cooldown, then exactly one
request probes it again.

Python 3 standard library only.
"""
import http.client
import re
import threading
import time
import urllib.error

import netsim

# One local model runs at a time across chat and voice: the CPU has only two physical cores.
LOCAL_LOCK = threading.Lock()


class NoResponse(Exception):
    """The cloud sent no HTTP response headers in time (or the connection failed before them)."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason  # "deadline" | "connect" | "straggler"


class HeaderDeadlineOpener:
    """Opens cloud requests with a hard deadline on receiving response headers.

    urllib's socket timeout applies to each socket operation and does not bound DNS resolution,
    so the open runs in a worker thread and the caller waits on it for at most the deadline.
    An abandoned worker closes any response it eventually gets. At most one abandoned worker is
    allowed per opener; while it is still alive, new attempts count as no-response immediately.
    """

    def __init__(self, opener, service=None):
        self._opener = opener
        self.service = service  # netsim service name ("ask", "voice") for the offline test switch
        self._lock = threading.Lock()
        self._straggler = None

    def open(self, req, header_deadline, socket_timeout):
        """Return the response once headers arrive. urllib.error.HTTPError (a status was received)
        propagates unchanged; anything else before headers raises NoResponse."""
        simulated = netsim.blocked(self.service) if self.service else None
        if simulated == "instant":
            raise NoResponse("simulated")
        with self._lock:
            if self._straggler is not None and self._straggler.is_alive():
                raise NoResponse("straggler")
            self._straggler = None
        box = {"abandoned": False}
        box_lock = threading.Lock()
        done = threading.Event()

        def work():
            try:
                if simulated:  # "realistic": behave like a black-holed connection
                    time.sleep(socket_timeout)
                    raise TimeoutError("timed out (offline test)")
                resp, exc = self._opener.open(req, timeout=socket_timeout), None
            except BaseException as e:  # handed to the waiting caller, or dropped if abandoned
                resp, exc = None, e
            with box_lock:
                if box["abandoned"]:
                    if resp is not None:
                        resp.close()
                    return
                box["resp"], box["exc"] = resp, exc
                done.set()

        worker = threading.Thread(target=work, name="cloud-open", daemon=True)
        worker.start()
        done.wait(header_deadline)
        with box_lock:
            if not done.is_set():
                box["abandoned"] = True
                with self._lock:
                    self._straggler = worker
                raise NoResponse("deadline")
        exc = box["exc"]
        if exc is None:
            return box["resp"]
        if isinstance(exc, urllib.error.HTTPError):
            raise exc
        if isinstance(exc, (OSError, http.client.HTTPException)):
            # DNS failure, refused/reset connection, TLS failure, socket timeout, closed without
            # a status line: no response was received.
            raise NoResponse("connect")
        raise exc


class CloudGate:
    """Per-service cooldown after a no-response, then a single probe. Monotonic clock, locked."""

    def __init__(self, cooldown_seconds, clock=time.monotonic):
        self.cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._until = None
        self._probing = False

    def begin(self):
        """True if this caller may try the cloud. Every True must be followed by finish()."""
        with self._lock:
            if self._until is None:
                return True
            if self._clock() < self._until or self._probing:
                return False
            self._probing = True
            return True

    def finish(self, responded):
        """Record the outcome of a cloud attempt: responded=False starts (or restarts) the cooldown."""
        with self._lock:
            self._probing = False
            self._until = None if responded else self._clock() + self.cooldown

    def reset(self):
        """Forget any cooldown (used when the offline test switch turns off)."""
        with self._lock:
            self._until = None
            self._probing = False

    def cooling_down(self):
        with self._lock:
            return self._until is not None and self._clock() < self._until


def acquire_local(deadline):
    """Take LOCAL_LOCK, waiting at most until the monotonic `deadline`. Returns True if acquired."""
    return LOCAL_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic()))


# --- plain text for local answers ------------------------------------------------------------
# Small local models often answer with markdown (**bold**, "* " bullets) and LaTeX ($F = C \\times 1.8$),
# which the plain-text answer box would show as raw symbols.
_LATEX_SYMBOLS = {"times": "×", "cdot": "·", "div": "÷", "pm": "±", "approx": "≈", "le": "≤", "leq": "≤",
                  "ge": "≥", "geq": "≥", "neq": "≠", "circ": "°", "degree": "°"}


def plain_text(text):
    s = re.sub(r"\^\{?\\circ\}?", "°", text)
    s = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}",
               lambda m: "/".join(p if re.fullmatch(r"[\w.]+", p) else "(%s)" % p for p in m.groups()), s)
    s = re.sub(r"\\(?:text|mathrm|mathbf|textbf|operatorname)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\(" + "|".join(sorted(_LATEX_SYMBOLS, key=len, reverse=True)) + r")(?![A-Za-z])",
               lambda m: _LATEX_SYMBOLS[m.group(1)], s)
    s = re.sub(r"\\(?:left|right)(?![A-Za-z])", "", s)
    s = re.sub(r"\\[()\[\]]", "", s)
    # Paired $...$ math only: "$5 y $10" is two prices, not a formula (closing $ before a digit).
    s = re.sub(r"(?<![\w$])\$\$?(?=\S)([^$\n]+?)(?<=\S)\$\$?(?![\w$])", r"\1", s)
    s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), s, flags=re.S)
    s = re.sub(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])", r"\1", s)
    s = re.sub(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+", "", s)               # headings
    s = re.sub(r"(?m)^([ \t]*)[*+-][ \t]+", r"\1• ", s)           # bullets
    s = re.sub(r"`([^`\n]*)`", r"\1", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
