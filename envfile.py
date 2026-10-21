"""Private files for the tablet dashboard: the .env secrets file and other owner-only files.

Shared by gcal.py (sign-in), admin.py (API keys page) and the setup scripts. Values are never
logged. Python 3 standard library only.
"""
import os
import re
import secrets
import stat

FILE_MAX_BYTES = 16384
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def read_private(path, limit=FILE_MAX_BYTES):
    """(text, mtime_ns) of an owner-only regular file, or raise OSError / PermissionError."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
            raise PermissionError("insecure file")
        return os.read(fd, limit).decode("utf-8", "replace"), st.st_mtime_ns
    finally:
        os.close(fd)


def write_private(path, text):
    """Atomically write an owner-only (600) file."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = os.path.join(d, ".%s.%s.tmp" % (os.path.basename(path), secrets.token_hex(4)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _lines(env_path):
    try:
        raw, _ = read_private(env_path)
    except FileNotFoundError:
        return []
    return raw.splitlines()


def _matcher(names):
    return re.compile(r"^\s*(export\s+)?(%s)\s*=" % "|".join(re.escape(n) for n in names))


def read_values(env_path):
    """{NAME: value} for every assignment in the file (later lines win). Raises like read_private."""
    out = {}
    for line in _lines(env_path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            out[name.strip()] = value.strip()
    return out


def set_values(env_path, values):
    """Set NAME=value lines in the .env file, keeping all other lines."""
    for name, value in values.items():
        if not NAME_RE.match(name) or not isinstance(value, str) or re.search(r"[\r\n\x00]", value):
            raise ValueError("bad .env entry")
    pat = _matcher(values)
    kept = [ln for ln in _lines(env_path) if not pat.match(ln)]
    kept += ["%s=%s" % (k, v) for k, v in values.items()]
    write_private(env_path, "\n".join(kept) + "\n")


def remove_values(env_path, names):
    """Delete NAME= lines from the .env file. Returns how many lines were removed."""
    lines = _lines(env_path)
    pat = _matcher(names)
    kept = [ln for ln in lines if not pat.match(ln)]
    if len(kept) != len(lines):
        write_private(env_path, "\n".join(kept) + "\n" if kept else "")
    return len(lines) - len(kept)
