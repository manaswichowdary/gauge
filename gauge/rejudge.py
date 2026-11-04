"""Re-grade the stored answers of one run with the current judge. Costs a few cents (Nova Lite by default).

Use it after changing judge.py: python -m gauge.rejudge
Only the judge is called. The models being tested are not called again.
"""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select

from .config import load_settings
from .costs import cost_usd
from .db import Result, Run, init_db, make_engine, make_session_factory
from .judge import judge_output
from .providers import BedrockProvider
from .setups import MODELS
from .tasks import load_tasks


def rejudge(factory, provider, judge_key: str, tasks: dict, run_id: int, workers: int = 4) -> dict:
    with factory() as s:
        rows = [(r.id, r.task_id, r.output_text) for r in
                s.execute(select(Result).where(Result.run_id == run_id, Result.output_text != "")).scalars()]
    model_id = MODELS[judge_key][0]
    cache: dict[tuple, tuple] = {}  # identical task and output are judged once

    def work(item):
        rid, task_id, text = item
        key = (task_id, text)
        if key not in cache:
            cache[key] = judge_output(provider, model_id, tasks[task_id], text)
        return rid, cache[key]

    cost = 0.0
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(work, rows))
    with factory() as s:
        for rid, (verdict, reason, tin, tout, err) in results:
            r = s.get(Result, rid)
            if err or verdict is None:
                failed += 1
                continue
            r.judge_score, r.judge_reason = float(verdict), reason
        s.commit()
    for key, (_, _, tin, tout, _) in cache.items():
        cost += cost_usd(judge_key, tin, tout)
    return {"judged": len(results) - failed, "failed": failed, "unique_calls": len(cache), "cost_usd": cost}


def main() -> None:
    settings = load_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    tasks = {t.id: t for t in load_tasks("tasks")}
    with factory() as s:
        run_id = s.execute(select(func.max(Run.id))).scalar_one()
    provider = BedrockProvider(settings.aws_region, settings.aws_profile, 200, 1, settings.request_timeout_s)
    out = rejudge(factory, provider, settings.judge_model, tasks, run_id, settings.max_concurrency)
    print(f"Run {run_id}: judged {out['judged']}, failed {out['failed']}, "
          f"{out['unique_calls']} judge calls, about ${out['cost_usd']:.4f}.")


if __name__ == "__main__":
    main()
