"""Tests for pgstore.py (PostgreSQL history through psql) and the weather fields it records.

A fake `psql` stands in for the database: nothing here touches the real PostgreSQL.
Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgstore  # noqa: E402
import weather  # noqa: E402


class FakePsql:
    """Records argv and stdin; answers with a canned stdout or fails."""

    def __init__(self, stdout=b"", returncode=0, stderr=b""):
        self.calls = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

    def __call__(self, argv, input=None, stdout=None, stderr=None, timeout=None):
        self.calls.append((argv, input.decode("utf-8")))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    def var(self, call, name):
        argv = self.calls[call][0]
        for i, a in enumerate(argv):
            if a == "-v" and argv[i + 1].startswith(name + "="):
                return argv[i + 1][len(name) + 1:]
        return None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def cfg(**over):
    c = dict(pgstore.DEFAULT_DATABASE_CONFIG, enabled=True)
    c.update(over)
    pgstore.validate_database_config(c)
    return c


READING = {
    "current": {"temperature": 17.2, "humidity": 79, "code": 61, "condition": "Light rain", "icon": "rain",
                "is_day": False, "observed": "2026-10-23T22:45", "precipitation": 0.4, "wind_speed": 3,
                "wind_gusts": 7.6},
    "today": {"date": "2026-10-23", "min": 14.8, "max": 20.8, "precipitation": 2.1, "precipitation_probability": 60},
    "forecast": [],
}


class DatabaseTests(unittest.TestCase):
    def test_off_by_default_and_validation(self):
        self.assertFalse(pgstore.DEFAULT_DATABASE_CONFIG["enabled"])
        fake = FakePsql()
        db = pgstore.Database(dict(pgstore.DEFAULT_DATABASE_CONFIG), runner=fake)
        self.assertFalse(db.record_weather("home", READING))
        self.assertEqual(fake.calls, [])
        for bad in ({"name": "dash board"}, {"name": "x;drop"}, {"timeout_seconds": 0}, {"enabled": "yes"}):
            with self.assertRaises(ValueError):
                cfg(**bad)

    def test_values_are_variables_never_sql_text(self):
        fake = FakePsql()
        db = pgstore.Database(cfg(), runner=fake)
        evil = dict(READING, current=dict(READING["current"], condition="x'); drop table weather_readings; --"))
        self.assertTrue(db.record_weather("Casa", evil))
        schema_call, insert_call = fake.calls
        self.assertIn("create table if not exists weather_readings", schema_call[1])
        argv, sql = insert_call
        self.assertEqual(argv[:9], ["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", "dashboard"])
        self.assertNotIn("drop table", sql)
        self.assertIn(":'condition'", sql)
        self.assertEqual(fake.var(1, "condition"), "x'); drop table weather_readings; --")
        self.assertEqual(fake.var(1, "precipitation"), "0.4")
        self.assertEqual(fake.var(1, "today_precip_prob"), "60")
        self.assertEqual(fake.var(1, "is_day"), "false")
        self.assertEqual(json.loads(fake.var(1, "raw"))["current"]["observed"], "2026-10-23T22:45")
        db.record_weather("Casa", READING)
        self.assertEqual(len(fake.calls), 3)  # schema only once

    def test_missing_values_become_null(self):
        fake = FakePsql()
        db = pgstore.Database(cfg(), runner=fake)
        db.record_weather("Casa", {"current": {"temperature": 20}, "today": {}})
        self.assertEqual(fake.var(1, "wind_gusts"), "")
        self.assertIn("nullif(:'wind_gusts', '')::real", fake.calls[1][1])

    def test_unavailable_database_is_skipped_and_retried_later(self):
        fake = FakePsql(returncode=2, stderr=b'psql: error: FATAL:  role "dashboard" does not exist\n')
        clock = Clock()
        db = pgstore.Database(cfg(), runner=fake, clock=clock)
        with self.assertLogs("dashboard.pgstore", "WARNING") as logs:
            self.assertFalse(db.record_weather("Casa", READING))
        self.assertIn('role "dashboard" does not exist', logs.output[0])
        self.assertFalse(db.record_weather("Casa", READING))
        self.assertEqual(len(fake.calls), 1)  # not retried within RETRY_SECONDS
        fake.returncode = 0
        clock.t += pgstore.RETRY_SECONDS + 1
        self.assertTrue(db.record_weather("Casa", READING))
        self.assertIsNone(db.weather_summary() if fake.stdout else None)

    def test_weather_summary(self):
        fake = FakePsql(stdout=b'[{"readings": 96, "min": 13.1, "max": 21.0, "rain_mm": 3.2, "rained": true, '
                               b'"last_rain": "2026-10-23T04:15"}]\n')
        db = pgstore.Database(cfg(), runner=fake)
        s = db.weather_summary(24)
        self.assertEqual(s["rain_mm"], 3.2)
        self.assertEqual(fake.var(1, "hours"), "24")
        self.assertIn("json_agg", fake.calls[1][1])
        fake.stdout = b'[{"readings": 0, "min": null, "max": null, "rain_mm": 0, "rained": null, "last_rain": null}]\n'
        self.assertIsNone(db.weather_summary())

    def test_bad_variable_name_refused(self):
        db = pgstore.Database(cfg(), runner=FakePsql())
        with self.assertRaises(ValueError):
            db.run("select 1", **{"x; drop": 1})


def wcfg():
    import config
    c = json.loads(json.dumps(config.DEFAULT_CONFIG["weather"]))
    c.update(latitude=31.7, longitude=-116.6)
    return c


class WeatherFieldTests(unittest.TestCase):
    def test_new_fields_are_optional_and_parsed(self):
        w = weather.WeatherService(wcfg(), "test")
        raw = {"current": {"time": "2026-10-23T22:45", "temperature_2m": 17.2, "relative_humidity_2m": 79,
                           "weather_code": 61, "is_day": 0, "precipitation": 0.4, "wind_speed_10m": 3,
                           "wind_gusts_10m": 7.6},
               "daily": {"time": ["2026-10-23", "2026-10-24"], "weather_code": [61, 3],
                         "temperature_2m_max": [20.8, 22.5], "temperature_2m_min": [14.8, 15.2],
                         "precipitation_sum": [2.1, 0], "precipitation_probability_max": [60, 17]}}
        d = w.normalize(raw)
        self.assertEqual((d["current"]["precipitation"], d["current"]["wind_gusts"], d["current"]["observed"]),
                         (0.4, 7.6, "2026-10-23T22:45"))
        self.assertEqual((d["today"]["precipitation"], d["forecast"][0]["precipitation_probability"]), (2.1, 17))
        for key in ("precipitation", "wind_speed_10m", "wind_gusts_10m", "time"):
            del raw["current"][key]
        del raw["daily"]["precipitation_sum"]
        d = w.normalize(raw)
        self.assertIsNone(d["current"]["precipitation"])
        self.assertIsNone(d["today"]["precipitation"])
        self.assertIn("precipitation,wind_speed_10m,wind_gusts_10m", w.build_url())

    def test_save_hook_failure_never_breaks_weather(self):
        w = weather.WeatherService(wcfg(), "test")
        w._download = lambda: {"current": {"temperature_2m": 1, "relative_humidity_2m": 2, "weather_code": 0},
                               "daily": {"time": ["2026-10-23"], "weather_code": [0], "temperature_2m_max": [3],
                                         "temperature_2m_min": [1]}}

        def boom(data):
            raise RuntimeError("db down")
        w.on_update = boom
        with self.assertLogs("dashboard.weather", "ERROR"):
            self.assertTrue(w.fetch_once())
        self.assertEqual(w.snapshot()["data"]["current"]["temperature"], 1)


if __name__ == "__main__":
    unittest.main()
