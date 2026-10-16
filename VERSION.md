# Version history

Every meaningful change gets a new version here, with what improved. Versions follow
[Semantic Versioning](https://semver.org/): **minor** (0.x.0) for new features or behaviour changes, **patch** (0.0.x) for
fixes and small improvements. While the project is below 1.0, settings and routes may still change between minor versions.

The running version appears in the dashboard's Info panel and in `/api/config`. It comes from `VERSION` in `server.py`.

Versions 0.3.0 to 0.5.0 were numbered in hindsight from dated development snapshots. The code reported 0.2.0 throughout
that period.

---

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
