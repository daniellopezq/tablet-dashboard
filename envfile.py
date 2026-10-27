"""Private files for the tablet dashboard: the .env secrets file and other owner-only files.

Shared by gcal.py (sign-in), admin/ (API keys page) and the setup scripts. Values are never
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


# --- API keys ---------------------------------------------------------------------------------

KEY_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")


def load_api_key(env_path, names):
    """Return (key, problem). Exactly one is None. Messages never contain the key.

    `names` are variable names in priority order (e.g. a standard name plus an alias). The
    environment wins over the file. A .env file is used only if it is a regular, non-symlink
    file owned by this user with no group/other permissions.
    """
    key = ""
    for name in names:
        key = os.environ.get(name, "").strip()
        if key:
            break
    if not key:
        try:
            fd = os.open(env_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None, "not_configured"
        except OSError:
            return None, "insecure_file"
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
                return None, "insecure_file"
            raw = os.read(fd, 8192).decode("utf-8", "replace")
        finally:
            os.close(fd)
        found = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, sep, value = line.partition("=")
            if sep and name.strip() in names:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                found[name.strip()] = value.strip()
        for name in names:
            if found.get(name):
                key = found[name]
                break
        if not key:
            return None, "not_configured"
    if not KEY_RE.match(key):
        return None, "invalid_key_format"
    return key, None
