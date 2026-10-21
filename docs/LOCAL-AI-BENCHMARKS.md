# Local AI on Old Hardware: Benchmarks

The question this answers: *how much useful local AI can a 2014 desktop (2-core i3, 16 GB DDR3, a 4 GB GTX 745) deliver next to a running media server, without buying anything?*

Measurements behind the local fallback for the tablet dashboard ([architecture](LOCAL-FALLBACK-ARCHITECTURE.md)): six LLM builds, two speech backends, CPU vs GPU, and end-to-end latency. Dates: 2026-10-15/16.

**Summary**
- **Chat:** **Qwen3.5-2B Q4_K_M on the CPU**: 11.3 tok/s, 30/33 on the quality check, short plain-text answers in 1–6 s, ≈ 2 GB RAM. The smaller models were 2–3× faster but scored 21–24/33. They failed the regional and arithmetic questions; Gemma 3 1B also refused facts and answered Spanish in English.
- **Speech:** **whisper base on the GPU via Vulkan**: ≈ 2.0 s per clip vs ≈ 5 s on CPU, same transcripts.
- **GPU vs CPU:** the GTX 745 made the LLM **slower** (9.4 vs 11.3 tok/s) but Whisper **≈ 2.5× faster**. The system now uses each where it wins.
- **End to end with the cloud unreachable:** Ask ≈ 8 s for the first question (5 s of that is the no-response deadline), then ≈ 1.8 s. Voice ≈ 12 s first, then ≈ 3.3 s.

---

## 1. Test environment

| | |
|---|---|
| CPU | Intel Core i3-4150 (Haswell, 2014): 2 cores / 4 threads, 3.5 GHz, 3 MiB L3, AVX2 + FMA + F16C |
| RAM | 16 GB DDR3, two modules at **1600 MT/s** (configured speed, from `dmidecode`): the platform maximum. Dual channel is assumed (2 DIMMs), giving a theoretical 25.6 GB/s (23.8 GiB/s). |
| GPU | NVIDIA GeForce GTX 745 (Maxwell GM107, compute 5.0), 4 GB **DDR3** on a 128-bit bus at 900 MHz (≈ 28.8 GB/s), PCIe gen 2 ×16. Vulkan reports no fp16 math, no integer dot product, no matrix cores. |
| Driver | NVIDIA 535 (CUDA 12.2 runtime, Vulkan 1.3.242) |
| OS / tools | Ubuntu 24.04.4, kernel 6.17, gcc 13.3, cmake 3.28.3, Python 3.12.3 |
| Inference | llama.cpp **b11327** (552f18f); whisper.cpp **v1.9.5** (d1be6fd); CPU builds with `GGML_NATIVE=ON`; Vulkan builds with `GGML_VULKAN=ON` (glslc 2023.8) |
| Concurrent load | Plex, Jellyfin, Docker, a NoMachine session and a desktop with **Chrome open** (≈ 20 processes) during all runs; no media stream playing. Treat absolute numbers as slightly pessimistic, and see "Not yet measured". |

### Models

| Model | File | Size | Source |
|---|---|---|---|
| Qwen3 0.6B | Q8_0 | 639 MB | `Qwen/Qwen3-0.6B-GGUF` (official) |
| Qwen3 0.6B | Q4_K_M | 397 MB | `unsloth/Qwen3-0.6B-GGUF` |
| Qwen3.5 0.8B | Q8_0 / Q4_K_M | 812 / 533 MB | `unsloth/Qwen3.5-0.8B-GGUF` |
| Gemma 3 1B (it) | Q8_0 / Q4_K_M | 1069 / 806 MB | `unsloth/gemma-3-1b-it-GGUF` |
| **Qwen3.5 2B** | **Q4_K_M** / Q8_0 | **1281** / 2012 MB | `unsloth/Qwen3.5-2B-GGUF` |
| whisper base (multilingual) | `ggml-base.bin` | 148 MB | `ggerganov/whisper.cpp` |

All files were checked against the publisher's sha256 (`local-ai/models/SHA256SUMS`). Qwen3.5 uses a hybrid architecture (`qwen35` in llama.cpp) that mixes linear-attention and full-attention layers, and is non-thinking by default at these sizes.

---

## 2. LLM throughput (`llama-bench`)

`llama-bench -p 128 -n 64 -r 3`, `nice -n 10`. **pp128** = prompt processing ("reading", tokens/s); **tg64** = generation ("writing", tokens/s). The ± values are llama-bench's standard deviation over 3 repetitions.

### CPU, 2 vs 4 threads

| Model | Quant | Size (MiB) | pp128, 2 thr | tg64, 2 thr | pp128, 4 thr | tg64, 4 thr |
|---|---|---:|---:|---:|---:|---:|
| Qwen3 0.6B | Q8_0 | 604 | 94.2 ± 1.8 | **26.6 ± 0.2** | 94.6 ± 3.6 | 23.7 ± 0.5 |
| Qwen3 0.6B | Q4_K_M | 373 | 136.5 ± 2.0 | **38.7 ± 0.4** | 130.4 ± 6.5 | 34.6 ± 2.5 |
| Qwen3.5 0.8B | Q8_0 | 764 | 70.1 ± 0.8 | **17.7 ± 0.0** | 73.8 ± 1.2 | 14.7 ± 0.2 |
| Qwen3.5 0.8B | Q4_K_M | 497 | 98.3 ± 0.4 | **22.3 ± 0.8** | 94.6 ± 1.1 | 19.8 ± 0.4 |
| Gemma 3 1B | Q8_0 | 1014 | 60.6 ± 3.0 | 16.7 ± 0.0 | — | — |
| Gemma 3 1B | Q4_K_M | 762 | 54.9 ± 0.9 | 18.4 ± 0.1 | — | — |
| **Qwen3.5 2B** | **Q4_K_M** | 1208 | **39.2 ± 2.4** | **11.3 ± 0.2** | — | — |
| Qwen3.5 2B | Q8_0 | 1905 | 29.2 ± 0.3 | 8.1 ± 0.1 | — | — |

**4 threads never helped generation** (it was 10–17% slower) and barely changed prompt processing. Everything ships with 2 threads.

### Why: generation is memory-bandwidth bound

Each generated token reads (nearly) the whole model once, so *tg × model size* approximates the memory bandwidth actually achieved:

| Model / quant | tg64 × size |
|---|---:|
| Gemma 3 1B Q8_0 | 16.5 GiB/s |
| Qwen3 0.6B Q8_0 | 15.7 GiB/s |
| Qwen3.5 2B Q8_0 | 15.0 GiB/s |
| Qwen3 0.6B Q4_K_M | 14.1 GiB/s |
| Gemma 3 1B Q4_K_M | 13.7 GiB/s |
| Qwen3.5 2B Q4_K_M | 13.3 GiB/s |
| Qwen3.5 0.8B Q8_0 | 13.2 GiB/s |
| Qwen3.5 0.8B Q4_K_M | 10.8 GiB/s |

The figure is nearly constant, 11–16 GiB/s (≈ 45–70% of the RAM's theoretical 23.8 GiB/s), across models 2–5× apart in size. That's the signature of a memory-bound workload. More threads can't fix it, and hyper-threads add contention. Two second-order effects:
- **Q4_K formats sit at the low end:** dequantizing them costs CPU work that Q8_0 avoids, which is why Gemma's Q4_K_M is barely faster than its Q8_0.
- **Qwen3.5 sits ≈ 15% lower than Qwen3 per byte:** its linear-attention layers add compute.

Practical consequence: **on this CPU, speed is set by file size**. Q4_K_M is the right trade-off for the 2B model.

**Upgrade implications (estimates, not measured):**
- **Same-socket i7-4770** (4 cores / 8 threads, 8 MB L3, same DDR3-1600 memory controller): generation would stay ≈ 11–12 tok/s, but prompt processing, which is compute-bound, should roughly double. In the measured 3.0 s local answer, prompt reading was ≈ 2.2 s, so expect ≈ 2 s per answer, plus 2 spare cores for the media server.
- **RAM:** already at the platform's 1600 MT/s maximum, so no gain there.
- **GPU:** a newer card with GDDR5/GDDR6 memory and fp16/dp4a support would be the largest gain. It is out of scope for this project, which deliberately sticks to the hardware on hand. The result above stands on its own: even a weak 2014 GPU is worth using for the right workload (speech), and wrong for another (LLM generation).

### GPU (Vulkan, all layers offloaded, `-ngl 99`)

| Model | Quant | pp128 GPU | pp128 CPU | tg64 GPU | tg64 CPU | tg64 GPU vs CPU |
|---|---|---:|---:|---:|---:|---:|
| Qwen3 0.6B | Q8_0 | 110.3 | 94.2 | 14.6 | 26.6 | **−45%** |
| Qwen3.5 2B | Q4_K_M | 33.6 | 39.2 | 9.4 | 11.3 | **−17%** |
| Qwen3.5 2B | Q8_0 | 35.9 | 29.2 | 4.6 | 8.1 | **−43%** |

Peak VRAM during these runs was 2.35 GiB (including ≈ 0.3 GiB used by the desktop), and the card reached 68 °C.

**Interpretation:**
- The GTX 745's memory is faster on paper (≈ 28.8 GB/s), yet the GPU reached only ≈ 8.6–11 GiB/s of effective model reads.
- Without fp16 or integer-dot instructions, llama.cpp's Vulkan kernels dequantize in fp32 on 384 Maxwell cores, so the GPU is compute-bound well before it is bandwidth-bound.
- CUDA would face the same hardware limits: sm_50 lacks the `dp4a` instruction that llama.cpp's fast quantized CUDA kernels use. It wasn't worth installing the CUDA toolkit to confirm.
- **Decision: the LLM stays on the CPU.**

---

## 3. Speech-to-text (whisper base)

Clips: `jfk.wav` (11 s, ships with whisper.cpp), and the same trimmed to 4.5 s (a typical spoken question). All runs use 2 threads and `-l auto`.

### Why a 4.5 s clip costs as much as an 11 s one

Whisper's encoder always processes a **30 s window**, and language auto-detection runs the encoder **twice** (once to detect, once to transcribe). On the CPU each encoder pass takes ≈ 2.2 s, regardless of clip length (model load is only 0.1 s):

| CPU, whisper-cli | encode | total |
|---|---:|---:|
| 4.5 s clip | 2 × 2.2 s | 4.9–5.4 s |
| 11 s clip | 2 × 2.2 s | 5.6 s |
| 11 s clip, 4 threads | 2 × 2.1 s | 5.4 s |

### Experiment: shrinking the audio window (`audio_ctx`), rejected

| Clip | Setting | Total | Transcript |
|---|---|---:|---|
| 11 s | full window | 5.6 s | exact |
| 11 s | `-ac 768` (≈ 15 s window) | 4.1 s | exact |
| 11 s | `-ac 512` (≈ 10 s, shorter than the clip) | **13.7 s** | decoder looped; punctuation lost |
| 4.5 s | full window | 5.0 s | "And so my fellow Americans asked not" |
| 4.5 s | `-ac 300` | 3.0 s | "And so, my fellow Americans." (**words dropped**) |
| 4.5 s | `-ac 300 -l en` (one encode only) | **9.9 s** | "…asked not to." (decoder looped) |

Small windows speed up the encoder but make the decoder unreliable on this model. The shipped configuration keeps the full window.

### CPU vs GPU

| Backend | Mode | 4.5 s clip | 11 s clip | Notes |
|---|---|---:|---:|---|
| CPU | whisper-cli | 4.9–5.4 s | 5.6 s | encode 2 × 2.2 s |
| CPU | whisper-server (warm) | 4.8–5.6 s | — | first request after start ≈ 9.5 s |
| **Vulkan (GTX 745)** | whisper-cli | 2.3 s | 2.6 s | encode 2 × 0.93 s; first run after the build 11.0 s (shader compile) |
| **Vulkan (GTX 745)** | **whisper-server (warm)** | **1.95–1.97 s** | **2.11 s** | 3 runs each; ≈ 315 MiB VRAM |

Transcripts were identical on both backends. Whisper's encoder is dense float matrix math with no quantized weights, which is where even this GPU beats two CPU cores. **Decision: Whisper runs on the GPU.**

**Not yet measured:** accuracy on real Spanish speech, noise, silence and TV audio from the tablet's microphone. The public JFK sample is clean English.

---

## 4. LLM answer quality

### Method

- **Questions:** 12, the standalone kind a family asks a kitchen tablet. 7 are in Spanish and 5 in English: regional facts, a literature fact, unit conversion, simple arithmetic, basic science, and one open-ended request.
- **Request:** sent through llama-server exactly as the dashboard sends it, with thinking disabled.
- **Runs:** 3 per model (temperature 0.8, then 0.2 twice), `max_tokens` 200, using the cloud system prompt plus a "same language" line (the *legacy* local prompt).
- **Scoring:** fixed keyword rules (`tools/eval_local_llm.py`).
  - **Keyword:** the expected answer appears.
  - **Strict:** it appears *and* the answer doesn't also say it can't answer. This penalises Gemma's "I cannot tell you how many legs a spider has. Spiders have eight legs."
  - The open-ended question (three sleep tips) is judged by reading, not scored.
  - Every answer was also read by hand; notes below.

Reference answers: Mexicali; Gabriel García Márquez; Ensenada–Tijuana ≈ 100 km by road (80–130 accepted); 86 °F; 8 legs; Jupiter; 2 apples; 132; Paris; 7 days; 42.

### Results (correct runs out of 3)

| Question | Qwen3 0.6B Q8 | Qwen3.5 0.8B Q8 | Gemma 3 1B Q8 | Gemma 3 1B Q4 | **Qwen3.5 2B Q4** | Qwen3.5 2B Q8 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| Capital of Baja California | 0 | 0 | 0 | 0 | **2** | 2 |
| Author of *Cien años de soledad* | 0 | 2 | 3 | 3 | **2** | 3 |
| Ensenada–Tijuana distance | 0 | 0 | 0 | 0 | **2** | 1 |
| 30 °C in °F | 3 | 3 | 2 | 1 | **3** | 3 |
| Spider legs | 3 | 3 | 3 | 2 | **3** | 3 |
| Largest planet | 3 | 3 | 3 | 3 | **3** | 3 |
| 3 apples, eat 1 | 3 | 3 | 3 | 3 | **3** | 3 |
| 12 × 11 | 0 | 0 | 3 | 3 | **3** | 3 |
| Capital of France | 3 | 3 | 1 | 3 | **3** | 3 |
| Days in a week | 3 | 3 | 3 | 2 | **3** | 3 |
| 15 + 27 | 3 | 3 | 3 | 3 | **3** | 3 |
| **Keyword score (of 33)** | 21 | 23 | 24 | 23 | **30** | 30 |
| **Strict score (of 33)** | 21 | 23 | **15** | **17** | **30** | 30 |
| Answered Spanish questions in Spanish (of 21) | 21 | 21 | 3 | 6 | 21 | 21 |
| Answers with markdown / LaTeX (of 36) | 3 / 0 | 4 / 0 | 2 / 0 | 1 / 0 | 6 / 3 | 6 / 2 |
| 12-question set, seconds (3 runs) | 11 / 10 / 10 | 33 / 27 / 33 | 75 / 64 / 69 | 73 / 58 / 60 | 65 / 44 / 48 | 89 / 84 / 87 |
| Avg tokens per answer | 16 | 34–42 | 72–79 | 73–80 | 36–54 | 52–56 |
| Slowest single answer | 1.9 s | 6.9 s | 10.7 s | 9.4 s | 14.3 s | 18.7 s |
| llama-server RSS, start → after 36 answers | 0.88 → 0.89 GiB | 0.90 → 1.75 GiB | 0.72 → 1.22 GiB | 0.91 → 1.03 GiB | 1.82 → 2.52 GiB | 2.02 → 2.81 GiB |

The RSS growth is llama-server's prompt cache (default limit 8 GiB), not the model. With `--cache-ram 256` the 2B model stayed at ≈ 1.9–2.0 GB.

### What the answers looked like (hand review)

- **Qwen3 0.6B:** fastest and briefest. Fluent but confidently wrong on anything regional:
  - Baja California's capital: "San José", "La Paz", "Baja California de San Salvador".
  - *Cien años de soledad*: "Cervantes", or an invented author.
  - Ensenada–Tijuana: 220 to 1,500 km.
  - 12 × 11: treated as division (≈ 1.09).
- **Qwen3.5 0.8B:** knew García Márquez but padded answers with false side claims ("known for *El Señor de los Anillos*"; "Saturn is actually the largest planet by volume"). Distances of 576–1,400 km. Also treated 12 × 11 as 12 / 11.
- **Gemma 3 1B:** best at arithmetic among the small models.
  - The cloud prompt's sentence "You have no access to live weather, calendars, cameras, or server status" made it refuse plain facts: "I do not have access to real-time information… Therefore, I cannot tell you the capital of France." This happened in about half its answers.
  - It answered Spanish questions in English.
  - Q4_K_M once said 30 °C is 54 °F.
- **Qwen3.5 2B:** the first model that knew Mexicali and roughly the Ensenada–Tijuana distance (120 km). It still has slips:
  - One Q4_K_M answer named Mexicali and then said it "is not the capital".
  - One Q8_0 answer said "Cancún"; another called García Márquez "la autora argentina".
  - It used `**bold**` and `$F = (C \times 1.8) + 32$`-style LaTeX, which would print as raw symbols in the plain-text answer box.
- **Q8_0 vs Q4_K_M at 2B:** same score; Q8_0 is 30–45% slower and wordier.

### Prompt tuning (Qwen3.5 2B Q4_K_M)

Shipped local prompt (`assistant.LOCAL_SYSTEM_PROMPT`):

> Answer the user's question in one or two short sentences, in the same language as the question. Use plain text only: no markdown, no asterisks, no LaTeX or math notation. If you are not sure of a fact, say so briefly.

Request settings: `temperature` 0.3 and `max_tokens` 120. Answers then pass through `fallback.plain_text()`.

| | Legacy prompt (T 0.8/0.2/0.2, 200 tokens) | **Tuned prompt (T 0.3, 120 tokens)** |
|---|---:|---:|
| Score (strict) | 30/33 | **30/33** (10/11 in each of 3 runs) |
| 12-question set | 44–65 s | **24.1–28.5 s** |
| Average tokens per answer | 36–54 | **16–18** |
| Slowest answer | 14.3 s | **5.8 s** |
| Answers needing markdown/LaTeX cleanup | 9 of 36 | **0 of 36** |

The only remaining miss is the Ensenada–Tijuana distance (160–270 km). Typical tuned answers:
- "La capital de Baja California es Mexicali."
- "12 por 11 es 132."
- "Cien años de soledad fue escrito por Gabriel García Márquez."

### Model decision

| Model | Verdict |
|---|---|
| **Qwen3.5 2B Q4_K_M** | **Shipped.** Best accuracy at usable speed. |
| Qwen3.5 2B Q8_0 | Same accuracy, slower, +0.7 GB. |
| Qwen3 0.6B Q8_0 | Fast, but wrong on regional facts and multiplication. Good as an emergency low-RAM option. |
| Qwen3.5 0.8B | Slower than Qwen3 0.6B with little accuracy gain. |
| Gemma 3 1B | Refusals and wrong language; slowest of the small models. |

---

## 5. End-to-end latency (dashboard → local models)

- **Setup:** a second dashboard instance on a spare port with dummy API keys, and the cloud URLs pointed at a local "black hole" socket that accepts connections and never answers. This is the real code path, using the real local models.
- **Timings:** wall-clock from `curl`; phase timings from the dashboard's usage log.

### Shipped configuration (Qwen3.5-2B Q4_K_M on CPU, whisper base on Vulkan), 2026-10-16

| Request | Total | Convert | Cloud wait | Local |
|---|---:|---:|---:|---:|
| Ask #1, cloud silent: "¿Cuál es la capital de Baja California?" → "…es Mexicali." | **7.97 s** | — | 5.00 s | 2.95 s |
| Ask #2, cooldown: "¿Cuánto es 12 por 11?" → "12 por 11 es 132." | **1.90 s** | — | skipped | 1.88 s |
| Ask #3, cooldown: largest planet → "Jupiter…" | **1.75 s** | — | skipped | 1.74 s |
| Voice #1, cloud silent, 4.5 s clip | **11.97 s** | 4.03 s* | 5.00 s | 2.93 s |
| Voice #2, cooldown, 11 s clip | **3.53 s** | 0.47 s | skipped | 3.05 s |
| Voice #3, cooldown, 4.5 s clip | **3.24 s** | 0.32 s | skipped | 2.90 s |

\* Cold start of the snap-packaged ffmpeg. The same conversion takes 0.3–0.5 s when warm.

The black hole saw exactly **2 connections** for these 6 requests: one per service. The cooldown kept every later request away from the cloud.

### Live dashboard with the offline test switch (Realistic mode), 2026-10-19

The production dashboard (0.9.0) itself, with the admin page's **Fallback test** button and the switch on for Ask and Voice. Phase timings come from the usage log; those entries carry `"simulated": true`.

| Request | Total | Convert | Cloud wait | Local |
|---|---:|---:|---:|---:|
| Ask, cloud "silent" (simulated) | **6.67 s** | — | 5.00 s | 1.67 s |
| Voice, cloud "silent", 11 s sample (`jfk.wav`) | **9.55 s** | 1.52 s | 5.00 s | 3.03 s |

The simulated wait matched the real 5 s deadline, as it should, because Realistic mode runs the same worker-thread path. Turning the switch off reset both cooldowns, and the next requests went to the cloud.

### First version (Qwen3 0.6B Q8_0 on CPU, whisper base on CPU), 2026-10-15

| Request | Total | Cloud wait | Local |
|---|---:|---:|---:|
| Ask, cloud silent | 5.8 s | 5.0 s | 0.8 s |
| Ask, cooldown | 0.9 s | skipped | 0.9 s |
| Voice, cloud silent (4.5 s clip) | 13.5 s | 5.0 s | 8.0 s (convert 0.5 s) |

Moving Whisper to the GPU cut local voice time from 8.0 s to ≈ 3 s. Switching to the 2B model adds about 1–2 s per answer and buys much better accuracy.

**Open question:** whisper-server answers in ≈ 2.0 s when called directly, but the dashboard's local voice phase measures ≈ 2.9 s. The ≈ 0.9 s gap (multipart upload, `verbose_json` output, or lock handling) hasn't been profiled yet.

---

## 6. Resource footprint (shipped configuration)

| Process | RAM (RSS) | VRAM | CPU while idle | Priority |
|---|---:|---:|---|---|
| llama-server (Qwen3.5-2B Q4_K_M, `-c 2048`, `--cache-ram 256`) | 1.9–2.0 GB, flat | — | ≈ 0 | nice 10, 2 threads |
| whisper-server (base, Vulkan) | ≈ 0.2 GB | ≈ 315 MiB | ≈ 0 | nice 10, 2 threads |

Machine headroom before starting: ≈ 5 GB RAM available with Chrome open (≈ 7 GB without), and ≈ 3.7 GB of free VRAM.

---

## 7. Not yet measured / caveats

- **Under media load:** none of these runs had a Plex or Jellyfin stream playing. Whisper now shares the GPU with Jellyfin's transcoder and NoMachine's encoder.
- **Real speech from the tablet microphone**, in Spanish, with noise, silence and a TV in the background.
- **Tap-to-answer on the tablet** over HTTPS.
- **Sample size:** the quality check is small (12 questions × 3 runs) and keyword-scored. It separates models that are far apart; it can't rank models within a couple of points of each other.
- **Background load:** Chrome and the other services were running throughout, so all timings include that background noise.

## 8. Reproducing

```bash
# throughput
local-ai/build/llama-cpu/bin/llama-bench -m <model.gguf> -t 2 -p 128 -n 64 -r 3 -o md
local-ai/build/llama-vulkan/bin/llama-bench -m <model.gguf> -ngl 99 -t 2 -p 128 -n 64 -r 3 -o md
local-ai/build/whisper-<cpu|vulkan>/bin/whisper-cli -m local-ai/models/ggml-base.bin -t 2 -l auto -f <clip.wav> -nt
# quality (any OpenAI-compatible server)
python3 tools/eval_local_llm.py --url http://127.0.0.1:8091/v1 --runs 3 --out results.json
```

Raw data lives in `local-ai/bench/`: the llama-bench tables (`llama-*.md`) and every answer from every quality run (`quality/raw/*.json`, plus the RSS samples and `quality/scores.json`).
