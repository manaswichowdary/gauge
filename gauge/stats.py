"""Numbers computed from stored results: accuracy, latency, cost, judge agreement."""
import statistics

from sqlalchemy import select

from .db import Label, Result

MODEL_FAILURE_ERRORS = ("hit max tool steps",)
# Bedrock rejects the whole call when the model invents a tool name that is not valid. That is the model's
# mistake, so it counts as a wrong answer. Throttling and outages do not contain this text.
MODEL_FAILURE_MARKERS = ("toolUse.name",)


def is_infra_error(r: Result) -> bool:
    """True when the call never got a real answer (throttling, outage). These are not the model's fault."""
    if not r.error:
        return False
    return not (r.error.startswith(MODEL_FAILURE_ERRORS) or any(m in r.error for m in MODEL_FAILURE_MARKERS))


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def summarize(results: list[Result]) -> list[dict]:
    """One row per setup.

    Accuracy only counts calls that reached the model. Calls lost to throttling are counted
    separately in 'errors'. Cached rows keep the cost and latency of the call they were copied from,
    so a setup looks the same whether its answers were fresh or reused.
    """
    by_setup: dict[str, list[Result]] = {}
    for r in results:
        by_setup.setdefault(r.setup_name, []).append(r)
    rows = []
    for name, rs in sorted(by_setup.items()):
        valid = [r for r in rs if not is_infra_error(r)]
        lat = [r.latency_ms for r in rs if not r.error]
        cost = sum(r.cost_usd for r in rs)
        rows.append({
            "setup": name,
            "n": len(rs),
            "valid": len(valid),
            "accuracy": (sum(1 for r in valid if r.passed) / len(valid)) if valid else 0.0,
            "p50_ms": percentile(lat, 50),
            "p95_ms": percentile(lat, 95),
            "cost_usd": cost,
            "cost_per_100": (cost / len(rs) * 100) if rs else 0.0,
            "errors": len(rs) - len(valid),
        })
    return rows


def by_category(results: list[Result]) -> dict[str, dict[str, float]]:
    table: dict[str, dict[str, list[int]]] = {}
    for r in results:
        if is_infra_error(r):
            continue
        table.setdefault(r.setup_name, {}).setdefault(r.category, []).append(1 if r.passed else 0)
    return {s: {c: sum(v) / len(v) for c, v in cats.items()} for s, cats in table.items()}


def flaky_tasks(results: list[Result]) -> list[tuple[str, str]]:
    """Setup/task pairs where repeats disagreed (some passed, some failed)."""
    groups: dict[tuple[str, str], set[bool]] = {}
    for r in results:
        if is_infra_error(r):
            continue
        groups.setdefault((r.setup_name, r.task_id), set()).add(bool(r.passed))
    return sorted(k for k, v in groups.items() if len(v) > 1)


def cohens_kappa(a: list[int], b: list[int]) -> float:
    """Agreement between two raters, corrected for chance. 1 is perfect, 0 is no better than chance."""
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    observed = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1, pb1 = sum(a) / n, sum(b) / n
    expected = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if expected == 1:
        return 1.0
    return (observed - expected) / (1 - expected)


def judge_agreement(session) -> dict:
    """Compare the judge and the rule scorer against human labels."""
    rows = session.execute(
        select(Label.human_score, Result.judge_score, Result.passed).join(Result, Result.id == Label.result_id)
    ).all()
    rows = [r for r in rows if r.judge_score is not None]
    if not rows:
        return {"n": 0}
    human = [int(r.human_score) for r in rows]
    judge = [int(r.judge_score) for r in rows]
    rule = [1 if r.passed else 0 for r in rows]
    return {
        "n": len(rows),
        "judge_percent_agreement": 100 * sum(h == j for h, j in zip(human, judge)) / len(rows),
        "judge_kappa": cohens_kappa(human, judge),
        "rule_percent_agreement": 100 * sum(h == r for h, r in zip(human, rule)) / len(rows),
        "rule_kappa": cohens_kappa(human, rule),
    }


def mean_or_zero(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0
