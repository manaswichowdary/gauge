"""Save one run to a small JSON file and load it back, so the dashboard can be shown without a database.

The snapshot holds scored answers and numbers only. It has no keys, no prompts and no raw tool data,
so it is safe to commit and deploy.
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from .db import Result, Run
from .stats import judge_agreement

OUTPUT_LIMIT = 300  # characters of each answer kept in the snapshot
FIELDS = ("setup_name", "task_id", "category", "repeat_index", "passed", "judge_score",
          "latency_ms", "cost_usd", "error")


def export_snapshot(session, run_id: int | None, path: str | Path) -> dict:
    """Write one run (default: the latest) and the judge trust numbers to a JSON file."""
    stmt = select(Run).where(Run.id == run_id) if run_id else select(Run).order_by(Run.id.desc()).limit(1)
    run = session.execute(stmt).scalar_one_or_none()
    if run is None:
        raise ValueError("No runs to export. Run `make run` first.")
    rows = list(session.execute(select(Result).where(Result.run_id == run.id)).scalars())
    data = {
        "run": {"id": run.id, "mode": run.mode, "repeats": run.repeats, "started_at": run.started_at.isoformat(),
                "total_cost_usd": run.total_cost_usd, "notes": run.notes, "stopped_reason": run.stopped_reason},
        "judge_trust": judge_agreement(session),
        "results": [{**{f: getattr(r, f) for f in FIELDS}, "output_text": (r.output_text or "")[:OUTPUT_LIMIT]}
                    for r in rows],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=1))
    return data


def load_snapshot(path: str | Path):
    """Read a snapshot. Returns (run, results, judge_trust). Rows have the same fields as the database rows."""
    data = json.loads(Path(path).read_text())
    run = SimpleNamespace(**data["run"])
    run.started_at = datetime.fromisoformat(run.started_at)
    results = [SimpleNamespace(**row) for row in data["results"]]
    return run, results, data["judge_trust"]
