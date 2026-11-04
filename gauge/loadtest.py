"""Load test: send many requests at once and see where speed and reliability start to degrade.

For each concurrency level it sends the same number of requests, then reports p50 and p95 latency, throughput
(answers per second) and how often the service pushed back (throttling retries). It uses the real provider, so it
costs real money on Bedrock. Keep the request count small.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .costs import cost_usd
from .stats import percentile

KNEE_FACTOR = 2.0  # a level "degrades" when its p95 (including retry waits) is this many times the first level's


def _one(provider, setup, task):
    out = provider.generate(setup, task)
    return out, (out.cost_usd if out.cost_usd is not None else cost_usd(setup.model_key, out.input_tokens, out.output_tokens))


def run_load_test(provider, setup, tasks, *, levels=(1, 4, 8, 16), requests_per_level: int = 24,
                  progress=None) -> dict:
    jobs = [tasks[i % len(tasks)] for i in range(requests_per_level)]
    rows, total_cost = [], 0.0
    for level in levels:
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=level) as pool:
            outs = list(pool.map(lambda t: _one(provider, setup, t), jobs))
        wall = time.perf_counter() - start
        good = [o for o, _ in outs if not o.error]
        total_cost += sum(c for _, c in outs)
        model_ms = [o.latency_ms for o in good]
        user_ms = [o.latency_ms + o.wait_ms for o in good]  # what a caller waited, retries included
        rows.append({
            "concurrency": level,
            "requests": len(outs),
            "ok": len(good),
            "errors": len(outs) - len(good),
            "retries": sum(o.retries for o, _ in outs),
            "p50_ms": round(percentile(model_ms, 50)),
            "p95_ms": round(percentile(model_ms, 95)),
            "p95_with_retries_ms": round(percentile(user_ms, 95)),
            "throughput_per_s": round(len(good) / wall, 2) if wall > 0 else 0.0,
            "wall_s": round(wall, 1),
        })
        if progress:
            progress(rows[-1])
    return {
        "setup": setup.name,
        "model_id": setup.model_id,
        "requests_per_level": requests_per_level,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "levels": rows,
        "first_degraded_level": first_degraded(rows),
        "cost_usd": round(total_cost, 4),
    }


def first_degraded(rows: list[dict]):
    """The first concurrency level where callers got errors, retries or a much slower p95. None if it held up."""
    if not rows:
        return None
    base = rows[0]["p95_with_retries_ms"] or 1
    for r in rows[1:]:
        if r["errors"] or r["retries"] or r["p95_with_retries_ms"] > KNEE_FACTOR * base:
            return r["concurrency"]
    return None


def to_markdown(data: dict) -> str:
    lines = [f"Load test: {data['setup']}, {data['requests_per_level']} requests per level, cost ${data['cost_usd']:.4f}", "",
             "| Concurrency | OK | Errors | Retries | p50 ms | p95 ms | p95 with retries ms | Answers per second |",
             "|---|---|---|---|---|---|---|---|"]
    for r in data["levels"]:
        lines.append(f"| {r['concurrency']} | {r['ok']} | {r['errors']} | {r['retries']} | {r['p50_ms']} | "
                     f"{r['p95_ms']} | {r['p95_with_retries_ms']} | {r['throughput_per_s']} |")
    knee = data["first_degraded_level"]
    lines += ["", f"First level that degraded: {knee}" if knee else "No level degraded in this test."]
    return "\n".join(lines)
