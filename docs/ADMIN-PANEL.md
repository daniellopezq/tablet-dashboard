# Admin Panel: Reference

The browser admin page for the tablet dashboard, introduced in **0.9.0**. This doc covers its internals: modules, security model, API, config sections and extension points. For how to use it, see the [README](../README.md#admin-page). For the offline test switch's effect on the fallback, see [LOCAL-FALLBACK-ARCHITECTURE.md](LOCAL-FALLBACK-ARCHITECTURE.md#8-testing).

- URL: `https://<server>:8443/admin`, LAN only.
- First run: `scripts/set-admin-password.sh` on the server.

---

## 1. Modules

| Module | Role |
|---|---|
| `admin/` | `AdminService` (`service.py`): sign-in, sessions, the `/api/admin/*` entry point, background jobs. One mixin per page: `keys.py`, `oauth.py` (Google sign-in), `ai.py` (models, offline switch, fallback tests), `settings.py` (config saves), `ops.py` (overview, health, logs, usage, maintenance, restart). Shared constants, schemas and password hashing in `core.py`. CLI: `python3 -m admin set-password`. |
| `health.py` | `HealthChecker` (background thread, history ring buffers), TCP/HTTP checks, `KumaClient` (status page, `/metrics`, push). |
| `localai.py` | `LocalAI`: lists model files and builds under `local-ai/`, RAM guard, start/stop via the scripts, direct test calls. CLI: `python3 localai.py env`, which `scripts/local-ai-start.sh` reads. |
| `netsim.py` | `OfflineSim`: the offline test switch (`data/offline-test.json`). CLI: `scripts/offline-test.sh`. |
| `envfile.py` | Reads and writes `.env` (mode 600, atomic, name and value checks). `gcal.py` keeps thin aliases to it. |
| `web/admin.{html,js,css}` | The page. Plain JS, no inline script or style (strict CSP), and no tablet ES5 limits. |

`App` in `server.py` builds `localai`, `admin` and `health` next to the other services. `routes/admin_api.py` checks https + LAN and the body limits, then calls `AdminService.handle()`, which finds the endpoint by name (`api_<method>_<route>`) on the class or one of its mixins. `main()` calls `netsim.install(...)` and starts and stops the health thread.

## 2. Security model

| Layer | Rule |
|---|---|
| Listener | Only the https server (`DashboardServer(..., is_tls=True)`) serves `/admin*` and `/api/admin/*`. On http :8099 they return 404, with a hint to use https. |
| Network | `assistant.is_lan_client`: only loopback and private addresses. Anyone else gets 404. |
| Password | One admin. `hashlib.scrypt` (n=2¹⁴, r=8, p=1, 32 bytes, random salt) in `data/admin.json` (mode 600). At least 10 characters. Compared in constant time. With no password set, the page says to run the script and accepts no sign-in. |
| Lockout | 5 failures per IP in 15 min block sign-in from that IP for the rest of the window. Failures are logged without the password. |
| Session | `secrets.token_urlsafe(32)`, in memory only. Cookie `dash_admin`: `HttpOnly; Secure; SameSite=Strict; Path=/`. Expires after 8 h idle or 7 days at most. A restart signs everyone out. |
| CSRF | Every POST needs `X-Admin: 1`, a same-origin `Origin`/`Host` (`assistant.check_origin`), `Content-Type: application/json`, a `Content-Length`, and a body no larger than `max_body_bytes` (64 KiB). |
| Secrets | Write-only. Responses show "set" or "…last4" (values shorter than 12 characters show only "set"). `GOOGLE_CLIENT_ID` is not secret and is shown in full. Values with CR, LF or NUL are refused, so nothing can inject extra `.env` lines. Keys are re-read on each use, so no restart is needed. |
| Errors | Upstream error text is cleaned before it is shown. Exceptions are logged server-side, never returned in full. |

## 3. API

All routes are under `/api/admin/` and return JSON `{"ok": true, ...}`, or `{"ok": false, "error": "..."}` with a 4xx status. `AdminService.handle()` maps `METHOD /a/b` to `api_<method>_<a>_<b>`. Every route except `session` and `login` needs a session.

| Route | Purpose |
|---|---|
| `GET session` · `POST login` · `POST logout` | Sign-in state (also reports whether a password is set) |
| `GET job` | The current or last background job: `{id, kind, state: running/done/error, result}` |
| `GET overview` | Version, uptime, PID, disk, memory, certificate expiry, one line per service |
| `GET keys` · `POST keys/set` · `POST keys/remove` · `POST keys/test` | `.env` secrets. Test runs as a job: gateway or Groq model list, one OWM tile, a Google token refresh, Kuma `/metrics`, or a Kuma push. |
| `GET calendar` · `POST calendar/start` · `calendar/finish` · `calendar/cancel` · `calendar/revoke` | Google connect: PKCE plus a loopback listener. You can also paste the redirect URL. Pending state lasts 10 min. |
| `GET health` · `POST health/run` | Health snapshot; run all checks now (as a job) |
| `GET settings` · `POST settings` | The editable `config.json` fields (`SETTINGS` schema) |
| `GET ai` · `POST ai/models` · `ai/save` · `ai/test` | Cloud model and fallback keys per service (`AI_KEYS`), the provider's model list, and test calls (as jobs) |
| `POST ai/local/save` · `ai/local/action` | The `local_ai` section; start, stop or restart llama or whisper |
| `GET offline` · `POST offline` · `offline/off` · `offline/fallback-test` | The offline test switch, and one Ask plus one Voice round trip |
| `GET logs` · `GET usage` | Log tails (dashboard, llama, whisper); usage totals for today and this month |
| `POST maint` | `action`: `refresh`, `clear_maps`, `clear_ai_block`, `backup`, `restart` |

**Jobs:** only one runs at a time; a second gets 409. The page polls `GET job` until `state` isn't `running`.

## 4. Config sections (`config.json`)

Every dict in `DEFAULT_CONFIG` is a section. Partial overrides merge one level deep and are validated at start-up and on every save.

**`admin`**

| Key | Default |
|---|---|
| `enabled` | `true` |
| `password_file` | `data/admin.json` |
| `session_idle_hours` | `8` |
| `session_max_days` | `7` |
| `max_body_bytes` | `65536` |

**`health`**

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `interval_seconds` | `60` | 15–3600 |
| `timeout_seconds` | `3` | 0.5–30, per check |
| `google_token_max_age_days` | `7` | `null` once the Google app is published |
| `disk_warn_gb` | `5` | |
| `targets` | `[]` | At most 40, unique names. Each is `{"name","type":"tcp","host","port"}` or `{"name","type":"http","url"}`. HTTP counts any status below 500 as up. |

**`kuma`**

| Key | Default | Notes |
|---|---|---|
| `enabled` | `false` | |
| `base_url` | `""` | e.g. `http://<host>:3001` |
| `status_page_slug` | `""` | Public status page, no key needed |
| `use_metrics` | `false` | Reads `/metrics` instead, with basic auth `":" + KUMA_API` (older name `KUMA_API_KEY` also accepted) |
| `push_enabled` | `false` | Sends a heartbeat to `KUMA_PUSH_URL` after each check |
| `timeout_seconds` | `5` | |
| `max_response_bytes` | 2 MiB | |
| `env_file` | `.env` | |

**`local_ai`**: see [LOCAL-FALLBACK-ARCHITECTURE.md §6](LOCAL-FALLBACK-ARCHITECTURE.md#6-interfaces).

**Saving:**
1. Apply the changes to a copy.
2. Validate it with `load_config` on a temp file.
3. Copy the old file to `backups/config-<time>.json`, next to the config file.
4. Replace the config file atomically.

Settings that apply live are pushed to the running service; for example, `apply_settings()` on assistant and voice swaps their cfg and their gate cooldown. Fields marked restart-needed in `SETTINGS` bring up the restart banner.

**Restart** re-executes the process with `os.execv`, after the response has been sent. The PID stays the same, so under systemd (0.18.0) the service simply stays `active`; it is not counted as a crash. Any restart signs the admin out, because sessions are in memory.

## 5. Health checks

The built-in checks come from `AdminService.builtin_checks()`:
- both listeners
- weather, maps and calendar freshness (while loading, this shows as warn)
- each API key present
- the Google sign-in lifetime: warn under 2 days left, fail when expired
- local llama and whisper health
- disk free
- the https certificate: warn under 30 days, fail when expired
- the offline switch: warn while it is on

On top of those come the `health.targets` checks and the Kuma monitors. Since 0.12.0, a target whose name is the media server switched off on purpose from the Home tab (`Home.intentionally_stopped()`, e.g. Jellyfin while Plex runs) is skipped instead of reported down. The API keys page also lists `HA_TOKEN` (Home Assistant long-lived token); its Test button calls HA's `GET /api/`. Each check keeps its state (`ok`, `warn`, `fail`, `unknown`), the time it last changed, and a history string (one letter per run: `o`, `w`, `f`, `u`) covering about 24 h. History is kept in memory only.

Example targets: a media server (`tcp`, port 32400), a file share (`tcp`, port 445), or a web app (`http`). With Kuma enabled, every monitor of your Kuma instance is listed too.

**Public snapshot (0.11.0).** `HealthChecker.public_snapshot()` feeds `GET /api/uptime` (LAN only), which the tablet's Uptime section and the modern view's Uptime page and Overview card read. It carries only names, the group (`kuma` or `service`), state, ping, uptime % and the last 30 history letters: no hostnames, URLs or error text. The admin `GET health` keeps the full detail. Built-in checks (listeners, keys, certificate…) stay admin-only.

## 6. Tests

| File | Tests | Covers |
|---|---:|---|
| `tests/test_admin.py` | 27 | http and non-LAN rejection, session required, CSRF header and Origin checks, no-password state, wrong password and lockout, idle expiry, hashing; key set, mask and remove (keeps the `GROQ` alias, refuses bad values, pasted newlines and an unsafe `.env`); settings saved live vs restart-needed, invalid saves change nothing; AI save applied live; offline switch and the tablet flag; Google connect by pasted redirect; the restart hook; logs and usage |
| `tests/test_health.py` | 16 | Kuma metrics and status-page parsers and clients (fixtures), push, config validation, TCP and HTTP checks, checker history, a failing built-in check is reported rather than raised; `localai`: model inventory, the path guard, the RAM guard, env lines |
| `tests/test_netsim.py` | 11 | expiry, parsing, instant and realistic modes, gate reset |

Run all of them with `python3 -m unittest discover -s tests`: 327 tests at 0.25.1 (269 at 0.21.2, 247 at 0.18.0, 182 at 0.9.0).

## 7. Not done yet

- Alerts on state changes (ntfy or email)
- Audit log of admin actions
- Tablet controls (night dimming, forced reload, default tab)
- Camera list editor (cameras are still edited in `config.json`). Since 0.20.0 the server follows a moved RTSP camera by MAC on its own, in memory only, and logs a warning to update `config.json`.
- `BLINK_PROXY_TOKEN` (0.21.0, Blink live view) on the API keys page: for now it's added to `.env` by hand
- An autostart switch (install/remove is a script for now: `scripts/install-autostart.sh`)

Done since this list was written: the tablet "Servers" chip became the Uptime section (0.11.0), and the admin page is in the public repo (ported with 0.7.0–0.10.0).
