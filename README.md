# Tablet Dashboard

Turn an old Android tablet into an always-on home dashboard: clock, weather, animated weather maps, a Google Calendar agenda
and a voice-enabled AI assistant.

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
- **Cameras:** placeholder tab for now.

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
| HTTPS | OpenSSL private CA, for microphone access on the LAN |
| Tests | `unittest` with mock servers for every upstream service (107 tests) |

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

## Layout

| Path | Purpose |
|---|---|
| `server.py` | HTTP(S) server: static files, `/api/*`, `/media/*`, background weather fetcher |
| `assistant.py` | AI assistant: key loading, usage limits, Vercel AI Gateway call |
| `voice.py` | Voice input: upload checks, ffmpeg conversion, Groq Whisper transcription |
| `maps.py` | Weather maps: OpenWeatherMap and OpenStreetMap tiles, rate limiting, Pillow compositing |
| `gcal.py` | Google Calendar: background fetcher, plus the `auth`, `status`, `test` and `revoke` commands |
| `config.example.json` | Example settings; copy to `config.json` (gitignored) |
| `.env` | **Private** API keys (mode 600; never served, gitignored) |
| `web/` | The only directory served as static files |
| `media/` | Optional; the only directory served under `/media/` (mp4, m4v, webm) |
| `data/`, `logs/`, `run/` | Runtime state, log and PID file (created automatically, gitignored) |
| `scripts/` | `start.sh`, `stop.sh`, `make-cert.sh`, `set-assistant-key.sh` |
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

The server refuses to use a `.env` that is group- or world-readable, a symlink, or owned by another user. Environment
variables with the same names take priority over the file.

## HTTP routes

| Route | Behaviour |
|---|---|
| `/` and static files | Only from `web/`; html, css, js, svg, png, ico, txt and crt. No directory listings. Dotfiles, `..`, encoded traversal, backslashes, NUL bytes and symlinks leaving `web/` return 404. |
| `/api/time` | Server time, timezone and UTC offset; never cached |
| `/api/weather` | Cached weather plus status (`unconfigured`, `loading`, `error`, `ok`) and a `stale` flag. Never contacts Open-Meteo during the request. |
| `/api/calendar` | Cached agenda grouped by day, plus status. LAN clients only. |
| `/api/config` | Client settings (version, timezone, clock format, weather unit, https port) |
| `POST /api/assistant`, `/api/assistant/status` | Ask one question; setup state and remaining quota |
| `POST /api/transcribe`, `/api/voice/status` | Speech to text; setup state and remaining quota |
| `/api/maps`, `/maps/<layer>.png`, `/maps/<layer>_small.png` | Map status and rendered images |
| `/media/<file>` | Only from `media/`, with byte-range support |

Only GET and HEAD are accepted, apart from the two POST routes. Idle connections time out after 20 s.

## Client behaviour

- **Clock:** driven by server time, so the tablet's own clock and timezone don't need to be right. Each sync corrects for
  half the round-trip time; the display then advances locally. Syncs run every 60 s, every 15 s after a failure, and
  whenever the page becomes visible or the local clock jumps. The footer shows sync status (green, amber or red).
- **Weather:** the server fetches Open-Meteo every 15 min and backs off after failures (1, 2, 4, 8 min). It keeps the last
  good data, which the page marks "Stale".
- **Fullscreen** button in the rail. If the browser has no fullscreen API, the button is dimmed.
- **Diagnostics:** the Info button shows user agent, viewport, pixel ratio, sync round-trip, clock error and more.
- Tabs can be bookmarked directly, e.g. `http://<server-ip>:8099/#assistant`.

## AI assistant (Ask tab)

Each question starts fresh: only a short system instruction and the question go to the model. There is no history, tool
use or access to the dashboard's data. Answers are plain text.

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

The refresh token is stored in `data/gcal-token.json` (mode 600). Settings live under `calendar`: `calendar_ids`
(default `["primary"]`) and `days_ahead`.

## Weather data

From the [Open-Meteo](https://open-meteo.com/) Forecast API. It needs no key, and the free tier is **for non-commercial use
only** (under 10,000 calls a day; this dashboard makes about 96). The data is
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), and the page shows the required "Weather data by Open-Meteo.com"
credit.

## Known limitations

- Cameras tab is a placeholder.
- English-only day and month names.
- Logs are not rotated.
- Answers are not streamed and can take a few seconds.
- On an 800 × 400 screen the on-screen keyboard covers much of the Ask tab.
- Provider free tiers and rate limits change. Check current terms before relying on them.

## Versions

See [VERSION.md](VERSION.md) for the version history.

## License

[MIT](LICENSE) © 2026 Daniel Lopez Quezada
