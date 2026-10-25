# Version history

Every meaningful change gets a new version here, with what improved. Versions follow
[Semantic Versioning](https://semver.org/): **minor** (0.x.0) for new features or behaviour changes, **patch** (0.0.x) for
fixes and small improvements. While the project is below 1.0, settings and routes may still change between minor versions.

The running version appears in the dashboard's Info panel and in `/api/config`. It comes from `VERSION` in `server.py`.

Versions 0.3.0 to 0.5.0 were numbered in hindsight from dated development snapshots. The code reported 0.2.0 throughout
that period.

---

Versions 0.21.3 to 0.25.1 were developed on the author's own server and published together.

## 0.25.1 (2026-10-25): Camera live view works with ffmpeg 6

**Fixed**
- RTSP live view showed no picture ("Option not found") with ffmpeg 5 or newer, which removed the RTSP `-stimeout`
  option. `cameras.py` now asks ffmpeg for its version once and uses `-timeout` from 5.0 on and `-stimeout` before
  that, so either build works.

**Tests**
- The fake ffmpeg in `tests/test_cameras.py` answers `-version`, plus a test of the flag choice.

## 0.25.0 (2026-10-25): Google voice for spoken answers; shorter home replies

**Added**
- `googletts.py`: optional Google Cloud Text-to-Speech, voice **es-US-Chirp3-HD-Achernar** (and
  en-US-Chirp3-HD-Achernar for English), picked by ear against Piper and other Chirp 3 HD voices. It signs in with a
  service account key (`data/google-tts-sa.json`, mode 600). The JWT is signed with python3-cryptography (Ubuntu
  package), and the access token is kept in memory only. Each call takes about 0.5–0.8 s, against 1.3 s for Piper.
- `tts.engine`: `"google"` speaks with Google first and falls back to Piper when Google fails, times out
  (`google_timeout_seconds` 5), is past the monthly limit, or the offline test blocks Ask. `"piper"` (the default)
  works as before.
- Spending guard: characters sent are counted per calendar month (UTC) in `data/google-tts-usage.json`. Calls stop
  at `tts.google_monthly_chars` (500,000, half of Google's 1M free Chirp 3 HD characters a month) and Piper takes
  over. `/api/assistant/status` shows `speech.engine` and `speech.google.{chars_this_month, monthly_limit}`.
- The phrase bank records in the active engine's voice. Bank builds never fall back to Piper, so a Piper clip can't
  end up under a Google name. A build stops after 3 failures in a row and retries in 10 minutes.
- Admin page (Settings, Spoken answers): engine, Google voices, Google speeds, monthly character limit.

**Changed**
- Home replies are shorter and easier to say: "Abanico apagado.", "Abanico prendido.", "Movie night presionado."
  (was "Listo, apagué Abanico."). English: "Abanico is off.", "Movie night pressed." (the button label is used as is).

**Privacy**
- With the Google engine, the text of spoken answers (home replies and AI answers) is sent to Google. Saved phrases
  are sent once, when they are recorded.

**Tests**
- New `tests/test_googletts.py` (fake Google, throwaway RSA key): token reuse and refresh, usage count and monthly
  limit, errors, Google-first with Piper fallback, bank clips only from Google, Piper engine untouched.

## 0.24.0 (2026-10-24): Saved phrase bank for spoken answers

**Added**
- Spoken replies to home commands play at once instead of after ~1 s of Piper. `homeai.House.speech_phrases()` lists
  every fixed reply the house can give for the current Home buttons (on/off/press, already on/off, states, armed and
  disarmed, confirmations, media switch, Home Assistant down, expired, offline look) in Spanish and English. `tts.py`
  renders them, plus `tts.phrases_extra` ("Listo.", "Ok.", "Apagado.", "Prendido.", …), in a background thread into
  `data/tts-phrases/` (mode 700, files 600), one clip at a time between live answers.
- A reply made of several saved sentences is joined from the saved clips; the longest saved run is matched first, so a
  confirmation line stays one clip.
- File names are a hash of engine, voice, speed, sentence pause and text: changing a speed on the admin page wakes the
  thread, which re-renders and removes clips no longer wanted. It checks again every 6 hours.
- Config `tts.phrase_bank` (on; also on the admin page) and `tts.phrases_extra` (up to 200 phrases).
  `/api/assistant/status` → `speech.phrases` shows `{ready, total}`.

**Privacy**
- Only these template phrases are written to disk. AI answers are never saved; if any sentence of an answer is not in
  the bank, the whole answer is rendered live.

**Tests**
- Phrase bank tests in `tests/test_homeai.py`, plus a check that every reply the house produces is covered by
  `speech_phrases()`.

## 0.23.0 (2026-10-23): Weather history in PostgreSQL

**Added**
- `pgstore.py`: optional history in a local PostgreSQL (database `dashboard`, peer login over the Unix socket as the
  Linux user running the dashboard, so no password). It talks to PostgreSQL through the `psql` client, so the dashboard
  still needs no Python packages beyond the standard library and Pillow. Every value is a psql variable quoted by psql,
  never SQL text. Tables are created on startup; if the database is down, readings are skipped and logged once, and it
  retries every 5 minutes. Config section `database` (`enabled`, `name`, `psql_path`, `timeout_seconds`), off by
  default.
- Every weather fetch (every 15 minutes) is saved to `weather_readings`: temperature, humidity, condition, rain in the
  last 15 minutes, wind and gusts, today's min/max, rain total and rain chance, plus the full reading as JSON.
- Open-Meteo now also returns rain, wind and gusts (current) and rain total and rain chance (daily). Same single call.
- Ask gets tomorrow's rain chance and a last-24-hours summary from its own records (low/high, approximate rain, when it
  last rained), so "¿llovió anoche?" is answered from data.

**Tests**
- New `tests/test_pgstore.py` (fake psql; checks values are variables, nulls, backoff, summary, new weather fields).

## 0.22.4 (2026-10-23): Offers are confirmed, not carried out

**Fixed**
- A model reply that asked "Would you like me to turn on the fan?" while also listing actions had those actions carried
  out at once, before the person answered. The prompt now says to leave actions empty when suggesting or asking, and as
  a safety net, a reply containing a question mark has its actions held for the on-screen Yes/No (the same one-time
  token as the alarm) instead of being run.

## 0.22.3 (2026-10-23): Camera looks the model asks for, more look phrases, weather in Ask

**Fixed**
- When the model answered a question with "look at camera X", the follow-up call with the picture was refused by the
  10-second gap between questions. The follow-up now only counts toward the hourly and daily limits.

**Improved**
- More phrases go straight to a camera without the model ("¿puedes ver si…", "revisa", "checa", "can you check…",
  "did I leave…").
- The assistant gets the dashboard's own weather (current, today, tomorrow from the Open-Meteo cache, no extra calls),
  so "¿está lloviendo?" is answered from real data.

## 0.22.2 (2026-10-23): Better Spanish voice, faster speech

**Improved**
- Spoken answers in Spanish use Piper's high-quality Mexican voice "claude", 15% faster (`tts.length_scale_es` 0.85).
  English keeps "lessac" at normal speed. New `sentence_silence` setting (0.2 s). Speeds and pause are on the admin
  page (Settings, Spoken answers). `scripts/install-piper.sh` installs the voice.

## 0.22.1 (2026-10-23): Voice on the modern view goes to https

**Fixed**
- On the modern view over plain http, Voice opened the phone's recorder app, because browsers only allow the
  microphone on https. It now goes to the dashboard's https address (`#assistant`), where one tap records. The recorder
  app is only used when no https address is configured.

## 0.22.0 (2026-10-23): Ask the house: home commands, camera questions and spoken answers

**Added**
- **Home commands in Ask** (typed or by voice): `homeai.py` understands simple commands and state questions in Spanish
  and English ("apaga el abanico", "turn on the bedroom light", "¿qué está prendido?") without the AI and without using
  the Ask quota. Other requests go to the model with the list of Home tiles and their states, and come back as JSON
  actions. Only Home tiles can be pressed, and `Home.set_state` checks the current state instead of toggling blindly.
- **Server-side confirmation:** alarm, media switch and any `"confirm": true` tile are held back until the person taps
  Yes (`POST /api/assistant/confirm`, one-time token, 60 s). Before, only the pages asked.
- **Short-term memory:** the last 6 messages of a conversation for 2 minutes idle (in memory), so follow-ups work.
- **Camera questions:** "what do you see outside?" sends one picture to the cloud model: a live RTSP frame, or the Home
  Assistant snapshot (refreshed first when older than 60 s). Optional background snapshots for cameras with
  `ai_refresh_seconds`. Offline, camera questions say the internet is needed.
- **Spoken answers:** Piper (local, ~0.7 s per sentence) in a Mexican Spanish or US English voice, picked from the
  answer. `GET /api/tts/<id>.wav`, played through Web Audio on the tablet and the modern view; "Sound on/off" button.
  `scripts/install-piper.sh` installs it into `../local-ai/` with checksums.
- `aliases` for Home buttons and cameras; admin settings for house control, memory, spoken answers and the background
  camera refresh.

**Changed**
- `POST /api/assistant` also accepts `conversation` and `speak`; replies may carry `source: "house"|"offline"`,
  `actions_done`, `confirm` and `speech_id`. `/api/assistant/status` reports `home_control` and `speech`.
- With house control off the assistant behaves exactly as before.

**Tests**
- New `tests/test_homeai.py` (matcher, confirmation, JSON replies, memory, camera paths, background refresh, Piper with
  a fake binary); route snapshot extended with the new routes.

## 0.21.3 (2026-10-23): Server code split into modules

**Changed** (internal only; no change to routes, responses, ports or the pages)
- `server.py` went from 1,121 to about 250 lines. It now only builds the services and runs the listeners:
  - `config.py`: config defaults, loading and validation;
  - `weather.py`: the Open-Meteo fetcher;
  - `httpcore.py`: content types, CSP, safe paths, Range requests and the `send_*` helpers;
  - `routes/`: one route table (exact paths, ordered prefixes, a LAN-only flag per route) and small handler modules.
- `admin.py` (1,329 lines) became the `admin/` package: `AdminService` plus one mixin per admin page and a `core.py`
  for shared constants and password hashing. The CLI is now `python3 -m admin set-password`.
- The command-line tools load the config through `config.py` instead of importing the whole server. FastAPI was
  considered and rejected: it would add dependencies and replace tested security code for no gain at this load.

**Tests**
- New `tests/test_routes_table.py`: requests over http and https compared against a golden file recorded from the code
  before the split, in an empty base directory so local keys don't affect it; and a check that every LAN-only route
  refuses outside clients. 327 tests in total at 0.25.1, run by GitHub Actions on every push.

---

Versions 0.15.0 to 0.17.0 were developed on the author's own server and published together.

Versions 0.18.0 to 0.21.2 were developed on the author's own server and published together.

## 0.21.2 (2026-10-22): Reference docs refreshed

**Changed** (docs only)
- `docs/SYSTEM-OVERVIEW.md` covers 0.18–0.21:
  - autostart, the camera controls, find-by-MAC, Blink live through a local proxy, and the Modern view button;
  - a new Cameras constraints table;
  - an updated status section.
- `docs/ADMIN-PANEL.md` and `docs/LOCAL-FALLBACK-ARCHITECTURE.md`: status, systemd behaviour and test counts.

## 0.21.1 (2026-10-22): Switch from the classic to the modern view

**Improved**
- The classic view's **Go to** menu (tap the section name in the rail) has a **Modern view** button. A PC that got the
  classic view (for example after choosing "Classic view", which is remembered in a cookie) can switch back in one tap.
  `/?ui=auto` returns to automatic detection.

## 0.21.0 (2026-10-21): Blink live view (on demand, through a local proxy)

**Added**
- A Blink camera can have an on-demand **Live view** on the tablet and on phones/PCs. It's read from a local
  [Blink live-view proxy](https://github.com/Teethree89/ha-blink-live-view-proxy) that you run yourself (e.g. Docker,
  pinned, published on loopback only), which streams MPEG-TS over HTTP. The existing `LiveFeed` turns that into MJPEG,
  scaled to 720p, with one FFmpeg shared by every viewer.
- The page starts it only when asked: each start is a Blink cloud session, and the proxy allows a new one about 30 s
  after the last. Leaving the camera, changing tabs or hiding the page stops it and forgets it.
- Config: `cameras.blink_proxy_url` (empty = off), `cameras.blink_proxy_token_name` (default `BLINK_PROXY_TOKEN`, read
  from `.env`), and per Home Assistant camera `blink_live`, the proxy's camera name (accents allowed, percent-encoded).
- The token goes to the proxy as an `Authorization` header, never in the URL, because FFmpeg's error messages quote the
  URL and appear on the page. Proxy errors are reworded: cooldown, unknown camera, refused token, proxy not running.
- `/api/cameras` items have `can_live` and `live_error`. 269 tests in total.

**Measured (one Blink camera):** first picture about 7.5 s after the press. Blink sends 1920x1080; at 720p the FFmpeg
uses about 27% of one core of an i3-4150 and sends about 300 KB/s.

## 0.20.0 (2026-10-21): Find a camera again after its address changes

**Added**
- Optional `"mac"` per RTSP camera. When its stream ends without a single frame, or an ONVIF control gets no answer, the
  server sends an ONVIF WS-Discovery probe (multicast 239.255.255.250:3702), at most every 2 minutes per camera. If the
  device with that MAC answers from a new address, the camera's `rtsp_url`, `rtsp_url_hd` and `onvif_url` move there, in
  memory only, with a log warning to update `config.json`.
- The MAC comes from the reply's endpoint address when it carries one, or else from ONVIF `GetNetworkInterfaces`.
  `onvif.py` gains `discover()`, `parse_probe_match()`, `normalize_mac()`, `Camera.mac()` and `Camera.moved_to()`.
- Tests for discovery parsing and relocation. 265 in total.

**Verified** on a real V380: with its URLs pointed at an unused address, the dashboard found it again about 5 s after the
first failed picture.

## 0.19.0 (2026-10-21): Camera controls (pan/tilt, HD/SD, day/night)

**Added**
- **Pan/tilt arrows** over a live camera's picture, on the tablet and on phones/PCs (plus arrow keys on a PC). Each tap is a
  half-second ONVIF `ContinuousMove` followed by `Stop`.
- **HD / SD** for the live view: an optional `rtsp_url_hd` (the camera's main stream) gets its own on-demand FFmpeg. The
  tablet starts in SD; phones and PCs start in HD and remember the choice.
- **Day / Night / Auto** (the ONVIF IR-cut filter). On the tablet one button steps through them; the modern view has three
  chips. The current mode is read from the camera when its page opens.
- `onvif.py`: a minimal standard-library ONVIF client (`GetCapabilities`, `GetProfiles`, PTZ, imaging). Service addresses
  from the camera are reduced to their paths and always called on the configured host.
- Per-camera config: `rtsp_url_hd`, `onvif_url`, and optional `ptz` and `day_night` (both on by default with `onvif_url`).
  Routes: `GET /api/cameras/<id>/controls`, `POST /api/cameras/control`, `live.mjpg?q=hd`.
- Tests: `tests/test_onvif.py` (mock camera), control and HD tests. 257 in total.

**Measured on a V380:** HD 1280x720 at 10 fps costs 14-19% of one core and about 330 KB/s, against about 10% and 110 KB/s
for SD. A pan step answers in about 0.6 s.

## 0.18.0 (2026-10-21): Autostart and crash recovery

**Added**
- **systemd user services** in `deploy/systemd/`: `tablet-dashboard.service` starts the dashboard at boot and restarts it
  5 s after a crash (`Restart=always`); `tablet-local-ai.service` starts the optional local models. With lingering on,
  both start without a login.
- `scripts/install-autostart.sh` installs and enables the units and hands a running dashboard over to systemd;
  `--remove` goes back to manual mode, and `--no-local-ai` enables the dashboard only.

**Changed**
- `scripts/start.sh` and `scripts/stop.sh` use `systemctl --user` when autostart is installed, so the usual commands
  work unchanged.

## 0.17.0 (2026-10-21): Modern view phase 3 (Ask with voice), so every section is now in the modern view

**Added** (modern view for phones and PCs; the tablet's classic view is unchanged)
- **Ask:** standalone questions (nothing is kept between them), with the same limits, wait countdown, quota line and
  "local fallback" warning as the tablet. Enter sends; Shift+Enter starts a new line.
- **One-tap voice with `MediaRecorder`:** tap **Voice**, speak, and recording stops by itself 1.3 s after you finish.
  It gives up after 7 s without speech and stops at `max_audio_seconds`. The thresholds match the tablet. The
  recording (WebM/Opus on Chrome and Firefox, MP4 on Safari) goes to the existing `/api/transcribe`, which already
  accepted those formats, and the text is asked automatically (`voice.auto_ask`). Leaving the page always turns the
  microphone off.
- **On plain http:** browsers allow the microphone only on secure pages, so **Voice** opens the phone's recorder app
  and the page links to the https address for one-tap voice.
- An Ask card on the Overview (full width on PC). On phones, **Ask** replaces Weather in the tab bar, since the
  Overview starts with the weather; Weather moves to More.

**Removed**
- The "open in the classic view" placeholders. Every section now has a modern version.

---

## 0.16.0 (2026-10-20): Modern view phase 2 (Home, Cameras, Calendar)

**Added** (modern view for phones and PCs; the tablet's classic view is unchanged)
- **Home:** every Home Assistant button and the Plex/Jellyfin switch as large tiles. They use the same wording and
  confirmations as the tablet (arming or disarming Blink, switching media servers). The state updates immediately and
  is confirmed on the next poll. Polling runs every 2 s while the media server is switching.
- **Cameras:** a grid of stills that reload only when a camera has a new picture. Tap one for its own page
  (`#cameras/<id>`). Snapshot cameras get **New snapshot**. The V380 plays live MJPEG with **Restart live**. Live view
  pauses after `live_max_seconds` or when the page is hidden, and the stream is dropped when you leave. A picture that
  fails to load shows "No picture".
- **Motion alert:** a red banner on every screen with **View**, shown once per alert, that hides after `popup_seconds`.
  Unlike the tablet, it doesn't jump away from what you're doing.
- **Calendar:** the coming days with all-day, multi-day, current (highlighted) and past (dimmed) events. Times follow
  the 12/24 h setting, and the view refreshes every minute.
- **Overview:** Home tiles, camera thumbnails and upcoming events, alongside weather, maps and uptime.
- Shared pieces: a confirm dialog, toasts, `section/param` routes, a polling interval that depends on the data, and
  optimistic updates in the feed store.

**Changed**
- Only Ask still opens in the classic view (due in 0.17.0).

---

## 0.15.0 (2026-10-20): Modern view for phones and PCs (phase 1)

**Added**
- **Browser detection on `/`** (`uidetect.py`). Old browsers get the classic tablet page from `web/`, unchanged: Chrome
  below 80 (the tablet runs Chrome 50), Firefox below 78, Safari/iOS below 14, and IE. Everything else gets the new
  **modern view** from `web-modern/`. The URL stays the same and the response carries `Vary: User-Agent, Cookie`.
- **Overrides:** `/?ui=modern` or `/?ui=classic` picks a view and remembers it for a year in a `ui` cookie;
  `/?ui=auto` forgets the choice; `&once=1` picks it for one visit only. The classic Info panel links to the modern
  view and back to automatic.
- **Modern view** (`web-modern/`, ES modules, no build step, no framework, no CDN, same CSP):
  - PC: sidebar plus an **Overview** grid with clock and current weather, the rotating map, uptime, and links.
  - Phone: bottom tab bar (Overview, Weather, Home, Cameras, More).
  - Weather, Maps and Uptime sections. Light and dark themes follow the device. Polling pauses while the page is hidden.
  - Home, Cameras, Calendar and Ask open in the classic view for now (one visit only) until phases 2 and 3.
- **Safety net:** browsers without ES modules run `fallback.js`, and a modern app that fails to load (syntax too new),
  is caught by `boot.js`. Both send the browser to the classic view.
- Settings: `modern_dir` (default `web-modern`) and `ui.modern_enabled` (default `true`; `false` sends everyone to
  the classic view). `GET /m/<file>` serves the modern files with the same restrictions as `web/`.
- Tests: `tests/test_uidetect.py` and new routing tests in `tests/test_server.py`.

**Changed**
- `/index.html` is always the classic page. The secret-hygiene test also scans `web-modern/` and checks that its
  JavaScript never uses `innerHTML`.

---

Versions 0.12.0 to 0.14.0 were developed on the author's own server and published together.

## 0.14.0 (2026-10-20): live view of local RTSP cameras

**Added**
- **RTSP cameras** (`"type": "rtsp"` in `cameras.list`), read straight from the LAN with no Home Assistant or cloud,
  e.g. a V380 with ONVIF turned on in its app.
- **Live view:** tap the camera for live video as MJPEG in an `<img>`, which works on old browsers like Chrome 50.
  **Restart live** reconnects; the view pauses after `live_max_seconds` (600) with **Resume live**, and the stream is
  dropped when you leave the camera, change tabs or the page is hidden.
- `LiveFeed` in `cameras.py`: one ffmpeg per camera (H.264 to MJPEG at `live_fps`), started on demand, shared by every
  viewer and the grid still, stopped 15 s after the last viewer, with a 5 s back-off after a failure.
- `GET /api/cameras/<id>/live.mjpg` (LAN only, at most 4 viewers per camera).
- Settings `cameras.ffmpeg_path`, `still_seconds`, `live_idle_seconds`, `live_max_seconds`. `rtsp_url` must be rtsp://
  with no credentials or query.

**Changed**
- The Cameras grid holds up to 6 cameras: 2x2 for up to 4, 3x2 for 5-6.
- Without Home Assistant the tab still shows RTSP cameras instead of a full-page "not connected".

## 0.13.0 (2026-10-20): Home Assistant cameras, arm/disarm and motion pop-ups

**Added**
- **Cameras tab** (was a placeholder): the latest snapshot of each Home Assistant camera (e.g. Blink) with a badge
  (MOTION, BATTERY LOW, MOTION OFF, OFFLINE). Tap one for the full-size picture and **New snapshot**.
- **Motion pop-up:** when a camera with `"popup": true` reports new motion, the tablet jumps to it from any tab and
  goes back after `popup_seconds` unless someone touched the screen.
- `cameras.py`: camera status from HA's states, an image proxy through `/api/camera_proxy` (the HA token never reaches
  the tablet), and a rate-limited New snapshot.
- `GET /api/cameras`, `GET /api/cameras/<id>.jpg`, `POST /api/cameras/snapshot` (LAN only).
- Home buttons can use `on_service` + `off_service` chosen by the current state (e.g. an alarm panel).
- Icons `shield` and `motion`.

## 0.12.0 (2026-10-20): Home tab: Home Assistant buttons

**Added**
- **Home** tab: one large tile per button, 8 per page. Lights and plugs show ON/OFF from Home Assistant and toggle with
  one tap.
- `home.py`: Home Assistant REST client (one `GET /api/states` per poll, `POST /api/services/...`), 5 s timeouts, and an
  optional, advanced Plex/Jellyfin switch through your own root-owned `media-switch` script and a sudoers rule.
- `GET /api/home` and `POST /api/home/action` (LAN only, same-origin, JSON ≤ 1 KB, 2 s debounce per button,
  120 presses per hour).
- Admin page: **Home Assistant long-lived token** (`HA_TOKEN`) under API keys, with a Test button.
- `config.example.json`: generic `home` and `cameras` examples.
- Icons `home`, `media`, `fan`, `plug`, `light`.

**Changed**
- Health checks skip a media server that the switch turned off on purpose.

Versions 0.7.0 to 0.10.0 were developed on the author's own server and published together in this release.

## 0.11.0 (2026-10-20): Uptime section on the tablet

**Added**
- **Uptime** tab (between Calendar and Ask; 6 sections now). It shows the Uptime Kuma monitors first, then the
  `health.targets` services from `config.json`, 4 large cards per page (2x2) with a Back/Next pager. Each card has the
  name, big UP/DOWN/WAIT, response time, uptime % and a 30-check history strip. Anything down sorts first in its group,
  a failed card turns red, and the header shows "N up · N down · checked HH:MM".
- `GET /api/uptime` (LAN only, like `/api/calendar`): `HealthChecker.public_snapshot()` in `health.py`. It sends only
  names, states, ping, uptime % and recent history: no hostnames, URLs or error text, because the route has no login.
- Results now carry `ping_ms` (Kuma from `/metrics`, services from the TCP/HTTP timing).
- Icon `web/icons/uptime.svg`; the jump grid is now 3+3 tiles.

**Notes:** Kuma's `/metrics` has no 24 h uptime, so uptime % comes from the dashboard's own in-memory history (checks
every `interval_seconds`, up to 24 h). It restarts with the server, and the card shows the span ("1 min", "3 h") until
24 h have built up. The tab polls only while it's visible.

## 0.10.0 (2026-10-20): Back/Next navigation for the touchscreen

**Changed**
- The left rail's five small section buttons (56 px tall with 6 px gaps) often caught the wrong tap on the tablet's
  poor touchscreen. The rail is now 150 px wide (was 112) and has two large **Back** / **Next** buttons (about 133x97
  at 800x400) that wrap around, with the current section (icon, name, "n / 5") between them, separated by 12 px dead zones.
- Tapping the current section name opens a full-screen **Go to** grid with five large tiles for a direct jump.
- 24h / Info / Fullscreen moved behind one **More…** button that opens a panel with three large buttons. The
  element ids are unchanged, so the existing handlers work as before.
- Taps on a panel in the first 300 ms after it opens are ignored, so a bounce can't pick a tile by accident.
- Small-viewport CSS: a 110 px rail with the same layout.

**Verified:** headless Chromium over CDP at 800x400 and 533x267. Back wraps from Weather to Ask, Next steps, the grid
jump works, a tap within 300 ms is ignored, 24h toggles, Info opens Diagnostics, the rail doesn't overflow, and there are
no JS errors. 183 tests OK. Not yet tried on the tablet.

## 0.9.2 (2026-10-19): Uptime Kuma key name

**Changed**
- The Kuma API key is named `KUMA_API` in `.env`. The older `KUMA_API_KEY` is still accepted as an alias, in both
  `health.py` and the admin page's API keys row.
- Tested against a real Kuma instance through `/metrics` (http and keyword monitors).

## 0.9.1 (2026-10-19): Reference docs brought up to date

Documentation only; no code change apart from the version number.

**Changed**
- `docs/SYSTEM-OVERVIEW.md`:
  - adds the admin page to the features table and the architecture diagram, and covers its state files and access rules
  - adds new constraint rows for testing fallbacks safely and for health monitoring
  - updates the status to 0.9.0 and its open items
- `docs/LOCAL-FALLBACK-ARCHITECTURE.md`:
  - status is now live
  - documents the `local_ai` config section and how the start script reads it
  - adds a section on the offline test switch (Realistic vs Instant modes, auto-expiry, gate reset, labelling)
  - adds the admin page and `offline-test.sh` to Operations, and marks "enable in production" done
- `docs/LOCAL-FALLBACK-PLAN.md`: the status header says it is live.
- `README.md`: links the system overview, version history and admin URL; the local-fallback summary mentions the
  `local_ai` section and the offline test switch. Links the new admin reference.
- `docs/LOCAL-AI-BENCHMARKS.md`: adds the live offline-switch round trip (Ask 6.67 s, Voice 9.55 s; 5.00 s simulated
  wait each).
- `docs/LOCAL-FALLBACK-ARCHITECTURE.md` pitfalls: the intermittent `BrokenPipeError` in
  `test_voice.test_rejected_input_uses_no_quota` is a known flaky test that predates 0.9.0.

**Added**
- `docs/ADMIN-PANEL.md`: reference for the admin internals (modules, security model, `/api/admin/*` routes and jobs,
  the `admin`/`health`/`kuma` config sections, config saving and restart, health checks, tests, open work).

**Verified:** 182 tests pass at 0.9.0; the admin password is set (`data/admin.json`, mode 600), and `/admin` returns 404
on http 8099.


## 0.9.0 (2026-10-19): Admin page, offline test switch, health checks

A password-protected admin page (`https://<server>:8443/admin`, LAN only) replaces most shell-only maintenance.

**Added**
- `admin.py` + `web/admin.{html,js,css}`: single-admin sign-in (scrypt hash in `data/admin.json`, set with
  `scripts/set-admin-password.sh`), in-memory sessions with `HttpOnly; Secure; SameSite=Strict` cookies, 5-failure lockout,
  `X-Admin` + same-origin check on every POST. Served only on the https listener and only to LAN clients.
- **API keys page**: save, replace, remove and test the Gateway, Groq, OpenWeatherMap, Google OAuth and Uptime Kuma
  secrets. Write-only (shows the last 4 characters); applies without a restart; also accepts the alias `GROQ`.
- **AI models page**: cloud model per service (with the provider's model list), token limit, fallback deadline/cooldown/
  time limit, applied live; local chat/speech model and build picker from `local-ai/` (memory guard), start/stop/restart,
  health and memory per server; direct cloud and local test buttons. `localai.py`; new `local_ai` config section, read by
  `scripts/local-ai-start.sh` (environment variables still win).
- **Offline test switch** (`netsim.py`, `scripts/offline-test.sh`): simulates an internet outage inside the dashboard only,
  for Ask, Voice, Weather, Maps and/or Calendar. Realistic mode exercises the real no-response deadline, straggler guard and
  cooldown; instant mode fails at once. Always auto-expires (≤ 120 min), resets the cooldowns when turned off, marks usage
  entries `simulated`, and shows a red OFFLINE TEST tag on the tablet. A one-click fallback test reports which side answered.
- **Google Calendar page**: connect / reconnect / revoke in the browser (paste-the-redirect flow), token age and the 7-day
  Testing-mode expiry.
- **Health & uptime** (`health.py`): checks every 60 s of the listeners, data freshness, keys, Google sign-in age, local
  models, disk, certificate and the offline switch, plus TCP/HTTP targets (other services on your network) and
  optional Uptime Kuma monitors (status page or `/metrics`) with a push heartbeat. ~24 h history per check.
- **Settings** editor for the safe parts of `config.json` (validated like at start-up, previous file backed up), **Logs &
  usage** (cost, tokens, local and simulated answers today/this month), **Maintenance** (refresh, rebuild maps, clear the
  quota pause, backup, restart).
- `envfile.py` (shared private-file helpers); `/api/config` reports `offline_test`.
- 52 new tests (`test_admin.py`, `test_netsim.py`, `test_health.py`).

**Changed**
- Weather, maps and calendar background threads can be woken for an immediate refresh.
- `gcal.py revoke` logic is shared with the admin page.

## 0.8.0 (2026-10-16): Touch-friendly Ask tab

The tablet's touchscreen often misses small targets and drags, so the Ask tab now uses big targets and avoids scrolling.

**Changed**
- Voice is a large green button (150 × 124 px, about 3× the old size) to the right of the question box, spanning the box and
  the Ask/Clear row. It turns red and reads "Stop" while listening, as before. Ask and Clear are 48 px tall.
- Answers use larger type (27 px, was 20 px) and are **paged instead of scrolled**. The text area is cut to whole lines, and
  when an answer does not fit, big bold **▲ Back** / **Next ▼** buttons and a page counter appear beside it. Pages are
  recalculated when the screen size changes (e.g. entering fullscreen). The smaller-viewport layout scales the same way.

**Fixed**
- The status line wraps to two lines, so "Local fallback (small model, may be wrong)" is no longer cut off next to
  Ask/Clear. On small viewports the character counter is hidden to make room (the 1,000-character limit still applies).

Verified in headless Chromium at 800 × 400 and 533 × 267, not yet on the tablet.


## 0.7.0 (2026-10-16): Local AI fallback on legacy hardware

Ask and Voice keep working when the internet or the cloud APIs are unreachable, using small models on the same 2014-era
server (i3-4150, GTX 745). Design: `docs/LOCAL-FALLBACK-ARCHITECTURE.md`; measurements: `docs/LOCAL-AI-BENCHMARKS.md`.

**Added**
- Local fallback, used **only** when the cloud sends no response headers within 5 s or the connection fails. Any HTTP
  status (401/402/403/429/5xx) still shows the normal error. After a no-response, each service skips the cloud for 60 s,
  then lets one request probe it.
- `fallback.py`: the header deadline (a worker thread, so DNS is bounded too), the per-service cooldown gate, one shared
  local-inference lock, and a markdown/LaTeX-to-plain-text cleaner for local answers.
- Chat fallback: Qwen3.5-2B (Q4_K_M) through llama.cpp's `llama-server` on the CPU, with its own short plain-text prompt.
- Voice fallback: whisper.cpp `base` through `whisper-server` on the GPU via Vulkan (≈ 2.5× faster than the CPU here).
- `scripts/local-ai-start.sh` / `local-ai-stop.sh` (loopback only, low priority, warm-up, prompt cache capped);
  `scripts/start.sh` starts them unless `NO_LOCAL_AI=1`.
- `tools/eval_local_llm.py`: a repeatable quality and speed check for candidate models.
- Responses and usage logs carry `source` (`api` / `local`) and phase timings; the status endpoints report the fallback state.
- Config keys in `assistant` and `voice`: `fallback_enabled`, `fallback_base_url` (loopback only), `fallback_model`,
  `fallback_timeout_seconds`, `no_response_seconds`, `cloud_cooldown_seconds`, plus `fallback_max_output_tokens` (Ask)
  and `total_timeout_seconds` (Voice).
- 23 new tests (black-hole server, refused connections, stalled bodies, cooldown and probe, budgets, output cleaning).

**Changed**
- Cloud calls now give up waiting for the *first response* after 5 s when the fallback is enabled (the overall 30 s limit
  for a slow but answering cloud is unchanged).
- The page shows "Local fallback (small model, may be wrong)" for local answers and "(local fallback)" for local transcripts.
- A whole voice request is capped at 45 s on the server, leaving a margin under the page's 60 s limit.

**Measured** (cloud unreachable): Ask ≈ 8 s for the first question (5 s of it is the no-response deadline), then ≈ 1.8 s;
Voice ≈ 12 s first, then ≈ 3.3 s.


## 0.6.0 (2026-10-15): First public release

**Changed**
- Default timezone is now `UTC` (was a fixed local zone). The daily-limit message names the configured timezone.
- `assistant.allowed_hosts` defaults to empty. IP addresses and `localhost` are always accepted; add your hostname if you
  open the dashboard by name.
- `scripts/make-cert.sh` detects the server's IP and hostname (override with `DASHBOARD_IP`).
- The Info panel builds the https link from the current address and `https_port`, so no addresses are hard-coded in the page.
- `scripts/start.sh` explains how to create `config.json` if it's missing.
- `scripts/set-assistant-key.sh` prompts generically for any `KEY_NAME`.

**Added**
- `config.example.json` (copy to `config.json`, which is gitignored).
- README rewritten for new users: quick start, the keys each feature needs, setup for every service.
- MIT license.

**Removed**
- Machine-specific rollback script and test-video link.

## 0.5.0 (2026-10-15): HTTPS and one-tap voice

**Added**
- Optional https listener (`https_port`) using a private CA from `scripts/make-cert.sh`. Browsers only allow the
  microphone on secure origins.
- One-tap voice capture in the page via getUserMedia and Web Audio, for browsers without MediaRecorder (Chrome 50). It
  calibrates background noise, stops about 1.3 s after speech ends, gives up after 7 s of silence, and encodes 16 kHz WAV in
  JavaScript.
- `voice.auto_ask`: the transcribed question is asked automatically, so fewer taps are needed on poor touchscreens.
- The CA's public certificate is served at `/dashboard-ca.crt` for optional install on the device.

## 0.4.0 (2026-10-15): Google Calendar

**Added**
- Calendar tab: read-only agenda of upcoming events, grouped by day, from Google Calendar (`calendar.events.readonly`).
- `gcal.py auth | status | test | revoke`: one-time OAuth sign-in with a loopback redirect and PKCE, plus a paste-the-URL
  fallback for signing in from another machine.
- `/api/calendar`: served from a background cache (never calls Google during a request), LAN clients only. Hides cancelled
  and declined events.

## 0.3.0 (2026-10-15): Weather maps and voice input

**Added**
- Maps tab and a map card on the Weather tab: OpenWeatherMap precipitation, cloud, wind and temperature layers over a
  darkened OpenStreetMap base, composited with Pillow and rotating with a crossfade.
- Map builder with its own rate limiting, cached base tiles, backoff on 401/403/429, and stale marking.
- Voice button on the Ask tab: uploads a recording from the device's recorder app. The server checks the format by
  content, converts it with ffmpeg and transcribes it with Groq Whisper large-v3-turbo.
- Voice usage limits and a usage log that never stores audio or transcripts.

**Changed**
- Weather tab layout: clock and date on top, a compact today strip, and the map card filling the rest.
- Strong contrast and saturation settings for washed-out tablet LCDs.

## 0.2.0 (2026-10-15): AI assistant

**Added**
- Ask tab: standalone questions answered through Vercel AI Gateway (`google/gemini-2.5-flash-lite`), with no history or
  tool access.
- Cost guardrails: minimum interval, hourly and daily limits stored on disk (survive restarts), one request at a time, and a
  pause after the provider reports no credits. Upstream requests are never retried automatically.
- Security: same-origin and LAN-only checks, DNS-rebinding protection via a Host allowlist, provider redirects never
  followed, generic error messages, no CORS.
- `scripts/set-assistant-key.sh`: stores keys in a private `.env` without echoing them.
- Usage log with token counts that never stores questions or answers.

## 0.1.0 (2026-10-15): Clock and weather

**Added**
- Python standard-library server and an ES5 front end for an Android 4.4 tablet (800 × 400, Chrome 50).
- Clock driven by server time with round-trip correction, so the tablet's own clock doesn't matter. Has a 12h/24h toggle and
  a sync-status indicator.
- Weather from Open-Meteo, cached by the server every 15 minutes, with backoff and stale marking.
- Fullscreen button, Info/diagnostics panel, and a Cameras placeholder tab.
- Locked-down static and media serving (no directory listings, traversal and symlink escapes blocked).
- Start and stop scripts, and unit tests with mock upstream servers.
