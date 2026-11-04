"""Command line entry point: python -m gauge.cli <command>."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from .config import ConfigError, load_settings
from .db import Run, Result, init_db, make_engine, make_session_factory
from .gate import check_gate, save_baseline
from .aiperf_run import run_aiperf
from .loadtest import run_load_test, to_markdown
from .providers import BedrockProvider, FakeProvider, OpenAICompatProvider, RoutingProvider, SageMakerProvider
from .regress import run_regression_demo
from .report import build_report
from .runner import S3Store, estimate_cost, run_benchmark
from .sagemaker_ops import delete_endpoint_everything, deploy_endpoint, list_endpoints, new_endpoint_name, with_endpoint
from .setups import CHEAPEST, SAGEMAKER_SETUP, pick_setups
from .snapshot import export_snapshot
from .tasks import load_tasks, spread_sample

DEV_TASKS = 8
CI_TASKS = 12
BASELINE = Path("baseline.json")
SNAPSHOT = Path("snapshot/run.json")
RESULTS_DIR = Path("results")
REGRESSION_FILE = Path("snapshot/regression.json")
LOAD_FILE = Path("snapshot/load.json")
AIPERF_FILE = Path("snapshot/aiperf.json")


def make_provider(kind: str, settings):
    """fake: offline. bedrock: routes each setup to Bedrock or a local server, creating each only when needed."""
    if kind == "fake":
        return FakeProvider(max_output_tokens=settings.max_output_tokens)
    return RoutingProvider({
        "bedrock": lambda: BedrockProvider(settings.aws_region, settings.aws_profile, settings.max_output_tokens,
                                           settings.max_tool_steps, settings.request_timeout_s),
        "local": lambda: OpenAICompatProvider(settings.local_base_url, settings.max_output_tokens,
                                              settings.max_tool_steps, settings.request_timeout_s),
        "sagemaker": lambda: _sagemaker_provider(settings, settings.sagemaker_endpoint),
    })


def _sagemaker_provider(settings, endpoint):
    if not endpoint:
        raise RuntimeError("No SageMaker endpoint. Use `make sagemaker-run`, which creates one, or set SAGEMAKER_ENDPOINT_NAME.")
    return SageMakerProvider(settings.aws_region, settings.aws_profile, endpoint, settings.sagemaker_hourly_usd,
                             settings.max_output_tokens, settings.max_tool_steps, settings.request_timeout_s)


def sagemaker_client(settings):
    import boto3
    session = boto3.Session(profile_name=settings.aws_profile, region_name=settings.aws_region) if settings.aws_profile \
        else boto3.Session(region_name=settings.aws_region)
    return session.client("sagemaker")


def confirm_cost(setups, provider_kind: str, uses_judge: bool, yes: bool) -> bool:
    """Ask before spending money. Local only runs (and fake runs) spend nothing, so they never ask."""
    spends = provider_kind != "fake" and (uses_judge or any(s.provider == "bedrock" for s in setups))
    if provider_kind == "fake":
        print("FAKE provider: nothing is sent to AWS. Results are made up and mean nothing.")
        return True
    if not spends:
        print("Local models only: no AWS calls and no cost.")
        return True
    if yes:
        return True
    if input("This will call Bedrock and cost real money. Type yes to continue: ").strip().lower() != "yes":
        print("Cancelled.")
        return False
    return True


def git_sha() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def cmd_run(args, settings) -> int:
    tasks = load_tasks(args.tasks)
    if args.mode == "dev":
        setups = pick_setups(args.setups or CHEAPEST)
        tasks = spread_sample(tasks, DEV_TASKS)
        repeats = 1
    elif args.mode == "ci":
        setups = pick_setups(args.setups or CHEAPEST)
        tasks = spread_sample(tasks, CI_TASKS)
        repeats = 1
    elif args.mode == "scheduled":
        # Cheapest models on every task, once. About a cent. Big enough that one flipped answer is under the gate limit.
        setups = pick_setups(args.setups or CHEAPEST)
        repeats = 1
    else:
        setups = pick_setups(args.setups)
        repeats = args.repeats
    estimate = estimate_cost(setups, tasks, repeats, settings.judge_model if args.judge else None)
    calls = len(setups) * len(tasks) * repeats
    print(f"Mode: {args.mode}. {calls} model calls ({len(setups)} setups x {len(tasks)} tasks x {repeats} repeats).")
    print(f"Estimated cost: about ${estimate:.2f}. Budget cap: ${settings.budget_cap_usd:.2f}.")
    if not confirm_cost(setups, args.provider, bool(args.judge), args.yes):
        return 1
    provider = make_provider(args.provider, settings)
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    s3 = S3Store(settings.s3_bucket, settings.aws_region, settings.aws_profile) if settings.save_raw_to_s3 else None
    summary = run_benchmark(
        factory, provider, setups, tasks, repeats=repeats, mode=args.mode, task_set=str(args.tasks),
        max_concurrency=settings.max_concurrency, max_output_tokens=settings.max_output_tokens,
        max_tool_steps=settings.max_tool_steps,
        budget_cap_usd=settings.budget_cap_usd, judge_model=settings.judge_model if args.judge else None,
        use_cache=not args.no_cache, s3_store=s3, git_sha=git_sha(),
        notes="FAKE PROVIDER, results are not real" if args.provider == "fake" else None,
        progress=lambda d, n: print(f"\r{d}/{n}", end="", flush=True),
    )
    print(f"\nRun {summary.run_id} done. Cost ${summary.total_cost_usd:.4f}. "
          f"{summary.n_cached} from cache, {summary.n_errors} errors.")
    if summary.stopped_reason:
        print(f"Stopped early: {summary.stopped_reason}")
    return 0


def _load_run(settings, run_id, mode=None):
    """The run with this id, or the latest run (of the given mode, if one is named)."""
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        stmt = select(Run).order_by(Run.id.desc())
        if mode:
            stmt = stmt.where(Run.mode == mode)
        run = s.get(Run, run_id) if run_id else s.execute(stmt.limit(1)).scalar_one_or_none()
        if run is None:
            return None, []
        results = list(s.execute(select(Result).where(Result.run_id == run.id)).scalars())
        s.expunge_all()
    return run, results


def cmd_report(args, settings) -> int:
    run, results = _load_run(settings, args.run_id)
    if run is None:
        print("No runs yet. Run `make run-dev` first.")
        return 1
    text = build_report(run, results)
    out = Path("reports")
    out.mkdir(exist_ok=True)
    path = out / f"run-{run.id}.md"
    path.write_text(text)
    print(text)
    print(f"Saved to {path}")
    return 0


def cmd_export(args, settings) -> int:
    # Default to the latest full run. The seeded regression, scheduled and dev runs are not what the demo should show.
    run, _ = _load_run(settings, args.run_id, args.mode)
    if run is None:
        print(f"No {args.mode} run to export. Run `make run` first, or pass --run-id.")
        return 1
    engine = make_engine(settings.database_url)
    init_db(engine)
    with make_session_factory(engine)() as s:
        try:
            data = export_snapshot(s, run.id, SNAPSHOT)
        except ValueError as exc:
            print(exc)
            return 1
    print(f"Saved run {data['run']['id']} ({len(data['results'])} answers) to {SNAPSHOT}")
    # A Markdown summary of the same run, small enough to commit next to the code (results/run-N.md).
    run, results = _load_run(settings, data["run"]["id"])
    RESULTS_DIR.mkdir(exist_ok=True)
    summary_path = RESULTS_DIR / f"run-{run.id}.md"
    summary_path.write_text(build_report(run, results))
    print(f"Saved the summary to {summary_path}")
    return 0


def cmd_gate(args, settings) -> int:
    run, results = _load_run(settings, args.run_id, args.mode)
    if run is None:
        print(f"No {args.mode} run to check. Run it first, or pass --run-id.")
        return 1
    baseline = Path(args.baseline)
    if args.save_baseline:
        data = save_baseline(results, baseline)
        print(f"Saved baseline to {baseline}: {data}")
        return 0
    if not baseline.exists():
        print(f"No {baseline} yet. Run the matching baseline command after a good run.")
        return 1
    ok, messages = check_gate(results, baseline, args.max_drop)
    print("\n".join(messages))
    return 0 if ok else 1


def cmd_regress(args, settings) -> int:
    setups = pick_setups(args.setup)
    tasks = load_tasks(args.tasks)
    if args.tasks_limit:
        tasks = spread_sample(tasks, args.tasks_limit)
    estimate = estimate_cost(setups, tasks, args.repeats) * 2
    print(f"Seeded regression on {setups[0].name}: {len(tasks)} tasks x {args.repeats} repeats, healthy run then a "
          f"deliberately broken prompt. Estimated cost: up to ${estimate:.2f}.")
    if not confirm_cost(setups, args.provider, False, args.yes):
        return 1
    engine = make_engine(settings.database_url)
    init_db(engine)
    data = run_regression_demo(
        make_session_factory(engine), make_provider(args.provider, settings), setups[0], tasks,
        repeats=args.repeats, max_drop=args.max_drop, max_concurrency=settings.max_concurrency,
        max_output_tokens=settings.max_output_tokens, max_tool_steps=settings.max_tool_steps,
        budget_cap_usd=settings.budget_cap_usd, git_sha=git_sha(), task_set=str(args.tasks))
    if args.provider == "fake":
        data["fake"] = True
        print("FAKE provider: these numbers are made up.")
    REGRESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGRESSION_FILE.write_text(json.dumps(data, indent=1))
    print(f"Healthy {data['baseline_accuracy']:.0%} -> broken prompt {data['regressed_accuracy']:.0%} "
          f"(dropped {data['drop_points']} points, limit {data['limit_points']}).")
    print("\n".join(data["gate_messages"]))
    print(f"Gate {'CAUGHT the regression' if data['caught'] else 'did NOT catch the regression'} "
          f"in {data['seconds_to_detect']} seconds. Cost ${data['cost_usd']:.4f}. Saved to {REGRESSION_FILE}")
    return 0 if data["caught"] else 1


def cmd_load(args, settings) -> int:
    setups = pick_setups(args.setup)
    tasks = spread_sample(load_tasks(args.tasks), 12)
    levels = [int(x) for x in args.levels.split(",")]
    calls = len(levels) * args.requests
    print(f"Load test on {setups[0].name}: {calls} requests ({args.requests} at each of concurrency {levels}). "
          f"Throttled calls wait and retry, so a heavily throttled account can take several minutes.")
    if not confirm_cost(setups, args.provider, False, args.yes):
        return 1
    data = run_load_test(make_provider(args.provider, settings), setups[0], tasks, levels=levels,
                         requests_per_level=args.requests,
                         progress=lambda r: print(f"  concurrency {r['concurrency']}: {r['ok']} ok, {r['errors']} errors, "
                                                  f"{r['retries']} retries, p95 {r['p95_with_retries_ms']} ms"))
    if args.provider == "fake":
        data["fake"] = True
    LOAD_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOAD_FILE.write_text(json.dumps(data, indent=1))
    print()
    print(to_markdown(data))
    print(f"Saved to {LOAD_FILE}")
    return 0


def cmd_aiperf(args, settings) -> int:
    setup = pick_setups(args.setup)[0]
    if setup.provider != "local":
        print("AIPerf talks to an OpenAI style server, so use a local setup such as ollama-qwen2.5.")
        return 1
    print(f"AIPerf on {setup.name} at {settings.local_base_url}: concurrency {args.levels}, {args.requests} requests each. "
          f"No AWS calls and no cost. Make sure the model is pulled and `ollama serve` is running.")
    try:
        data = run_aiperf(setup, settings.local_base_url, levels=[int(x) for x in args.levels.split(",")],
                          requests_per_level=args.requests)
    except RuntimeError as exc:
        print(exc)
        return 1
    AIPERF_FILE.parent.mkdir(parents=True, exist_ok=True)
    AIPERF_FILE.write_text(json.dumps(data, indent=1))
    failed = [r["concurrency"] for r in data["levels"] if r["returncode"] != 0]
    print(f"Saved to {AIPERF_FILE}." + (f" AIPerf failed at concurrency {failed}. Scroll up for its error." if failed else ""))
    return 1 if failed else 0


def cmd_sagemaker_status(args, settings) -> int:
    rows = list_endpoints(sagemaker_client(settings))
    if not rows:
        print("No SageMaker endpoints in this account and region. Nothing is billing.")
        return 0
    for r in rows:
        print(f"{r['name']}: {r['status']}")
    print("Anything listed here is billing by the hour. Delete with: make sagemaker-down NAME=<name>")
    return 0


def cmd_sagemaker_down(args, settings) -> int:
    print("Deleted: " + (", ".join(delete_endpoint_everything(sagemaker_client(settings), args.name)) or "nothing found"))
    return 0


def cmd_sagemaker_run(args, settings) -> int:
    if not settings.sagemaker_role_arn:
        print("Set SAGEMAKER_ROLE_ARN in .env (see docs/sagemaker.md, step 2).")
        return 1
    if settings.sagemaker_hourly_usd <= 0:
        print("Set SAGEMAKER_HOURLY_USD in .env to the hourly price of "
              f"{settings.sagemaker_instance_type} (look it up on the SageMaker pricing page). Costs are reported from it.")
        return 1
    tasks = load_tasks(args.tasks)
    if args.tasks_limit:
        tasks = spread_sample(tasks, args.tasks_limit)
    low, high = settings.sagemaker_hourly_usd * 0.5, settings.sagemaker_hourly_usd * 1.0
    print(f"This starts one {settings.sagemaker_instance_type} endpoint (about 10 to 15 minutes), runs {len(tasks)} tasks "
          f"x {args.repeats} repeat, then deletes everything. Expect about 30 to 60 minutes of instance time: roughly "
          f"${low:.2f} to ${high:.2f} at ${settings.sagemaker_hourly_usd}/hour. The endpoint is deleted even if something fails.")
    if not args.yes and input("Type yes to continue: ").strip().lower() != "yes":
        print("Cancelled.")
        return 1
    client = sagemaker_client(settings)
    engine = make_engine(settings.database_url)
    init_db(engine)
    name = new_endpoint_name()
    deploy = lambda n: deploy_endpoint(n, settings.sagemaker_role_arn, settings.sagemaker_instance_type,  # noqa: E731
                                       settings.aws_region, settings.aws_profile)
    code = 0
    try:
        with with_endpoint(deploy, lambda n: print("Cleaning up: " + ", ".join(delete_endpoint_everything(client, n))), name):
            provider = _sagemaker_provider(settings, name)
            smoke = provider.generate(SAGEMAKER_SETUP, tasks[0])
            if smoke.error or not smoke.text:
                print(f"Smoke test failed, stopping: {smoke.error or 'empty answer'}")
                code = 1
            else:
                summary = run_benchmark(
                    make_session_factory(engine), provider, [SAGEMAKER_SETUP], tasks, repeats=args.repeats, mode="full",
                    task_set=str(args.tasks), max_concurrency=1,  # one at a time, so the hourly charge per call is exact
                    max_output_tokens=settings.max_output_tokens, max_tool_steps=settings.max_tool_steps,
                    budget_cap_usd=settings.budget_cap_usd, git_sha=git_sha(), use_cache=False,
                    notes=f"SageMaker endpoint, {settings.sagemaker_instance_type} at ${settings.sagemaker_hourly_usd}/hour",
                    progress=lambda d, n: print(f"\r{d}/{n}", end="", flush=True))
                print(f"\nRun {summary.run_id} done. Compute cost for the calls ${summary.total_cost_usd:.4f} "
                      f"(startup time is not included). {summary.n_errors} errors.")
    except KeyboardInterrupt:
        print("\nInterrupted. The endpoint was deleted.")
        code = 1
    left = [e for e in list_endpoints(client) if e["name"] == name]
    print("Check: the endpoint is gone." if not left else f"WARNING: {name} still exists. Run: make sagemaker-down NAME={name}")
    return code or (1 if left else 0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gauge")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["full", "dev", "ci", "scheduled"], default="dev")
    r.add_argument("--tasks", default="tasks")
    r.add_argument("--setups", default=None,
                   help="comma list, default all Bedrock setups (full) or the cheapest (dev, ci, scheduled). "
                        "Local models are chosen by name, for example ollama-llama3.1")
    r.add_argument("--repeats", type=int, default=2)
    r.add_argument("--provider", choices=["bedrock", "fake"], default="bedrock")
    r.add_argument("--judge", action="store_true", help="also grade every output with the LLM judge")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    rep = sub.add_parser("report")
    rep.add_argument("--run-id", type=int, default=None)
    ex = sub.add_parser("export", help="save a run to snapshot/run.json for the read only dashboard")
    ex.add_argument("--run-id", type=int, default=None)
    ex.add_argument("--mode", default="full", help="latest run of this mode (default: full)")
    g = sub.add_parser("gate")
    g.add_argument("--run-id", type=int, default=None)
    g.add_argument("--save-baseline", action="store_true")
    g.add_argument("--max-drop", type=float, default=0.05)
    g.add_argument("--baseline", default=str(BASELINE), help="baseline file (baseline-ci.json for scheduled runs)")
    g.add_argument("--mode", default="full", help="check the latest run of this mode (full, or scheduled for the weekly job)")
    rg = sub.add_parser("regress", help="break a setup on purpose and check that the gate catches it")
    rg.add_argument("--setup", default="nova-lite")
    rg.add_argument("--tasks", default="tasks")
    rg.add_argument("--tasks-limit", type=int, default=None, help="use a spread sample of this many tasks")
    rg.add_argument("--repeats", type=int, default=1)
    rg.add_argument("--max-drop", type=float, default=0.05)
    rg.add_argument("--provider", choices=["bedrock", "fake"], default="bedrock")
    rg.add_argument("--yes", action="store_true")
    ap = sub.add_parser("aiperf", help="run NVIDIA AIPerf against a local model (free)")
    ap.add_argument("--setup", default="ollama-qwen2.5")
    ap.add_argument("--levels", default="1,2,4")
    ap.add_argument("--requests", type=int, default=20)
    sub.add_parser("sagemaker-status", help="list SageMaker endpoints (anything listed is billing)")
    sd = sub.add_parser("sagemaker-down", help="delete a SageMaker endpoint, its config and its model")
    sd.add_argument("--name", required=True)
    sr = sub.add_parser("sagemaker-run", help="start an endpoint, benchmark it, then delete it")
    sr.add_argument("--tasks", default="tasks")
    sr.add_argument("--tasks-limit", type=int, default=None)
    sr.add_argument("--repeats", type=int, default=1)
    sr.add_argument("--yes", action="store_true")
    ld = sub.add_parser("load", help="send many requests at once and see where speed and reliability degrade")
    ld.add_argument("--setup", default="nova-lite")
    ld.add_argument("--tasks", default="tasks")
    ld.add_argument("--levels", default="1,4,8,16", help="comma list of concurrency levels")
    ld.add_argument("--requests", type=int, default=24, help="requests at each level")
    ld.add_argument("--provider", choices=["bedrock", "fake"], default="bedrock")
    ld.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2
    commands = {"run": cmd_run, "report": cmd_report, "gate": cmd_gate, "export": cmd_export,
                "regress": cmd_regress, "load": cmd_load, "aiperf": cmd_aiperf,
                "sagemaker-status": cmd_sagemaker_status, "sagemaker-down": cmd_sagemaker_down,
                "sagemaker-run": cmd_sagemaker_run}
    return commands[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
