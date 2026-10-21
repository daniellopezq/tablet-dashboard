#!/usr/bin/env python3
"""Quality/speed check for the local fallback LLM (any OpenAI-compatible server, e.g. llama-server).

Sends the 12-question set used in docs/LOCAL-AI-BENCHMARKS.md with the same request the dashboard
sends (assistant.LOCAL_SYSTEM_PROMPT, fallback_max_output_tokens, LOCAL_TEMPERATURE, thinking off),
cleans answers with fallback.plain_text, and scores them with fixed keyword rules.

    python3 tools/eval_local_llm.py                      # server on 127.0.0.1:8091, 2 runs
    python3 tools/eval_local_llm.py --runs 3 --out results.json --url http://127.0.0.1:8093/v1

Scoring is deliberately simple and repeatable: "keyword" = the expected answer appears;
"strict" = it appears AND the answer doesn't also claim it can't answer (Gemma 3 1B did both).
Read the answers too: a keyword hit can sit inside a wrong or self-contradicting sentence.
Python 3 standard library only.
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import assistant as am  # noqa: E402
import fallback as fb  # noqa: E402


def _km(a):
    return [float(n.replace(".", "").replace(",", "")) for n in re.findall(r"(\d[\d.,]*)\s*(?:km|kil)", a, re.I)]


# (question, check or None for "judge by reading"). Reference answers are in the benchmarks doc.
QUESTIONS = [
    ("¿Cuál es la capital de Baja California?", lambda a: "mexicali" in a.lower()),
    ("¿Quién escribió Cien años de soledad?", lambda a: re.search(r"garc[ií]a m[aá]rquez", a, re.I)),
    ("¿Aproximadamente qué distancia hay entre Ensenada y Tijuana?", lambda a: _km(a) and 80 <= _km(a)[0] <= 130),
    ("Convert 30 degrees Celsius to Fahrenheit.", lambda a: "86" in a),
    ("How many legs does a spider have?", lambda a: re.search(r"\b(eight|8|ocho)\b", a, re.I)),
    ("What is the largest planet in the solar system?", lambda a: re.search(r"j[uú]piter", a, re.I)),
    ("Dame tres consejos para dormir mejor.", None),
    ("Si tengo 3 manzanas y me como una, ¿cuántas me quedan?", lambda a: re.search(r"\b(2|dos)\b", a, re.I)),
    ("¿Cuánto es 12 por 11?", lambda a: "132" in a),
    ("What is the capital of France?", lambda a: re.search(r"par[ií]s", a, re.I)),
    ("¿Cuántos días tiene una semana?", lambda a: re.search(r"\b(7|siete|seven)\b", a, re.I)),
    ("What is 15 plus 27?", lambda a: "42" in a),
]
REFUSAL = re.compile(r"(do not|don't|cannot|can't|no tengo) (have )?acce|cannot tell you|can't tell you", re.I)


def ask(url, question, temperature, max_tokens, timeout):
    payload = {"model": "local", "stream": False, "max_tokens": max_tokens, "temperature": temperature,
               "messages": [{"role": "system", "content": am.LOCAL_SYSTEM_PROMPT},
                            {"role": "user", "content": question}],
               "chat_template_kwargs": {"enable_thinking": False}}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    raw = data["choices"][0]["message"]["content"].strip()
    return {"q": question, "t": round(time.monotonic() - t0, 2), "tok": data["usage"]["completion_tokens"],
            "raw": raw, "a": fb.plain_text(raw)}


def main():
    cfg = am.DEFAULT_ASSISTANT_CONFIG
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=cfg["fallback_base_url"])
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--temperature", type=float, default=am.LOCAL_TEMPERATURE)
    ap.add_argument("--max-tokens", type=int, default=cfg["fallback_max_output_tokens"])
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--out", help="write all answers and scores as JSON")
    args = ap.parse_args()

    runs = []
    for r in range(args.runs):
        run = [ask(args.url, q, args.temperature, args.max_tokens, args.timeout) for q, _ in QUESTIONS]
        runs.append(run)
        print("run %d: %.1f s for %d questions, avg %.0f tokens, slowest %.1f s" % (
            r + 1, sum(x["t"] for x in run), len(run), statistics.mean(x["tok"] for x in run), max(x["t"] for x in run)))
    print()
    keyword = strict = scored = 0
    for i, (q, check) in enumerate(QUESTIONS):
        answers = [run[i]["a"] for run in runs]
        if check is None:
            mark = "(read)"
        else:
            hits = [bool(check(a)) for a in answers]
            clean = [h and not REFUSAL.search(a) for h, a in zip(hits, answers)]
            keyword, strict, scored = keyword + sum(hits), strict + sum(clean), scored + len(hits)
            mark = "%d/%d" % (sum(clean), len(clean))
        print("%-7s %s" % (mark, q))
        for a in answers:
            print("          %s" % a.replace("\n", " / ")[:160])
    cleaned = sum(x["raw"] != x["a"] for run in runs for x in run)
    print("\nscore: strict %d/%d, keyword %d/%d; answers that needed markdown/LaTeX cleanup: %d"
          % (strict, scored, keyword, scored, cleaned))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"args": vars(args), "runs": runs, "strict": strict, "keyword": keyword, "scored": scored},
                      f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
