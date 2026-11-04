"""Seeded regression demo: break one setup on purpose and check that the gate catches it.

Steps: run the setup as it is (the healthy baseline), run it again with a deliberately bad system prompt
(same setup name), then run the normal gate check on the second run. The time is measured from the start of
the second run to the gate verdict, so it is the time a check takes to catch the problem once it starts. How soon
a check starts after a bad change depends on when it runs (on every change to prompts, or on a schedule).
"""
import time
from dataclasses import replace

from sqlalchemy import select

from .db import Result
from .gate import check_against
from .runner import run_benchmark
from .setups import DEGRADED_PROMPT, Setup
from .stats import summarize


def _results(factory, run_id: int) -> list:
    with factory() as s:
        rows = list(s.execute(select(Result).where(Result.run_id == run_id)).scalars())
        s.expunge_all()
    return rows


def _accuracy(results, name: str) -> float:
    return next(r["accuracy"] for r in summarize(results) if r["setup"] == name)


def run_regression_demo(factory, provider, setup: Setup, tasks, *, repeats: int = 1, max_drop: float = 0.05,
                        clock=time.perf_counter, **run_kwargs) -> dict:
    """Returns a dict with both accuracies, the drop, whether the gate caught it, and how long that took."""
    healthy = run_benchmark(factory, provider, [setup], tasks, repeats=repeats, mode="regress",
                            notes="seeded regression: healthy baseline", **run_kwargs)
    baseline = {setup.name: _accuracy(_results(factory, healthy.run_id), setup.name)}

    broken = replace(setup, system_prompt=DEGRADED_PROMPT)  # same name, so the gate compares like with like
    start = clock()
    degraded = run_benchmark(factory, provider, [broken], tasks, repeats=repeats, mode="regress", use_cache=False,
                             notes="seeded regression: degraded prompt", **run_kwargs)
    results = _results(factory, degraded.run_id)
    ok, messages = check_against(results, baseline, max_drop)
    seconds = clock() - start

    regressed = _accuracy(results, setup.name)
    return {
        "setup": setup.name,
        "tasks": len(tasks),
        "repeats": repeats,
        "baseline_accuracy": round(baseline[setup.name], 4),
        "regressed_accuracy": round(regressed, 4),
        "drop_points": round((baseline[setup.name] - regressed) * 100, 1),
        "limit_points": round(max_drop * 100, 1),
        "caught": not ok,
        "seconds_to_detect": round(seconds, 1),
        "cost_usd": round(healthy.total_cost_usd + degraded.total_cost_usd, 4),
        "healthy_run_id": healthy.run_id,
        "degraded_run_id": degraded.run_id,
        "gate_messages": messages,
        "degraded_prompt": DEGRADED_PROMPT,
    }
