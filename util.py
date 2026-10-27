"""Small helpers shared by the dashboard modules. Python 3 standard library only."""
import time


def now_ms():
    return int(time.time() * 1000)


def num(v):
    """True for an int or float (a bool is not a number here)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)
