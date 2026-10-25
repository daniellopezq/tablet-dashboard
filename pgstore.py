"""PostgreSQL storage for history (weather readings today; logs and events later).

Uses the local PostgreSQL 16 through the `psql` client, so the dashboard keeps to the Python standard library:
no driver to install. Login is Ubuntu's "peer" authentication over the Unix socket (the Linux user that runs the
dashboard is a database role of the same name), so there is no password anywhere. One-time setup, run by the owner:

    sudo -u postgres createuser "$USER"
    sudo -u postgres createdb -O "$USER" dashboard

Values never go into the SQL text: each one is a psql variable (`-v name=value`) quoted by psql as a literal
(`:'name'`), and the script is fed on stdin, where psql applies that quoting. Tables are created on startup.
A database problem never breaks the dashboard: writes are skipped and logged once, reads return nothing.
"""
import json
import logging
import re
import subprocess
import threading
import time

log = logging.getLogger("dashboard.pgstore")

DEFAULT_DATABASE_CONFIG = {
    "enabled": False,  # on in this server's config.json; off by default so tests never touch the real database
    "name": "dashboard",
    "psql_path": "psql",
    "timeout_seconds": 10,
}
NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
VAR_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
RETRY_SECONDS = 300  # after a failure, try the database again this long later

SCHEMA = """
create table if not exists weather_readings (
    id            bigserial primary key,
    fetched_at    timestamptz not null,
    observed      text,              -- Open-Meteo's local observation time, e.g. 2026-10-23T22:30
    place         text not null,
    temperature   real,
    humidity      real,
    code          integer,
    condition     text,
    is_day        boolean,
    precipitation real,              -- mm in the 15 minutes before `observed`
    wind_speed    real,              -- km/h
    wind_gusts    real,              -- km/h
    today_min     real,
    today_max     real,
    today_precip  real,              -- mm forecast for the whole day
    today_precip_prob integer,       -- %
    raw           jsonb not null,    -- the full normalized reading, so nothing is lost
    unique (place, observed)
);
create index if not exists weather_readings_fetched_at on weather_readings (fetched_at);
"""


def validate_database_config(d):
    """Raise ValueError (caller wraps it) for unusable settings."""
    if not isinstance(d["enabled"], bool):
        raise ValueError("database.enabled must be true or false")
    if not isinstance(d["name"], str) or not NAME_RE.match(d["name"]):
        raise ValueError("database.name must be a plain database name like dashboard")
    if not isinstance(d["psql_path"], str) or not d["psql_path"]:
        raise ValueError("database.psql_path must be a program name or path")
    t = d["timeout_seconds"]
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 1 <= t <= 60:
        raise ValueError("database.timeout_seconds must be 1-60")


class DatabaseError(Exception):
    pass


def _value(v):
    """psql variables are text; None becomes '' (read back with NULLIF)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return str(v)


class Database:
    def __init__(self, cfg, runner=subprocess.run, clock=time.monotonic):
        self.cfg = cfg
        self.runner = runner
        self.clock = clock
        self._lock = threading.Lock()
        self._ready = False
        self._failed_at = None
        self._warned = False

    def run(self, sql, **params):
        """Run a script; returns psql's unaligned output. Raises DatabaseError."""
        argv = [self.cfg["psql_path"], "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.cfg["name"]]
        for name, value in params.items():
            if not VAR_RE.match(name):
                raise ValueError("bad variable name %r" % name)
            argv += ["-v", "%s=%s" % (name, _value(value))]
        try:
            proc = self.runner(argv, input=sql.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=self.cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            raise DatabaseError("the database took too long")
        except OSError as exc:
            raise DatabaseError("could not run psql (%s)" % type(exc).__name__)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise DatabaseError(err[-1][:200] if err else "psql exited %d" % proc.returncode)
        return (proc.stdout or b"").decode("utf-8", "replace")

    def query_json(self, sql, **params):
        """Rows of a SELECT as a list of dicts (the query is wrapped in json_agg)."""
        out = self.run("select coalesce(json_agg(t), '[]'::json) from (%s) t;" % sql.rstrip().rstrip(";"),
                       **params).strip()
        return json.loads(out or "[]")

    def ready(self):
        """True once the schema exists. After a failure, retries every RETRY_SECONDS."""
        if not self.cfg["enabled"]:
            return False
        with self._lock:
            if self._ready:
                return True
            if self._failed_at is not None and self.clock() - self._failed_at < RETRY_SECONDS:
                return False
            try:
                self.run(SCHEMA)
            except DatabaseError as exc:
                self._failed_at = self.clock()
                if not self._warned:
                    log.warning("database: not available (%s); history is not being saved", exc)
                    self._warned = True
                return False
            self._ready = True
            self._failed_at = None
            if self._warned:
                log.info("database: available again")
            self._warned = False
            return True

    # --- weather ----------------------------------------------------------------------------

    def record_weather(self, place, data):
        """Save one normalized reading (weather.WeatherService.normalize output). Never raises."""
        if not self.ready():
            return False
        cur, today = data.get("current") or {}, data.get("today") or {}
        try:
            self.run("""
insert into weather_readings (fetched_at, observed, place, temperature, humidity, code, condition, is_day,
    precipitation, wind_speed, wind_gusts, today_min, today_max, today_precip, today_precip_prob, raw)
values (now(), nullif(:'observed', ''), :'place', nullif(:'temperature', '')::real, nullif(:'humidity', '')::real,
    nullif(:'code', '')::integer, nullif(:'condition', ''), nullif(:'is_day', '')::boolean,
    nullif(:'precipitation', '')::real, nullif(:'wind_speed', '')::real, nullif(:'wind_gusts', '')::real,
    nullif(:'today_min', '')::real, nullif(:'today_max', '')::real, nullif(:'today_precip', '')::real,
    nullif(:'today_precip_prob', '')::integer, :'raw'::jsonb)
on conflict (place, observed) do nothing;
""", observed=cur.get("observed"), place=place or "home", temperature=cur.get("temperature"),
                     humidity=cur.get("humidity"), code=cur.get("code"), condition=cur.get("condition"),
                     is_day=cur.get("is_day"), precipitation=cur.get("precipitation"),
                     wind_speed=cur.get("wind_speed"), wind_gusts=cur.get("wind_gusts"),
                     today_min=today.get("min"), today_max=today.get("max"), today_precip=today.get("precipitation"),
                     today_precip_prob=today.get("precipitation_probability"), raw=data)
        except DatabaseError as exc:
            log.warning("database: weather reading not saved (%s)", exc)
            return False
        return True

    def weather_summary(self, hours=24):
        """{readings, min, max, rain_mm, rained, last_rain} over the last `hours`, or None (no data/database)."""
        if not self.ready():
            return None
        try:
            rows = self.query_json("""
select count(*) as readings, min(temperature) as min, max(temperature) as max,
       round(coalesce(sum(precipitation), 0)::numeric, 1) as rain_mm,
       bool_or(coalesce(precipitation, 0) > 0 or code between 51 and 99) as rained,
       max(observed) filter (where coalesce(precipitation, 0) > 0 or code between 51 and 99) as last_rain
from weather_readings where fetched_at > now() - make_interval(hours => nullif(:'hours', '')::integer)
""", hours=int(hours))
        except (DatabaseError, ValueError) as exc:
            log.warning("database: weather summary failed (%s)", exc)
            return None
        row = rows[0] if rows else None
        return row if row and row.get("readings") else None
