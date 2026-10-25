"""Read-only JSON endpoints: time, weather, maps, calendar, uptime, home status, config."""


def get_time(h, path, head):
    h.send_json(h.app.time_payload(), head)


def get_weather(h, path, head):
    h.send_json(h.app.weather.snapshot(), head)


def get_maps(h, path, head):
    h.send_json(h.app.maps.snapshot(), head)


def get_calendar(h, path, head):
    h.send_json(h.app.calendar.snapshot(), head)


def get_uptime(h, path, head):
    h.send_json(h.app.health.public_snapshot(), head)


def get_home(h, path, head):
    h.send_json(h.app.home.status(), head)


def get_config(h, path, head):
    h.send_json(h.app.config_payload(), head)


def get_assistant_status(h, path, head):
    h.send_json(h.app.assistant.status(), head)


def get_voice_status(h, path, head):
    h.send_json(h.app.voice.status(), head)


def not_found(h, path, head):
    h.send_json({"error": "not found"}, head, status=404)
