"""Routing eval for the call brain: per-route accuracy, confusion matrix and pass^k.

  python3 route_eval.py ollama:qwen3:4b-instruct-2507-q4_K_M [-k 5]
  python3 route_eval.py openai:tier-1 --base http://127.0.0.1:4000/v1 --key-env JARVIS_ROUTER_KEY
  python3 route_eval.py openai:qwen.qwen3-32b --base https://bedrock-mantle.us-east-1.api.aws/v1 --key-env BEDROCK_MANTLE_KEY

A case passes a run when the model takes the expected path:
  answer        no tool call, and not a question back
  ask           no tool call, and the reply asks a question (for ambiguous requests)
  vault_search / think / hang_up   that tool is called
  delegate:<agent>                 delegate is called with that agent
pass^k: a case counts only if it passes all k runs (temperature 0.7), so flaky routing shows up.
Results are appended to results.jsonl next to this file.
"""
import argparse
import collections
import json
import os
import pathlib
import time
import urllib.request

HERE = pathlib.Path(__file__).parent
SYSTEM = (HERE / "system.md").read_text()
TOOLS = json.loads((HERE / "tools.json").read_text())
CASES = [json.loads(l) for l in (HERE / "routing_cases.jsonl").read_text().splitlines() if l.strip()]


def post(url, body, headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def ask_model(target, base, key, messages, temperature):
    kind, model = target.split(":", 1)
    if kind == "ollama":
        d = post(f"{base or 'http://127.0.0.1:11434'}/api/chat",
                 {"model": model, "stream": False, "think": False, "keep_alive": "10m", "messages": messages,
                  "tools": TOOLS, "options": {"temperature": temperature, "num_predict": 200}}, {})
        m = d["message"]
        calls = [(c["function"]["name"], c["function"].get("arguments") or {}) for c in (m.get("tool_calls") or [])]
        return calls, m.get("content") or ""
    d = post(f"{base}/chat/completions", {"model": model, "messages": messages, "tools": TOOLS,
                                           "temperature": temperature, "max_tokens": 300},
             {"Authorization": f"Bearer {key}"} if key else {})
    m = d["choices"][0]["message"]
    calls = []
    for c in m.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append((c["function"]["name"], args))
    return calls, m.get("content") or ""


def route_of(calls, content):
    """The path the model took, in the same vocabulary as `expect`."""
    if calls:
        name, args = calls[0]
        return f"delegate:{args.get('agent', '?')}" if name == "delegate" else name
    return "ask" if content.strip().endswith("?") else "answer"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--base")
    ap.add_argument("--key-env")
    ap.add_argument("--only", help="comma-separated tags to run")
    a = ap.parse_args()
    key = os.environ.get(a.key_env) if a.key_env else None
    cases = [c for c in CASES if not a.only or set(a.only.split(",")) & set(c["tags"])]

    confusion = collections.Counter()
    per_route = collections.defaultdict(lambda: [0, 0])      # route -> [pass^k count, cases]
    first_run_ok, fails, t0 = 0, [], time.monotonic()
    for c in cases:
        msgs = [{"role": "system", "content": SYSTEM}] + c.get("turns", []) + [{"role": "user", "content": c["text"]}]
        got = []
        for i in range(a.k):
            calls, content = ask_model(a.target, a.base, key, msgs, 0.0 if i == 0 else 0.7)
            got.append(route_of(calls, content))
        ok_all = all(g == c["expect"] for g in got)
        first_run_ok += got[0] == c["expect"]
        per_route[c["expect"]][0] += ok_all
        per_route[c["expect"]][1] += 1
        for g in got:
            confusion[(c["expect"], g)] += 1
        if not ok_all:
            fails.append(f"  ✗ {c['id']} {c['text'][:58]!r} want {c['expect']}, got {got}")

    n = len(cases)
    passk = sum(v[0] for v in per_route.values())
    print(f"{a.target}  cases {n}  k={a.k}  first-run {first_run_ok}/{n} ({first_run_ok/n:.0%})  "
          f"pass^{a.k} {passk}/{n} ({passk/n:.0%})  {time.monotonic()-t0:.0f}s")
    for route, (p, t) in sorted(per_route.items()):
        print(f"  {route:18} pass^{a.k} {p}/{t}")
    wrong = [(k, v) for k, v in confusion.items() if k[0] != k[1]]
    if wrong:
        print("  confusions (want → got: runs):", ", ".join(f"{w}→{g}: {v}" for (w, g), v in sorted(wrong, key=lambda x: -x[1])))
    print("\n".join(fails))
    with (HERE / "results.jsonl").open("a") as f:
        f.write(json.dumps({"when": time.strftime("%Y-%m-%d %H:%M"), "target": a.target, "k": a.k, "cases": n,
                            "first_run": first_run_ok, "pass_k": passk,
                            "per_route": {r: v for r, v in per_route.items()}}) + "\n")


if __name__ == "__main__":
    main()
