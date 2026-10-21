# Version history

Every meaningful change gets a new version here, with what improved. Versions follow
[Semantic Versioning](https://semver.org/): **minor** (0.x.0) for new features or behaviour changes, **patch** (0.0.x) for
fixes and small improvements. While the project is below 1.0, settings and routes may still change between minor versions.

The running version appears in the dashboard's Info panel and in `/api/config`. It comes from `VERSION` in `server.py`.

Versions 0.3.0 to 0.5.0 were numbered in hindsight from dated development snapshots. The code reported 0.2.0 throughout
that period.

---

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
