"""Builds the Markdown report for one run."""
from .stats import by_category, flaky_tasks, summarize


def build_report(run, results) -> str:
    rows = summarize(results)
    cats = by_category(results)
    lines = [
        f"# Gauge report, run {run.id}", "",
        f"Mode: {run.mode}. Repeats: {run.repeats}. Tasks per setup: {len({r.task_id for r in results})}. "
        f"Total cost: ${run.total_cost_usd:.4f}.",
    ]
    lines.append(_run_time(run, results))
    if run.stopped_reason:
        lines.append(f"**Run stopped early: {run.stopped_reason}.**")
    if run.notes:
        lines.append(f"Note: {run.notes}")
    lines += ["", "## Results by setup", "",
              "| Setup | Accuracy | Answers counted | p50 ms | p95 ms | Cost per 100 tasks | Lost to errors |",
              "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['setup']} | {r['accuracy']:.0%} | {r['valid']} of {r['n']} | {r['p50_ms']:.0f} | "
                     f"{r['p95_ms']:.0f} | ${r['cost_per_100']:.4f} | {r['errors']} |")
    all_cats = sorted({c for v in cats.values() for c in v})
    lines += ["", "## Accuracy by category", "", "| Setup | " + " | ".join(all_cats) + " |",
              "|---|" + "---|" * len(all_cats)]
    for setup, v in sorted(cats.items()):
        lines.append(f"| {setup} | " + " | ".join(f"{v.get(c, 0):.0%}" for c in all_cats) + " |")
    flaky = flaky_tasks(results)
    if flaky:
        lines += ["", "## Unstable tasks (repeats disagreed)", ""] + [f"- {s}: {t}" for s, t in flaky]
    return "\n".join(lines) + "\n"


def _run_time(run, results) -> str:
    """How long the run took. Answers reused from the cache make a run look faster than a fresh one, so say so."""
    if not (getattr(run, "finished_at", None) and getattr(run, "started_at", None)):
        return "Run time: not recorded."
    seconds = (run.finished_at - run.started_at).total_seconds()
    text = f"Run time: {int(seconds // 60)} min {int(seconds % 60)} s for {len(results)} answers."
    cached = sum(1 for r in results if getattr(r, "from_cache", False))
    if cached:
        text += f" {cached} of them came from the cache, so this is not the time of a fresh run."
    return text
