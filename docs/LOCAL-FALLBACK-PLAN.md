# Plan: local fallback models for the Ask and Voice tabs (rev 2, after adversarial review)

> **Status (2026-10-19): built, tested, benchmarked and live since 0.9.0.** The admin page's offline test switch is now the way to force the fallback (see the architecture doc). This is the plan as approved. The current design is in [LOCAL-FALLBACK-ARCHITECTURE.md](LOCAL-FALLBACK-ARCHITECTURE.md), and the measurements are in [LOCAL-AI-BENCHMARKS.md](LOCAL-AI-BENCHMARKS.md).
>
> **Where the build differs from this plan:**
> - **Model:** Qwen3.5-2B Q4_K_M replaced Qwen3 0.6B as the chat model, after a six-model comparison.
> - **Prompt:** the local model got its own short plain-text prompt (temperature 0.3, 120 tokens), plus a markdown/LaTeX cleaner (`fallback.plain_text`).
> - **GPU (step 8):** Vulkan made the LLM slower, so it stays on the CPU. Whisper runs on the GPU (≈ 2.5× faster). CUDA was not installed: Maxwell lacks the instructions llama.cpp's fast CUDA kernels need.
> - **Config:** flat keys (`fallback_enabled`, `no_response_seconds`, `cloud_cooldown_seconds`, …) instead of a nested `fallback` block, because the config loader merges only one level deep.
> - **Memory:** llama-server runs with `--cache-ram 256`; its prompt cache defaults to 8 GiB.
> - **Step 2 (still open):** the Plex-load runs, the tablet speech set and the tablet end-to-end test haven't been done. Chrome was open during all measurements.

## Context
The tablet dashboard's Ask tab (Vercel AI Gateway, `assistant.py`) and Voice button (Groq Whisper, `voice.py`) stop working when the internet or the API is unreachable. The goal is to run small local models on this old server, **Qwen3 0.6B** (chat) and **whisper base** (speech-to-text), as fallbacks. The project is a portfolio piece, so every performance or resilience claim must come from measurements, not estimates.

### Fallback policy (the user's decision, stated explicitly)
- **Local is used only when the cloud gives no response:** no HTTP response headers within **5 s**, measured from the start of the attempt and covering DNS, connect, TLS and send.
- **HTTP responses never trigger fallback.** This covers any status line received, including 401, 402 (credits exhausted), 403, 429 (rate limit) and 5xx. These keep today's error behaviour.
- **After headers arrive, failures never trigger fallback.** If the body stalls, is cut off or exceeds the existing 30 s deadline, the result is today's timeout or network error.
- **Consequence, accepted:** this protects against connectivity and no-response failures, not against every cloud outage. Running out of credits or hitting a provider 500 still shows an error.

### Other decisions
- Voice is Spanish + English: multilingual `ggml-base`, language auto-detect.
- GPU work happens only if the CPU measurements show a worthwhile bottleneck. If so, try Vulkan first and CUDA last.
- After a no-response, skip the cloud for 60 s per service. Then allow **one** cloud probe.
- The UI label is **"Local fallback"**, not "Offline": the API can fail while the internet still works.

### Server facts (scanned 2026-10-15; RAM and GPU figures are estimates until measured)
- i3-4150: **2 physical cores** / 4 threads, AVX2/FMA/F16C.
- 15 GB RAM, about 5 GB free; about 7 GB normally, because Chrome runs only while the user works at the machine.
- GTX 745: Maxwell, compute 5.0, 4 GB DDR3, driver 535 / CUDA 12.2.
- gcc 13, make and git are present. No cmake.
- Plex, Jellyfin and NoMachine stay running.
- **Tablet address:** the tablet uses **https://<server-ip>:8443/**, because the microphone needs a secure context. The same `server.py` also serves http on 8099, which has no voice. End-to-end tests go through 8443.

## Layout (outside the dashboard dir, so backups and the public-repo copy are unaffected)
```
../local-ai/          # a sibling of the dashboard folder
  src/llama.cpp, src/whisper.cpp          # clones at pinned release tags
  build/llama-cpu, build/whisper-cpu      # one build dir per project per backend
  build/llama-vulkan, build/whisper-vulkan   (only if step 8 happens)
  models/                                  # Qwen3-0.6B GGUF + ggml-base.bin, sha256 recorded
```
Both servers bind to **127.0.0.1 only** and run at `nice -n 10`.
- `llama-server`: `--parallel 1`, `-c 2048`, `--jinja`.
- `whisper-server`: `-l auto`.
- Thread count is chosen from measurements in step 2 (2 vs 4).

## Steps

### 0. Save the plan, snapshot, pin versions
- Copy this plan to `docs/LOCAL-FALLBACK-PLAN.md`.
- Snapshot the code to `backups/pre-localai-*` (no `.env`, no `data/`).
- Pick the exact llama.cpp and whisper.cpp release tags.
- **Before installing anything**, read each tag's build docs for its minimum cmake and compiler versions, and the server flags it supports. Confirm these flags exist at the pinned tags: `--jinja`, `chat_template_kwargs` / `enable_thinking`, whisper-server's `/inference` and `verbose_json`.
- Ubuntu 24.04 ships cmake 3.28. If a tag needs more, stop and report.
- Record the tags and sha256 sums in the README.

### 1. CPU builds and models
- The user runs `sudo apt install cmake`.
- Build both projects CPU-only into separate dirs (`-DGGML_NATIVE=ON`).
- Download the models:
  - Qwen3-0.6B: official Q8_0, plus Q4_K_M for comparison
  - `ggml-base.bin`
- Verify the sha256 sums.

### 2. Measurements (Chrome closed, Plex, Jellyfin and NoMachine running)
- **Inference only:**
  - `llama-bench` and `whisper-bench` at `-t 2` vs `-t 4`, Q8_0 vs Q4_K_M
  - **repeated under representative load:** a Plex or Jellyfin stream playing, with the user's help to start it
  - also check that the stream doesn't stutter
- **Memory:** resident RAM per server from `/proc/<pid>/status`, idle and during a request.
- **Speech set:** the user records about 12 clips through the tablet's own Voice path, saved via a temporary debug flag:
  - Spanish and English, short and long
  - a name or place (e.g. "Ensenada")
  - **silence, background noise and TV in the background**
  Compare the whisper-base transcripts with Groq's transcripts of the same clips as a reference. Also check that silence and noise produce no text, i.e. that `Transcriber._parse` treats them as no speech.
- **Decision gate:** pick the thread count, quantization and Whisper model (base, or `small` if base fails the speech set) from these numbers. Write all results into the README.

### 3. Start/stop scripts
- New `scripts/local-ai-start.sh` and `scripts/local-ai-stop.sh`, using the same pidfile and cmdline-check pattern as `scripts/start.sh` and `scripts/stop.sh` (pidfiles in `run/`, logs in `logs/`).
- Health check: `GET /health` (llama-server) and a tiny request to whisper-server.
- `start.sh` gets an optional call to `local-ai-start.sh`. Autostart stays deferred.

### 4. Shared fallback machinery (new small module `fallback.py`, used by both services)
**`call_with_header_deadline(opener, req, header_deadline, total_deadline)`**
- Runs `opener.open(req)` (which returns once headers arrive) in a worker thread and waits on an `Event` with `header_deadline`. That bounds DNS resolution too, which a socket timeout alone does not.
- Outcomes:
  - **Headers arrived in time:** read the body as today (bounded size, overall `total_deadline`). Any later failure → `UpstreamError` with `response_started=True`, so no fallback.
  - **HTTPError (status received):** classify as today, with `response_started=True`.
  - **Deadline passed with no headers, or a connect/DNS failure before headers:** raise `NoResponse`. This is the **only** fallback trigger.
- A late worker thread closes any response it eventually gets. At most **one** straggler per service: if the previous one is still alive, treat the cloud as no-response immediately instead of piling up threads.

**`CloudGate` (one per service)**
- `time.monotonic()` state under a `threading.Lock`.
- `allow_cloud()`:
  - true when not cooling down
  - during cooldown → false
  - after cooldown → true for exactly **one** probe; other callers stay local until that probe resolves
- `mark_no_response()` starts the 60 s cooldown. `mark_ok()` clears it.

**Shared local-inference lock**
- One lock across chat and voice, so only one local model runs at a time on the 2-core CPU. The wait is bounded by the request's remaining budget.

### 5. `assistant.py` integration
- New `assistant.fallback` config (defaults in `DEFAULT_ASSISTANT_CONFIG`, checks in `validate_assistant_config`):
  - `enabled`
  - `base_url: "http://127.0.0.1:8091/v1"`
  - `model`
  - `no_response_seconds: 5`
  - `cooldown_seconds: 60`
  - `local_timeout_seconds: 25`
  - `max_output_tokens: 200`
- `_call_upstream` (`assistant.py:568`) uses `call_with_header_deadline`.
- In `ask` (`assistant.py:467`):
  - `NoResponse` → `gate.mark_no_response()`, then local.
  - Cooldown → local directly.
  - Every other `UpstreamError` → `_map_upstream_error` unchanged.
- Local request:
  - same message structure as `_build_payload`
  - no auth header
  - `chat_template_kwargs: {"enable_thinking": false}`
  - system prompt gains "Answer in the same language as the question"
- **Error messages, written separately:**
  - cloud no-response + local failed: "The AI service didn't respond and the local fallback is unavailable."
  - cooldown + local failed: "Using the local fallback (the AI service was unreachable recently), but it is unavailable right now."
- Response includes `"source": "api" | "local"`.
- Usage log gains `source`, `outcome` (`success_local`, `local_error`) and phase timings: `api_wait_ms`, `local_ms`.
- Local answers count toward the existing caps.

### 6. `voice.py` integration
- Same pattern as step 5 with `voice.fallback`:
  - `base_url: "http://127.0.0.1:8092"`
  - same 5 s and 60 s settings
  - `local_timeout_seconds: 20`
  - its own `CloudGate`
- Local call: `POST /inference` via the existing `encode_multipart` with `response_format=verbose_json`, `temperature=0`, `language=auto`. Parse it with `Transcriber._parse` (`voice.py:347`).
- Log `convert_ms`, `api_wait_ms`, `local_ms` and `source`.

### 7. Deadlines and tablet UI (`web/app.js`, targeted ES5 edits)
**Worst-case budgets (the server enforces each phase):**
| Path | Phases | Total | Client timeout | Margin |
|---|---|---|---|---|
| Ask, cloud answers late | cloud body ≤ 30 s, no fallback | 30 s | `AI_TIMEOUT_MS` 45 s | 15 s |
| Ask, falls back | 5 s no response + ≤ 25 s local, lock wait inside the 25 s | 30 s | 45 s | 15 s |
| Voice | ffmpeg ≤ 20 s + 5 s + ≤ 20 s local (or 30 s cloud body) | ≤ 55 s | `VOICE_TIMEOUT_MS` 60 s | **5 s, too thin** |

- **Voice fix:** cap the whole voice request server-side at 45 s. The local or cloud phase gets whatever remains after conversion. Real conversion of a ≤ 30 s clip is far below 20 s, so check its timing in step 2. That leaves a 15 s margin under the 60 s client timeout.
- UI: when `data.source === 'local'`, show the small label **"Local fallback"** next to the answer or transcript (the `aiAsk` handler is at `app.js:723`).

### 8. GPU (only if step 2 shows a worthwhile bottleneck)
- **Vulkan:**
  - First check the pinned tags' Vulkan requirements (header and `glslc` versions) against Ubuntu's 1.3.275.
  - The user runs `sudo apt install glslc libvulkan-dev`.
  - Build `build/*-vulkan` and rerun the step 2 suite, including under load and the NoMachine/Plex checks.
- **CUDA (last resort):**
  - `apt-get -s install nvidia-cuda-toolkit` (12.0) dry run first. Abort if it touches any driver or `libnvidia-*` 535 package.
  - Never install NVIDIA's `cuda` meta-package.
  - Build with `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=50`.
- Adopt a GPU build only if end-to-end time improves noticeably or CPU contention with Plex goes down.

### 9. Docs and memory
- README "Local fallback" section: the policy (including what it does *not* cover), ports, models and tags, scripts, measured results and how to test.
- Update the memory notes.
- Syncing the public repo is later and optional; models are never committed.

## Verification
1. **Unit tests**, using test-only mock servers and a dummy key, never the real `.env`:
   - **black-hole server** (accepts the TCP connection, never sends) → fallback after `no_response_seconds` of about 0.5 s
   - closed port → immediate fallback
   - headers sent, then body stalls → timeout error, **no** fallback
   - 401/402/403/429/500 → today's errors, no fallback
   - cooldown skips the cloud; after expiry exactly one probe goes out (concurrent callers stay local)
   - straggler limit
   - local down during cooldown → the cooldown-specific message
   - shared local lock respects the remaining budget
   - voice 45 s total cap

   `python3 -m unittest discover -s tests`. All existing 51+ tests still pass.
2. **Isolated live instance:**
   - a second `server.py` on a spare port with a test config and a **dummy key** in a separate env file
   - the cloud `base_url` points at the local black-hole server, so no real credentials go anywhere unexpected
   - real local models answer
   - production instance and network untouched
3. **End-to-end timing on the tablet at https://<server-ip>:8443/:**
   - forced fallback, then normal mode
   - measure tap-to-answer from the log's phase timings plus a stopwatch from tap to text on screen
   - include the step 2 speech set
4. **Resource check:** `free -h`, `top` and `nvidia-smi` while idle and during requests, with a Plex stream running. No stutter, and NoMachine stays responsive.
