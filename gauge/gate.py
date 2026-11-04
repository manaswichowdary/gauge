"""CI gate: compare accuracy against a saved baseline and fail on a big drop."""
import json
from pathlib import Path

from .stats import summarize

DEFAULT_MAX_DROP = 0.05  # 5 accuracy points


def save_baseline(results, path: str | Path) -> dict:
    data = {r["setup"]: round(r["accuracy"], 4) for r in summarize(results)}
    Path(path).write_text(json.dumps(data, indent=2, sort_keys=True))
    return data


def check_gate(results, baseline_path: str | Path, max_drop: float = DEFAULT_MAX_DROP):
    """Returns (ok, messages). A setup fails if accuracy fell by more than max_drop."""
    return check_against(results, json.loads(Path(baseline_path).read_text()), max_drop)


def check_against(results, baseline: dict, max_drop: float = DEFAULT_MAX_DROP):
    """Same check, with the baseline given as a dict of setup name to accuracy."""
    current = {r["setup"]: r["accuracy"] for r in summarize(results)}
    messages, ok = [], True
    for setup, base in baseline.items():
        if setup not in current:
            continue
        drop = base - current[setup]
        if drop > max_drop + 1e-9:
            ok = False
            messages.append(f"FAIL {setup}: {base:.0%} -> {current[setup]:.0%} (dropped {drop * 100:.1f} points, limit {max_drop * 100:.0f})")
        else:
            messages.append(f"ok   {setup}: {base:.0%} -> {current[setup]:.0%}")
    if not messages:
        ok = False
        messages.append("FAIL no setups in common with the baseline")
    return ok, messages
