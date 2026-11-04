"""Runs tasks against setups in parallel, with a cache, a budget cap and a cost estimate."""
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .costs import PRICES_PER_1K, cost_usd
from .db import Result, Run, find_cached, utcnow
from .judge import judge_output
from .scoring import score_task
from .setups import MODELS, Setup
from .tasks import Task

# Rough token guesses used ONLY for the "about how much will this cost" estimate.
EST_INPUT_TOKENS = 450   # system prompt + question + tool definitions + tool results
EST_OUTPUT_TOKENS = 120
EST_TOOL_MULTIPLIER = 2.0  # tool tasks send the conversation again after each tool call


def make_cache_key(setup: Setup, task: Task, repeat: int, max_output_tokens: int, max_tool_steps: int = 5) -> str:
    # The step limit only matters for tool use tasks (several lookups plus a calculation).
    # Lookup and math tasks need one tool call, so they are not re-run when the limit changes.
    steps = max_tool_steps if (setup.tools_on and task.category in ("tool_use", "hard_tool_use")) else None
    raw = json.dumps([setup.name, setup.model_id, setup.system_prompt, setup.tools_on,
                      task.id, task.prompt, list(task.tools_allowed), repeat, max_output_tokens, steps])
    return hashlib.sha256(raw.encode()).hexdigest()


def estimate_cost(setups: list[Setup], tasks: list[Task], repeats: int, judge_model: str | None = None) -> float:
    total = 0.0
    for s in setups:
        for t in tasks:
            mult = EST_TOOL_MULTIPLIER if (s.tools_on and t.tools_allowed) else 1.0
            total += cost_usd(s.model_key, int(EST_INPUT_TOKENS * mult), int(EST_OUTPUT_TOKENS * mult)) * repeats
            if judge_model:
                total += cost_usd(judge_model, 300, 40) * repeats
    return total


@dataclass
class RunSummary:
    run_id: int
    total_cost_usd: float
    n_results: int
    n_cached: int
    n_errors: int
    stopped_reason: str | None


def _one_job(provider, setup, task, repeat, judge_model, max_out):
    out = provider.generate(setup, task)
    cost = out.cost_usd if out.cost_usd is not None else cost_usd(setup.model_key, out.input_tokens, out.output_tokens)
    score = None
    passed = False
    reason = None
    scored = None if out.error and not out.text else score_task(task, out.text)
    if scored is not None:
        score, passed, reason = scored
    judge = None
    if judge_model and out.text:
        verdict, jreason, jin, jout, jerr = judge_output(provider, MODELS[judge_model][0], task, out.text)
        cost += cost_usd(judge_model, jin, jout)
        judge = (verdict, jreason)
    return out, cost, (score or 0.0), passed, reason, judge


def run_benchmark(
    session_factory,
    provider,
    setups: list[Setup],
    tasks: list[Task],
    *,
    repeats: int = 1,
    mode: str = "full",
    task_set: str = "tasks",
    max_concurrency: int = 4,
    max_output_tokens: int = 400,
    max_tool_steps: int = 8,
    budget_cap_usd: float = 3.0,
    judge_model: str | None = None,
    use_cache: bool = True,
    s3_store=None,
    git_sha: str | None = None,
    notes: str | None = None,
    progress=None,
) -> RunSummary:
    with session_factory() as session:
        run = Run(task_set=task_set, setups=[s.name for s in setups], repeats=repeats, mode=mode,
                  git_sha=git_sha, notes=notes)
        session.add(run)
        session.commit()
        run_id = run.id

    lock = threading.Lock()
    state = {"cost": 0.0, "stop": None, "done": 0, "cached": 0, "errors": 0}
    jobs = [(s, t, r) for s in setups for t in tasks for r in range(repeats)]

    def work(job):
        setup, task, repeat = job
        key = make_cache_key(setup, task, repeat, max_output_tokens, max_tool_steps)
        with lock:
            if state["stop"]:
                return None
        if use_cache:
            with session_factory() as session:
                hit = find_cached(session, key)
                if hit is not None:
                    return ("cache", job, key, hit)
        out, cost, score, passed, reason, judge = _one_job(provider, setup, task, repeat, judge_model, max_output_tokens)
        with lock:
            state["cost"] += cost
            if state["cost"] > budget_cap_usd and not state["stop"]:
                state["stop"] = f"budget cap ${budget_cap_usd:.2f} reached"
        return ("fresh", job, key, (out, cost, score, passed, reason, judge))

    with ThreadPoolExecutor(max_workers=max(1, max_concurrency)) as pool:
        futures = [pool.submit(work, j) for j in jobs]
        for fut in as_completed(futures):
            item = fut.result()
            if item is None:
                continue
            kind, (setup, task, repeat), key, payload = item
            with session_factory() as session:
                if kind == "cache":
                    hit = payload
                    row = Result(run_id=run_id, setup_name=setup.name, task_id=task.id, category=task.category,
                                 repeat_index=repeat, output_text=hit.output_text, tool_calls=hit.tool_calls,
                                 score=hit.score, passed=hit.passed, check_type=task.check_type,
                                 judge_score=hit.judge_score, judge_reason=hit.judge_reason,
                                 input_tokens=hit.input_tokens, output_tokens=hit.output_tokens,
                                 latency_ms=hit.latency_ms, cost_usd=hit.cost_usd, cache_key=key, from_cache=True)
                    state["cached"] += 1
                else:
                    out, cost, score, passed, reason, judge = payload
                    row = Result(run_id=run_id, setup_name=setup.name, task_id=task.id, category=task.category,
                                 repeat_index=repeat, output_text=out.text, tool_calls=out.tool_calls,
                                 score=score, passed=passed, check_type=task.check_type,
                                 judge_score=(float(judge[0]) if judge and judge[0] is not None else None),
                                 judge_reason=(judge[1] if judge else reason),
                                 input_tokens=out.input_tokens, output_tokens=out.output_tokens,
                                 latency_ms=out.latency_ms, cost_usd=cost, error=out.error, cache_key=key)
                    if out.error:
                        state["errors"] += 1
                    if s3_store is not None:
                        row.raw_s3_key = s3_store.save(run_id, setup.name, task.id, repeat,
                                                       {"prompt": task.prompt, "output": out.text, "tool_calls": out.tool_calls})
                session.add(row)
                session.commit()
            state["done"] += 1
            if progress:
                progress(state["done"], len(jobs))

    with session_factory() as session:
        run = session.get(Run, run_id)
        run.finished_at = utcnow()
        run.total_cost_usd = state["cost"]
        run.stopped_reason = state["stop"]
        session.commit()
    return RunSummary(run_id, state["cost"], state["done"], state["cached"], state["errors"], state["stop"])


class S3Store:
    """Saves the raw model output of every call to S3 (optional, off by default)."""

    def __init__(self, bucket: str, region: str, profile: str | None = None):
        import boto3
        session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
        self.client = session.client("s3")
        self.bucket = bucket

    def save(self, run_id, setup, task_id, repeat, payload) -> str | None:
        key = f"gauge/run-{run_id}/{setup}/{task_id}-{repeat}.json"
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(payload).encode(),
                                   ContentType="application/json")
            return key
        except Exception:  # noqa: BLE001 - S3 problems must never fail a benchmark run
            return None
