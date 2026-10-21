# Tablet Dashboard

Turn an old Android tablet into an always-on home dashboard: clock, weather, animated weather maps, a Google Calendar agenda,
a voice-enabled AI assistant, Home Assistant buttons and a Cameras tab with live view.

A small Python server on your home network does the work. It fetches weather, maps and calendar events, calls the AI
services and keeps API keys private. The tablet just shows a lightweight web page. The project was built for an Android 4.4
tablet (800 × 400, Chrome 50), so it runs on hardware most modern web apps can't support.

## What it does

- **Clock:** synced to the server's time, so the tablet's own clock doesn't need to be right. Has a 12h/24h toggle.
- **Weather:** current conditions plus a short forecast, with automatic retries and a "stale" label when data is old.
- **Weather maps:** precipitation, cloud, wind and temperature layers over a darkened street map, rotating in a slideshow.
- **Calendar:** a read-only agenda of upcoming Google Calendar events.
- **Ask:** type or speak a question and get a short AI answer. Voice uses one tap and stops on its own when you stop talking.
- **Built for always-on use:** fullscreen mode, a diagnostics panel, usage limits so free API tiers aren't exceeded, and
  old data stays on screen when a service is down.
- **Local fallback:** when the internet is down, small models on the server (llama.cpp and whisper.cpp) answer Ask and Voice
  instead, and the page labels those answers.
- **Admin page:** a password-protected, LAN-only page for API keys, AI models (cloud and local), Google sign-in, health checks
  and optional Uptime Kuma monitors, settings, logs and an offline test switch for trying the fallbacks.
- **Touch-friendly:** big Back/Next buttons to move between sections, and long answers paged with big buttons, for poor
  touchscreens.
- **Home:** big tiles that toggle Home Assistant lights, plugs, scripts and alarms, with live ON/OFF state.
- **Cameras:** snapshots and motion pop-ups from Home Assistant cameras (e.g. Blink), plus live view of local RTSP
  cameras (e.g. V380) that keeps working without the internet.

## Why it's useful

- Reuses a tablet that would otherwise be e-waste.
- Self-hosted and LAN-only. API keys never reach the browser, and logs never store your questions, answers or audio.
- Light enough for 1 GB RAM devices, and a very old browser is enough.
- Every external service is optional. Each one stays off until you add its key.

## Tools and services

| Area | Used |
|---|---|
| Server | Python 3.9+ standard library (`http.server`, `ssl`, `urllib`, `zoneinfo`); no frameworks |
| Front end | Plain HTML, CSS and ES5 JavaScript, Web Audio and getUserMedia; no build step |
| Weather | [Open-Meteo](https://open-meteo.com/) Forecast API (no key) |
| Maps | [OpenWeatherMap](https://openweathermap.org/) tiles over [OpenStreetMap](https://www.openstreetmap.org/) base tiles, composited with Pillow |
| Calendar | Google Calendar API (read-only OAuth with PKCE) |
| AI assistant | [Vercel AI Gateway](https://vercel.com/docs/ai-gateway) (default model: Gemini 2.5 Flash-Lite) |
| Speech to text | [Groq](https://groq.com/) Whisper large-v3-turbo, with ffmpeg for audio conversion |
| Local fallback (optional) | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` (default Qwen3.5-2B Q4_K_M) and [whisper.cpp](https://github.com/ggml-org/whisper.cpp) `whisper-server` (base), loopback only |
| Home and cameras (optional) | Your own [Home Assistant](https://www.home-assistant.io/) (REST API, long-lived token); RTSP cameras through ffmpeg (MJPEG for old browsers) |
| Uptime (optional) | Built-in TCP/HTTP checks; [Uptime Kuma](https://github.com/louislam/uptime-kuma) status page or `/metrics` |
| HTTPS | OpenSSL private CA, for microphone access on the LAN |
| Tests | `unittest` with mock servers for every upstream service (227 tests) |

## Quick start

```bash
git clone <this repo> tablet-dashboard && cd tablet-dashboard
cp config.example.json config.json   # set your timezone and weather location
python3 server.py --check            # validate the config
scripts/start.sh                     # runs in the background, log in logs/dashboard.log
```

Open `http://<server-ip>:8099/` on the tablet. Stop the server with `scripts/stop.sh`. There is no autostart; use a systemd
unit or cron `@reboot` if you want one.

Optional features need extra setup and stay disabled or show a "not set up" message until you do it:

| Feature | Needs |
|---|---|
| Weather | Latitude and longitude in `config.json` (Open-Meteo, no key) |
| Weather maps | `OPENWEATHER_API_KEY` in `.env`, plus `python3-pil` |
| Calendar | A Google OAuth client and a one-time sign-in (see below) |
| Ask tab | `AI_GATEWAY_API_KEY` (Vercel AI Gateway) in `.env` |
| Voice input | `GROQ_API_KEY` in `.env`, `ffmpeg`, and https for one-tap recording |
| Admin page | https, and a password set with `scripts/set-admin-password.sh` |
| Local fallback | llama.cpp and whisper.cpp builds plus model files in a `local-ai/` folder next to this project (see "Local fallback") |
| Uptime Kuma | A Kuma instance; `base_url` plus a status page slug, or `KUMA_API` in `.env` |

## Layout

| Path | Purpose |
|---|---|
| `server.py` | HTTP(S) server: static files, `/api/*`, `/media/*`, background weather fetcher |
| `assistant.py` | AI assistant: key loading, usage limits, Vercel AI Gateway call, local fallback for Ask |
| `voice.py` | Voice input: upload checks, ffmpeg conversion, Groq Whisper transcription, local fallback for Voice |
| `fallback.py` | Local-fallback mechanism shared by Ask and Voice: header deadline, cooldown gate, local lock, plain-text cleaner |
| `maps.py` | Weather maps: OpenWeatherMap and OpenStreetMap tiles, rate limiting, Pillow compositing |
| `gcal.py` | Google Calendar: background fetcher, plus the `auth`, `status`, `test` and `revoke` commands |
| `admin.py` | Admin page backend: password sign-in, API keys, Calendar sign-in, settings, AI models, offline test, logs, maintenance |
| `health.py` | Health checks and the optional Uptime Kuma client (status page, `/metrics`, push heartbeat) |
| `localai.py` | Local model inventory, choice (`local_ai` in config) and start/stop for the admin page |
| `netsim.py` | Offline test switch (simulated internet outage inside the dashboard only) |
| `envfile.py` | Owner-only file helpers for `.env` and other private files |
| `config.example.json` | Example settings; copy to `config.json` (gitignored) |
| `.env` | **Private** API keys (mode 600; never served, gitignored) |
| `web/` | The only directory served as static files |
| `media/` | Optional; the only directory served under `/media/` (mp4, m4v, webm) |
| `data/`, `logs/`, `run/` | Runtime state, log and PID file (created automatically, gitignored) |
| `scripts/` | `start.sh`, `stop.sh`, `make-cert.sh`, `set-assistant-key.sh`, `set-admin-password.sh`, `offline-test.sh`, `local-ai-start.sh`, `local-ai-stop.sh` |
| `tools/eval_local_llm.py` | Quality and speed check for a local LLM |
| `docs/` | System overview, admin panel reference, local fallback architecture, benchmarks and plan |
| `tests/` | Unit tests with mock upstream servers: `python3 -m unittest discover -s tests` |

## Configuration (`config.json`)

| Key | Default | Notes |
|---|---|---|
| `bind`, `port` | `0.0.0.0`, `8099` | |
| `https_port` | `null` | Optional https listener (see "Voice input") |
| `timezone` | `UTC` | Clock timezone (IANA name), independent of the server's own timezone |
| `clock_default_24h` | `true` | Default clock format. The 12h/24h button choice is remembered per browser. |
| `weather.latitude`, `weather.longitude` | `null` | **Set both to enable weather.** Decimal degrees. |
| `weather.label` | `""` | Place name shown above the current conditions |
| `weather.timezone` | `UTC` | Which calendar day the daily high/low and forecast use |
| `weather.temperature_unit` | `celsius` | `celsius` or `fahrenheit` |
| `weather.refresh_minutes` | `15` | Minimum 5 |
| `weather.stale_after_minutes` | `35` | Older data is marked stale |
| `assistant.*`, `voice.*`, `maps.*`, `calendar.*` | | See the sections below |
| `local_ai.*`, `health.*`, `kuma.*`, `admin.*` | | See "Local fallback" and "Admin page"; full tables in [docs/ADMIN-PANEL.md](docs/ADMIN-PANEL.md) |

The weather location is never guessed from the timezone or IP address. Keys starting with `_` are ignored, so you can use
them for comments. Restart after editing: `scripts/stop.sh && scripts/start.sh`.

## API keys (`.env`)

Keys live only in `.env` next to `server.py`, never in `config.json` or `web/`. Store one without it showing on screen or in
shell history:

```bash
scripts/set-assistant-key.sh                               # AI_GATEWAY_API_KEY
KEY_NAME=GROQ_API_KEY scripts/set-assistant-key.sh         # voice
KEY_NAME=OPENWEATHER_API_KEY scripts/set-assistant-key.sh  # maps
scripts/set-assistant-key.sh --remove                      # delete a key (KEY_NAME applies here too)
```

The admin page's **API keys** page does the same in the browser: save, replace, remove and test `AI_GATEWAY_API_KEY`,
`GROQ_API_KEY`, `OPENWEATHER_API_KEY`, `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`, `KUMA_API`, `KUMA_PUSH_URL` and `HA_TOKEN`. It never
shows a stored value, only the last 4 characters, and changes apply without a restart.

The server refuses to use a `.env` that is group- or world-readable, a symlink, or owned by another user. Environment
variables with the same names take priority over the file.

## HTTP routes

| Route | Behaviour |
|---|---|
| `/` and static files | Only from `web/`; html, css, js, svg, png, ico, txt and crt. No directory listings. Dotfiles, `..`, encoded traversal, backslashes, NUL bytes and symlinks leaving `web/` return 404. |
| `/api/time` | Server time, timezone and UTC offset; never cached |
| `/api/weather` | Cached weather plus status (`unconfigured`, `loading`, `error`, `ok`) and a `stale` flag. Never contacts Open-Meteo during the request. |
| `/api/calendar` | Cached agenda grouped by day, plus status. LAN clients only. |
| `/api/uptime` | Uptime Kuma monitors, then the `health.targets` services: names, states, ping, uptime % and recent history only (no hosts or URLs). LAN clients only. |
| `/api/home` | Home tab buttons with live state (HA entity states, which media server runs, whether a switch is in progress). LAN clients only. |
| `POST /api/home/action` | Press one button (`{"id": "..."}`). Same-origin, LAN clients only, 2 s debounce per button. |
| `/api/cameras` | Camera list (label, availability, image version and time, motion, battery, live) and the current motion `alert`. LAN clients only. |
| `/api/cameras/<id>.jpg?v=<version>` | Latest still of one camera (from HA by the server, so the token never reaches the tablet, or from the RTSP feed). LAN clients only. |
| `/api/cameras/<id>/live.mjpg` | Live view of an RTSP camera as MJPEG (`multipart/x-mixed-replace`) for an `<img>`; ends after `live_max_seconds`. At most 4 viewers per camera. LAN clients only. |
| `POST /api/cameras/snapshot` | Ask one HA camera for a new picture (`{"id": "..."}`); wakes battery cameras, so rate limited. Same-origin, LAN only. |
| `/api/config` | Client settings (version, timezone, clock format, weather unit, https port) |
| `POST /api/assistant`, `/api/assistant/status` | Ask one question; setup state and remaining quota |
| `POST /api/transcribe`, `/api/voice/status` | Speech to text; setup state and remaining quota |
| `/api/maps`, `/maps/<layer>.png`, `/maps/<layer>_small.png` | Map status and rendered images |
| `/media/<file>` | Only from `media/`, with byte-range support |
| `/admin`, `/api/admin/*` | Admin page and its JSON API: **https listener and LAN clients only** (404 otherwise). See "Admin page". |

Only GET and HEAD are accepted, apart from the POST routes above. Idle connections time out after 20 s.

## Client behaviour

- **Clock:** driven by server time, so the tablet's own clock and timezone don't need to be right. Each sync corrects for
  half the round-trip time; the display then advances locally. Syncs run every 60 s, every 15 s after a failure, and
  whenever the page becomes visible or the local clock jumps. The footer shows sync status (green, amber or red).
- **Weather:** the server fetches Open-Meteo every 15 min and backs off after failures (1, 2, 4, 8 min). It keeps the last
  good data, which the page marks "Stale".
- **Navigation:** the left rail has big **Back** / **Next** buttons that step through Weather, Maps, Cameras, Calendar,
  Uptime and Ask (wrapping around), with the current section name between them. Tapping the name opens a full-screen grid to jump to
  any section. **More…** holds 12h/24h, Info and Fullscreen as big buttons. Taps in the first 300 ms after a panel opens
  are ignored, for poor touchscreens.
- **Uptime:** Uptime Kuma monitors first, then the services under `health.targets` in `config.json`, 4 cards per page
  with Back/Next. Anything down sorts to the front of its group. Each card shows the state, response time, uptime %
  over the dashboard's own check history (up to 24 h, kept in memory) and the last 30 checks. It polls every 30 s while
  the tab is open.
- **Fullscreen:** in the More panel. If the browser has no fullscreen API, the button is dimmed.
- **Diagnostics:** the Info button shows user agent, viewport, pixel ratio, sync round-trip, clock error and more.
- **Offline test tag:** while the admin page's offline test switch is on, a red **OFFLINE TEST** tag shows in the rail.
- Tabs can be bookmarked directly, e.g. `http://<server-ip>:8099/#assistant`.

## AI assistant (Ask tab)

Each question starts fresh: only a short system instruction and the question go to the model. There is no history, tool
use or access to the dashboard's data. Answers are plain text in large type, paged instead of scrolled: when an answer
doesn't fit, big Back and Next buttons appear beside it. The Voice button is a large target next to the question box.

- **Provider:** [Vercel AI Gateway](https://vercel.com/docs/ai-gateway), OpenAI-compatible
  `POST https://ai-gateway.vercel.sh/v1/chat/completions`, default model `google/gemini-2.5-flash-lite`
  (`assistant.model`). It's called with the standard library; no SDK.
- **Request:** `max_tokens` 300, no streaming, `reasoning: {"effort": "none"}` (set `assistant.disable_thinking` to `false`
  to omit it). Upstream requests are never retried automatically.
- **Limits** (global, under `assistant`):

  | Setting | Default | Meaning |
  |---|---|---|
  | `min_interval_seconds` | 10 | Minimum gap between questions |
  | `max_per_hour` | 20 | Rolling 60 minutes |
  | `max_per_day` | 100 | Per calendar day in the configured timezone |
  | `max_question_chars` | 1000 | Longer questions are rejected before using quota |
  | `max_output_tokens` | 300 | Cap on answer length |
  | `timeout_seconds`, `max_response_bytes` | 30, 65536 | Bounds on the upstream call |
  | `quota_block_minutes` | 60 | Pause after the provider reports no credits (402) or refused access (403) |

- Only one upstream request runs at a time. Every request that reaches the provider counts, even if it fails. Counters
  persist in `data/assistant-state.json`, so restarts don't reset them.
- `data/assistant-usage.jsonl` records time, model, outcome, status, duration and token counts. **It never contains
  questions, answers or keys.** Set `price_per_million_input_usd` and `price_per_million_output_usd` to get cost estimates.

### Security

- Same-origin only. Requests are refused if they have a foreign `Origin`, a `Sec-Fetch-Site` other than same-origin, the
  wrong `Content-Type`, or a `Host` that isn't an IP address, `localhost` or one of `assistant.allowed_hosts`. The Host
  check blocks DNS rebinding. Add your server's hostname to `allowed_hosts` if you open the dashboard by name. No CORS
  headers are sent.
- Only private-network and loopback clients are served. Provider redirects are never followed, so keys can't be forwarded
  elsewhere. Upstream error text is never shown to the browser or logged.
- Logs contain the request line and status only, never question or answer content.
- **Don't expose this server to the internet.** It's designed for a trusted LAN.

## Voice input (Voice button on the Ask tab)

Speech becomes the question text, transcribed by **Groq** `whisper-large-v3-turbo`.

- **One tap (needs https):** browsers only allow microphone access on secure origins. Run `scripts/make-cert.sh` (or
  `DASHBOARD_IP=<ip> scripts/make-cert.sh`), which creates a private CA and server certificate in `data/tls/`. Then set
  `"https_port": 8443` and restart. Open `https://<server-ip>:8443/`, accept the certificate warning once, and allow the
  microphone. The page records with getUserMedia and Web Audio, so it works on Chrome 50 without MediaRecorder. It stops
  about 1.3 s after you stop talking (30 s maximum), sends nothing if it hears nothing for 7 s, and with `voice.auto_ask`
  on asks the question automatically.
- **Removing the warning (optional):** the CA's public certificate is served at `/dashboard-ca.crt`. On Android, install it
  with Settings → Security → Install from storage. Android 4.4 then requires a screen-lock PIN.
- **Fallback over plain http:** Voice opens the system recorder or file chooser and uploads the recording.
- **Server side:** `POST /api/transcribe` takes up to 8 MiB. It checks the format by content (wav, mp3/aac, m4a/mp4/3gp,
  ogg, webm, flac, amr) and converts it with ffmpeg to 16 kHz mono WAV, trimmed to 30 s. Temporary files are deleted
  immediately.
- **Limits** (under `voice`): 5 s between recordings, 60 per hour, 300 per day, one at a time. Groq bills every request as
  at least 10 s of audio. `data/voice-usage.jsonl` never holds audio, transcripts or keys.
- Transcripts that are empty, only punctuation, or likely non-speech are reported as "didn't catch any speech".

## Admin page

`https://<server-ip>:8443/admin`, from a PC or phone on the LAN. It's served **only on the https listener and only to LAN
clients**; on http and from outside the LAN, `/admin` and `/api/admin/*` return 404. Internals (API, security model, config
sections): [docs/ADMIN-PANEL.md](docs/ADMIN-PANEL.md).

**First time:** on the server run `scripts/set-admin-password.sh` (at least 10 characters, typed with echo off). Only a
scrypt hash is stored, in `data/admin.json` (mode 600). Run it again to change the password.

**Sign-in:** one admin, one password. Sessions live in server memory (cookie `HttpOnly; Secure; SameSite=Strict`) and end
after 8 h idle, 7 days at most, on sign-out, or when the server restarts. Five wrong passwords from one address lock sign-in
for 15 minutes. Every POST must carry `X-Admin: 1`, a same-origin `Origin`/`Host` and a JSON body.

| Page | What it does |
|---|---|
| Overview | Version, uptime, disk, memory, certificate expiry, a line per service |
| API keys | Save, replace, remove and **test** each key. Write-only: shows "set" and the last 4 characters, never the value. Applies without a restart. |
| AI models | Per service (Ask, Voice): cloud model (with the provider's model list), token limit, fallback on/off, no-response deadline, cooldown, local time limit, applied **live**. Local models: pick the chat and speech model files and builds (`cpu`/`vulkan`) found under `local-ai/`, threads; Start/Stop/Restart; memory and health; test buttons. Models larger than the free memory are refused. |
| Offline test | See below. |
| Google Calendar | Connect / Reconnect / Revoke. The final sign-in page (`http://127.0.0.1:<port>/…`) fails to load on another computer, so paste its address into the page. Shows the token's age and, while your Cloud app is in Testing, the 7-day expiry. |
| Health & uptime | Built-in checks every 60 s (listeners, data freshness, keys, Google sign-in age, local models, disk, certificate, offline switch), TCP/HTTP checks of `health.targets` (other services on your network), and Uptime Kuma monitors. About 24 h of history per check, in memory. |
| Settings | Form editor for the safe parts of `config.json`. The old file is copied to `backups/config-<time>.json` first and the result is validated like at start-up. Fields marked ↻ need a restart (button provided); the rest apply immediately. |
| Logs & usage | Ask/Voice requests, local and simulated answers, cost and tokens for today and this month; tails of the server and local-model logs. |
| Maintenance | Refresh now, rebuild maps, clear the assistant's quota pause, back up code and config to `backups/` (no `.env`, no `data/`), restart the dashboard. |

### Offline test switch

Makes the **dashboard process only** behave as if the internet were down, to test the local fallbacks and the stale-data
display. Nothing else on the server loses its network; no firewall rules are touched.

- Choose services: `ask`, `voice` (cloud AI calls) and optionally `weather`, `maps`, `calendar`.
- **Realistic** mode (default): cloud AI requests hang like a black-holed connection, so the real 5 s no-response deadline,
  the straggler guard and the 60 s cooldown-then-probe run as in an outage. **Instant** mode fails at once.
- It always switches itself off (default 15 min, at most 120). Turning it off resets the cooldowns so the cloud is used again
  immediately.
- While it's on, the admin page shows a red banner, the tablet shows a red **OFFLINE TEST** tag, and usage-log entries carry
  `"simulated": true`.
- **Fallback test** sends one question through Ask and a sample recording through Voice and shows which side answered.
- Terminal: `scripts/offline-test.sh on ask,voice 10` · `scripts/offline-test.sh on ask 5 instant` ·
  `scripts/offline-test.sh off` · `scripts/offline-test.sh status`.

### Uptime Kuma (optional)

Show the monitors of an [Uptime Kuma](https://github.com/louislam/uptime-kuma) instance (for example, your web servers) on
the Health page. In Settings → Uptime Kuma (or `config.json` → `kuma`), set `base_url` (e.g. `http://192.168.1.10:3001`)
and either `status_page_slug` (a public status page; no key needed) or `use_metrics: true` with `KUMA_API` saved on the API
keys page (reads `/metrics`: all monitors, no uptime percentages). Then turn on `enabled`. With `push_enabled` and
`KUMA_PUSH_URL` (a Kuma *Push* monitor URL), the dashboard sends a heartbeat after every check, so Kuma can alert you when
the dashboard itself stops.

## Local fallback (offline models for Ask and Voice)

When the cloud APIs don't respond, small models on the server answer instead, and the page labels those answers **Local
fallback (small model, may be wrong)**. Details: [docs/LOCAL-FALLBACK-ARCHITECTURE.md](docs/LOCAL-FALLBACK-ARCHITECTURE.md)
(design, configuration, operations) and [docs/LOCAL-AI-BENCHMARKS.md](docs/LOCAL-AI-BENCHMARKS.md) (measurements on a 2014
dual-core i3 with a GTX 745).

- **When it falls back:** **only** when no HTTP response headers arrive within 5 s, or the connection fails. Any HTTP status
  (401/402/403/429/5xx) keeps the normal error, so billing and quota problems stay visible. After a no-response, that
  service skips the cloud for 60 s, then one request probes it.
- **Chat:** `llama-server` on 127.0.0.1:8091 (default Qwen3.5-2B Q4_K_M, CPU, 2 threads, about 2 GB RAM).
- **Voice:** `whisper-server` on 127.0.0.1:8092 (default whisper base; Vulkan GPU if available).
- **Setup:** build llama.cpp and whisper.cpp into `local-ai/build/llama-<cpu|vulkan>/` and `local-ai/build/whisper-<cpu|vulkan>/`,
  and put model files in `local-ai/models/`, where `local-ai/` is next to this project (or set `LOCAL_AI_DIR`). The
  architecture doc has the exact build commands and pinned versions.
- **Commands:** `scripts/local-ai-start.sh` / `scripts/local-ai-stop.sh`. `scripts/start.sh` runs the start script unless
  `NO_LOCAL_AI=1`, and the dashboard works without it. `python3 tools/eval_local_llm.py` runs a quality check.
- **Settings:** `fallback_*`, `no_response_seconds` and `cloud_cooldown_seconds` in the `assistant` and `voice` sections; the
  model and backend choice in `local_ai`. The admin page edits all of these.

## Weather maps (Maps tab and Weather tab card)

A background thread builds map images; requests only read finished files.

1. Fetch OpenWeatherMap tiles for each layer around the weather location.
2. Composite them over an OpenStreetMap base map (fetched once, cached in `data/maps/base/`), darkened to `base_brightness`.
3. Boost contrast and saturation (handy for washed-out tablet LCDs), draw a location marker, and write a 1024×512 image for
   the Maps tab and a 512×256 crop for the Weather tab.

Both views rotate through the layers with a crossfade and only poll while visible.

| Key (`maps`) | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `layers` | `precipitation_new`, `clouds_new`, `wind_new`, `temp_new` | `pressure_new` also allowed |
| `zoom` | `13` | 3–13. At 11 the large image covers about 65 × 32 km |
| `width`, `height` | `1024`, `512` | Tile count grows with size, not zoom |
| `refresh_minutes` | `30` | Minimum 10 |
| `max_calls_per_minute` | `30` | OpenWeather free plan allows 60 |
| `rotate_seconds` | `10` | Slideshow interval |
| `base_brightness`, `overlay_opacity` | `0.4`, `1.0` | 0–1 |
| `contrast`, `saturation` | `1.35`, `1.4` | 0.5–3 |

About 15 tiles per layer means roughly 60 calls per refresh and 2,900 a day at 30 min intervals. If the key is rejected
(401/403) or rate-limited (429), the cycle stops and backs off; old images stay up, marked stale. New OpenWeather keys can
take up to about 2 hours to activate.

**Attribution** ("Weather maps © OpenWeather", "Map © OpenStreetMap contributors") is shown on screen. Keep it, and follow
the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/).

## Google Calendar (read-only)

Uses scope `calendar.events.readonly` with your own OAuth client.

1. In Google Cloud Console, create a project, enable the **Google Calendar API**, and configure the OAuth consent screen
   (External). Complete the Branding page, then **publish the app to "In production"**. Apps left in Testing get refresh
   tokens that expire after 7 days. An unverified app is fine for personal use.
2. Create an OAuth client of type **Desktop app**. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`.
3. Sign in once: `python3 gcal.py auth` (optionally `--login-hint=you@example.com`). This uses a loopback redirect with PKCE.
   If your browser is on another machine, paste the final redirect URL back into the terminal.
4. Check with `python3 gcal.py status` and `python3 gcal.py test`. Sign out with `python3 gcal.py revoke`.

Steps 3 and 4 can also be done from the admin page's **Google Calendar** page.

The refresh token is stored in `data/gcal-token.json` (mode 600). Settings live under `calendar`: `calendar_ids`
(default `["primary"]`) and `days_ahead`.

## Home tab (Home Assistant buttons)

Optional; bring your own [Home Assistant](https://www.home-assistant.io/). One large tile per button in `config.json` →
`home.buttons`, 8 per page. Create a long-lived token in HA (your profile → Security) and save it as `HA_TOKEN` in `.env`
(or on the admin page: API keys → Home Assistant token, with a Test button). Set `home.ha_url` if HA isn't at
`http://127.0.0.1:8123`.

- **`ha`** buttons call a Home Assistant service, e.g. `{"service": "light.toggle", "entity_id": "light.lamp",
  "state_entity": "light.lamp"}`. With `state_entity` the tile shows ON/OFF; a tap flips it at once and the next poll
  confirms. Put routines in HA (scripts, scenes) and point a button at `script.turn_on`.
- **Two-service buttons:** instead of `service`, use `on_service` + `off_service` with `state_entity`; the server picks one
  by the current state (e.g. arm/disarm an alarm panel; tiles show ARMED / OFF). `"confirm": true` asks first.
- **`media_switch`** (optional, advanced) runs exactly one of Plex or Jellyfin. It calls your own root-owned
  `/usr/local/sbin/media-switch <plex|jellyfin>` through `sudo -n`, so you need to write that script and a sudoers rule
  allowing only it for the dashboard's user. Leave it out of `home.buttons` if you don't want it.

Devices with local control work without the internet; cloud integrations don't.
**Tests:** `python3 -m unittest tests.test_home` (fake systemctl/sudo and a mock HA; nothing is switched).

## Cameras tab

A 2x2 grid (3x2 for 5-6 cameras) of up to 6 cameras from `config.json` → `cameras.list`, with name, age of the picture and
one badge (MOTION, BATTERY LOW, MOTION OFF, OFFLINE, LIVE). Tap a camera for the full-size view.

**Home Assistant cameras** (default `"type": "ha"`, e.g. Blink): the latest snapshot HA has, fetched by the server.
**New snapshot** calls `blink.trigger_camera` (or your `snapshot_service`), rate limited per camera because it wakes
battery cameras. **Motion pop-up:** the tablet checks `/api/cameras` every 15 s on every tab; when a camera with
`"popup": true` has new motion, it jumps to that camera and goes back after `popup_seconds` unless someone touched the screen.

**Local RTSP cameras** (`"type": "rtsp"`): read straight from the LAN, with no Home Assistant or cloud.

```json
{"id": "garage", "label": "Garage", "type": "rtsp", "rtsp_url": "rtsp://192.168.1.20:554/live/ch00_1", "live_fps": 10}
```

- The tile shows a still that refreshes every `still_seconds` (10). Tap it for the **live view**; **Restart live**
  reconnects, and after `live_max_seconds` (600) it pauses with **Resume live**. The page drops the stream when you leave
  the camera, change tabs or the screen turns off.
- The server runs one ffmpeg per camera, only while someone watches, turning H.264 into MJPEG. Chrome 50 shows MJPEG in a
  plain `<img>` but can't be relied on to play a live H.264 stream. All viewers share that ffmpeg, which stops
  `live_idle_seconds` (15) after the last one. A 640x360 sub-stream at 10 fps costs about 10% of one CPU core.
- V380 cameras: turn on **ONVIF** in the V380 Pro app; RTSP then opens on port 554 (`/live/ch00_0` main, `/live/ch00_1`
  sub-stream). Use the sub-stream.
- `rtsp_url` may not contain a user:password or a query. Settings: `cameras.ffmpeg_path`, `still_seconds`,
  `live_idle_seconds`, `live_max_seconds`, and per camera `live_fps` (1-15).

**Tests:** `python3 -m unittest tests.test_cameras` (mock HA; a fake ffmpeg for RTSP cameras).

## Weather data

From the [Open-Meteo](https://open-meteo.com/) Forecast API. It needs no key, and the free tier is **for non-commercial use
only** (under 10,000 calls a day; this dashboard makes about 96). The data is
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), and the page shows the required "Weather data by Open-Meteo.com"
credit.

## Known limitations

- Cameras: HA cameras such as Blink give snapshots only, no live view. RTSP cameras without a password are visible to anyone on the LAN.
- English-only day and month names.
- Logs are not rotated.
- Answers are not streamed and can take a few seconds.
- On an 800 × 400 screen the on-screen keyboard covers much of the Ask tab.
- Provider free tiers and rate limits change. Check current terms before relying on them.

## Versions

See [VERSION.md](VERSION.md) for the version history.

## License

[MIT](LICENSE) © 2026 Daniel Lopez Quezada
