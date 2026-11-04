"""Re-score stored outputs with the current scorers. No model calls, so it costs nothing.

Use it after changing scoring.py: python -m gauge.rescore
"""
from sqlalchemy import select

from .config import load_settings
from .db import Result, init_db, make_engine, make_session_factory
from .scoring import score_task
from .tasks import load_tasks


def rescore(session, tasks: dict) -> tuple[int, int]:
    changed = total = 0
    for r in session.execute(select(Result)).scalars():
        task = tasks.get(r.task_id)
        if task is None or r.error and not r.output_text:
            continue
        scored = score_task(task, r.output_text)
        if scored is None:
            continue
        score, passed, _ = scored
        total += 1
        if bool(r.passed) != passed:
            changed += 1
        r.score, r.passed = score, passed
    session.commit()
    return changed, total


def repair_cached_costs(session) -> int:
    """Old cached rows were saved with cost 0. Copy the cost from the call they were copied from."""
    fixed = 0
    for r in session.execute(select(Result).where(Result.from_cache.is_(True), Result.cost_usd == 0)).scalars():
        original = session.execute(
            select(Result).where(Result.cache_key == r.cache_key, Result.from_cache.is_(False), Result.error.is_(None))
            .order_by(Result.id.asc()).limit(1)
        ).scalar_one_or_none()
        if original is not None and original.cost_usd:
            r.cost_usd = original.cost_usd
            fixed += 1
    session.commit()
    return fixed


def main() -> None:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    tasks = {t.id: t for t in load_tasks("tasks")}
    with make_session_factory(engine)() as session:
        changed, total = rescore(session, tasks)
        fixed = repair_cached_costs(session)
    print(f"Re-scored {total} results. {changed} changed. Repaired cost on {fixed} cached rows.")


if __name__ == "__main__":
    main()
