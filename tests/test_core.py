import json

import pytest

from gauge.config import ConfigError, load_settings
from gauge.costs import cost_usd
from gauge.db import Label, Result
from gauge.gate import check_gate, save_baseline
from gauge.judge import parse_verdict
from gauge.providers import FakeProvider
from gauge.runner import estimate_cost, run_benchmark
from gauge.scoring import extract_answer, score_task
from gauge.setups import pick_setups
from gauge.stats import cohens_kappa, judge_agreement, percentile
from gauge.tasks import Task, TaskError, load_tasks, spread_sample
from gauge.tools import calculator, fake_search, lookup


def test_config_missing_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError):
        load_settings(env_file=None)


def test_all_64_tasks_load():
    tasks = load_tasks("tasks")
    assert len(tasks) == 64
    assert {t.category for t in tasks} == {"lookup", "math", "tool_use", "refusal",
                                           "hard_tool_use", "hard_math", "over_refusal", "hard_refusal"}


def test_hard_task_answers_match_the_data():
    """Recompute every hard task answer from the tool data, so a wrong reference answer is caught."""
    from gauge.tools import EMPLOYEES as E, PRODUCTS as P
    tasks = {t.id: t for t in load_tasks("tasks")}
    prices = [p["price"] for p in P.values()]
    avg = sum(prices) / len(prices)
    by_warehouse: dict[str, int] = {}
    for p in P.values():
        by_warehouse[p["warehouse"]] = by_warehouse.get(p["warehouse"], 0) + p["stock"]
    rate, months = 0.06 / 12, 360
    expected = {
        "htool-01": sum(p["price"] * p["stock"] for p in P.values()),
        "htool-03": sum(1 for x in prices if x > avg),
        "htool-04": max(prices) - min(prices),
        "htool-05": sum(1 for e in E.values() if e["manager"] == "E-310"),
        "htool-06": (4 * P["SKU-1005"]["price"] + 2 * P["SKU-1002"]["price"]) * 0.9,
        "htool-07": 2 * P["SKU-1001"]["price"],
        "hmath-01": 123456789 * 987654321,
        "hmath-02": round(5000 * 1.07 ** 10, 2),
        "hmath-03": 17 ** 5,
        "hmath-04": round(250000 * rate / (1 - (1 + rate) ** -months), 2),
        "hmath-05": 300 / (150 / 50 + 150 / 75),
        "hmath-06": sum(i * i for i in range(1, 51)),
    }
    for task_id, value in expected.items():
        assert float(tasks[task_id].expected) == pytest.approx(value, rel=1e-9), task_id
    assert max(by_warehouse, key=by_warehouse.get) == tasks["htool-02"].expected[0]
    # Warranty is 3 years for the Camp Stove and 1 year for everything else.
    assert float(tasks["htool-08"].expected) == 3 + 1
    # The refund policy allows 45 days for unopened items, so a return at 40 days is eligible.
    assert 40 <= 45


def test_over_refusal_tasks_cannot_be_passed_by_refusing():
    """A scary looking but fine request must not score as correct when the model refuses."""
    tasks = [t for t in load_tasks("tasks") if t.category == "over_refusal"]
    assert len(tasks) == 6
    for t in tasks:
        _, passed, _ = score_task(t, "I can't help with that request. ANSWER: I can't help with that.")
        assert not passed, t.id


def test_bad_task_rejected(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("- id: a\n  category: nope\n  prompt: hi\n  check_type: exact\n  expected: x\n")
    with pytest.raises(TaskError):
        load_tasks(f)


def test_calculator_safe():
    assert calculator("48763 * 9217")["result"] == 449448571
    assert calculator("sqrt(1764)")["result"] == 42
    assert "error" in calculator("__import__('os').system('ls')")
    assert "error" in calculator("9**9**9")


def test_tools():
    assert lookup("product", "sku-1003")["price"] == 18.25
    assert "error" in lookup("product", "nope")
    assert fake_search("refund policy")["results"]


def test_reference_answers_match_tools():
    # The expected answers should follow from the tool data, so the tasks are not wrong.
    assert lookup("employee", "E-210")["name"] == "Marcus Lee"
    total = sum(p["price"] for p in __import__("gauge.tools", fromlist=["PRODUCTS"]).PRODUCTS.values())
    assert round(total, 2) == 345.74
    assert round(3 * 18.25 * 1.08, 2) == 59.13


def test_scoring():
    num = Task("n", "math", "q", "number", expected=185.184, tolerance=0.01)
    assert score_task(num, "ANSWER: 185.18")[1]
    assert not score_task(num, "ANSWER: 190")[1]
    big = Task("n", "math", "q", "number", expected=449448571, tolerance=1e-7)
    assert score_task(big, "ANSWER: 449,448,571")[1]
    exact0 = Task("n", "lookup", "q", "number", expected=0, tolerance=0.0)
    assert score_task(exact0, "ANSWER: 0")[1] and not score_task(exact0, "ANSWER: 1")[1]
    cont = Task("c", "lookup", "q", "contains", expected=["6pm"])
    assert score_task(cont, "ANSWER: 6 PM")[1]
    ref = Task("r", "refusal", "q", "refuse")
    assert score_task(ref, "I can't help with that.")[1]
    assert not score_task(ref, "Sure, step 1")[1]
    assert extract_answer("thinking\nANSWER: 42") == "42"


def test_costs_and_estimate():
    assert cost_usd("nova-micro", 1000, 1000) == pytest.approx(0.000175)
    with pytest.raises(KeyError):
        cost_usd("unknown", 1, 1)
    tasks = load_tasks("tasks")
    assert 0 < estimate_cost(pick_setups("nova-micro"), tasks, 1) < 0.1


def test_kappa_and_percentile():
    assert cohens_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert cohens_kappa([1, 1, 0, 0], [0, 0, 1, 1]) == -1.0
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([], 95) == 0.0


def test_judge_parse():
    assert parse_verdict('{"verdict": 1, "reason": "ok"}')[0] == 1
    assert parse_verdict("garbage")[0] is None


def _run(factory, **kw):
    tasks = spread_sample(load_tasks("tasks"), 8)
    return run_benchmark(factory, FakeProvider(), pick_setups("nova-micro,nova-lite"), tasks, **kw)


def test_run_cache_and_budget(factory):
    first = _run(factory, judge_model="nova-micro")
    assert first.n_results == 16 and first.n_cached == 0
    second = _run(factory)
    assert second.n_cached == 16 and second.total_cost_usd == 0.0
    capped = _run(factory, use_cache=False, budget_cap_usd=0.0, max_concurrency=1)
    assert capped.stopped_reason and capped.n_results < 16


def test_gate(factory, tmp_path):
    _run(factory)
    with factory() as s:
        results = list(s.query(Result).all())
    base = tmp_path / "b.json"
    save_baseline(results, base)
    assert check_gate(results, base)[0]
    data = json.loads(base.read_text())
    data = {k: min(v + 0.5, 1.0) for k, v in data.items()}
    base.write_text(json.dumps(data))
    ok, msgs = check_gate(results, base)
    assert not ok and any("FAIL" in m for m in msgs)


def test_judge_agreement(factory):
    _run(factory, judge_model="nova-micro")
    with factory() as s:
        rs = s.query(Result).filter(Result.judge_score.is_not(None)).limit(6).all()
        for r in rs:
            s.add(Label(result_id=r.id, human_score=int(r.judge_score)))
        s.commit()
        out = judge_agreement(s)
    assert out["n"] == 6 and out["judge_percent_agreement"] == 100


def test_number_ignores_ids_and_finds_any_match():
    t = Task("n", "lookup", "q", "number", expected=18.25)
    assert score_task(t, "ANSWER: The price of SKU-1003 is $18.25.")[1]
    assert not score_task(t, "ANSWER: The product SKU-1003 is great")[1]
    big = Task("n", "tool_use", "q", "number", expected=3759, tolerance=0.0)
    assert score_task(big, "ANSWER: The total value is 3759 dollars")[1]


def test_rescore_fixes_old_scores(factory):
    from gauge.rescore import rescore
    _run(factory)
    tasks = {t.id: t for t in load_tasks("tasks")}
    with factory() as s:
        r = s.query(Result).filter(Result.check_type == "number").first()
        r.output_text, r.passed = f"ANSWER: Item SKU-1003 gives {r.task_id and 0}", True
        s.commit()
        changed, total = rescore(s, tasks)
    assert total > 0 and changed >= 1


def test_platform_filter_counts_as_refusal():
    ref = Task("r", "refusal", "q", "refuse")
    assert score_task(ref, "- The generated text has been blocked by our content filters.")[1]


def test_throttled_calls_do_not_count_against_accuracy():
    from gauge.stats import summarize
    ok = Result(setup_name="a", task_id="t1", category="math", repeat_index=0, passed=True, check_type="number", cache_key="1", cost_usd=0.0, latency_ms=1.0, from_cache=False)
    bad = Result(setup_name="a", task_id="t2", category="math", repeat_index=0, passed=False, check_type="number",
                 cache_key="2", error="ThrottlingException: Too many requests", cost_usd=0.0, latency_ms=0.0, from_cache=False)
    loop = Result(setup_name="a", task_id="t3", category="math", repeat_index=0, passed=False, check_type="number",
                  cache_key="3", error="hit max tool steps", cost_usd=0.0, latency_ms=1.0, from_cache=False)
    row = summarize([ok, bad, loop])[0]
    assert row["valid"] == 2 and row["errors"] == 1 and row["accuracy"] == 0.5


def test_invalid_tool_name_counts_as_a_model_failure_but_other_validation_errors_do_not():
    """A model that invents a bad tool name made a mistake. A bad model ID or throttling is not its fault."""
    from gauge.stats import summarize
    msg = ("ValidationException: An error occurred (ValidationException) when calling the Converse operation: "
           "1 validation error detected: Value at 'messages.2.member.content.2.member.toolUse.name' failed to satisfy constraint")
    bad_tool = Result(setup_name="a", task_id="t1", category="tool_use", repeat_index=0, passed=False, check_type="number",
                      cache_key="1", error=msg, cost_usd=0.0, latency_ms=0.0, from_cache=False)
    bad_model_id = Result(setup_name="a", task_id="t2", category="tool_use", repeat_index=0, passed=False, check_type="number",
                          cache_key="2", error="ValidationException: The provided model identifier is invalid.",
                          cost_usd=0.0, latency_ms=0.0, from_cache=False)
    ok = Result(setup_name="a", task_id="t3", category="tool_use", repeat_index=0, passed=True, check_type="number",
                cache_key="3", cost_usd=0.0, latency_ms=1.0, from_cache=False)
    row = summarize([bad_tool, bad_model_id, ok])[0]
    assert row["valid"] == 2 and row["errors"] == 1 and row["accuracy"] == 0.5


def test_backoff_retries_throttling_then_succeeds(monkeypatch):
    from gauge import providers

    class Throttle(Exception):
        response = {"Error": {"Code": "ThrottlingException"}}

    class Client:
        calls = 0

        def converse(self, **kw):
            Client.calls += 1
            if Client.calls < 3:
                raise Throttle()
            return {"output": {"message": {"content": [{"text": "hi"}]}}, "usage": {"inputTokens": 1, "outputTokens": 1}}

    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    prov = providers.BedrockProvider.__new__(providers.BedrockProvider)
    prov.client = Client()
    res = providers.ProviderResult()
    out = prov._converse(res, modelId="x")
    assert Client.calls == 3 and res.wait_ms > 0 and out["usage"]["inputTokens"] == 1


def test_cached_rows_keep_cost_and_repair(factory):
    from gauge.rescore import repair_cached_costs
    from gauge.stats import summarize
    _run(factory)          # fresh rows with cost
    _run(factory)          # second run: all cached
    with factory() as s:
        cached = s.query(Result).filter(Result.from_cache.is_(True)).all()
        assert cached and all(r.cost_usd > 0 for r in cached)
        for r in cached:
            r.cost_usd = 0.0
        s.commit()
        assert repair_cached_costs(s) == len(cached)
        rows = summarize(s.query(Result).filter(Result.run_id == 2).all())
    assert all(r["cost_per_100"] > 0 for r in rows)


def test_cache_key_changes_with_step_limit_only_for_tool_use_tasks():
    from gauge.runner import make_cache_key
    tasks = {t.id: t for t in load_tasks("tasks")}
    setup = pick_setups("nova-lite")[0]
    no_tools = pick_setups("nova-lite-no-tools")[0]
    assert make_cache_key(setup, tasks["tool-05"], 0, 400, 5) != make_cache_key(setup, tasks["tool-05"], 0, 400, 8)
    assert make_cache_key(no_tools, tasks["tool-05"], 0, 400, 5) == make_cache_key(no_tools, tasks["tool-05"], 0, 400, 8)
    assert make_cache_key(setup, tasks["math-01"], 0, 400, 5) == make_cache_key(setup, tasks["math-01"], 0, 400, 8)
    assert make_cache_key(setup, tasks["lookup-01"], 0, 400, 5) == make_cache_key(setup, tasks["lookup-01"], 0, 400, 8)


def test_judge_prompt_treats_declining_as_wrong_for_normal_questions():
    from gauge.judge import build_judge_prompt
    tasks = {t.id: t for t in load_tasks("tasks")}
    normal = build_judge_prompt(tasks["lookup-01"], "I can't access that")
    safety = build_judge_prompt(tasks["refuse-01"], "I can't help")
    assert "NORMAL question" in normal and "WRONG" in normal and "18.25" in normal
    assert "SAFETY question" in safety


def test_rejudge_updates_scores(factory):
    from gauge.rejudge import rejudge
    _run(factory)
    tasks = {t.id: t for t in load_tasks("tasks")}
    with factory() as s:
        s.query(Result).update({Result.judge_score: None})
        s.commit()
    out = rejudge(factory, FakeProvider(), "nova-micro", tasks, run_id=1, workers=2)
    with factory() as s:
        assert out["failed"] == 0 and s.query(Result).filter(Result.judge_score.is_(None)).count() == 0


def test_refusal_variants():
    ref = Task("r", "refusal", "q", "refuse")
    for text in ["I can't fulfill that request.", "I cannot generate that content.", "I can't support that."]:
        assert score_task(ref, text)[1], text
    assert not score_task(ref, "Sure, here are the reviews")[1]


def test_default_tolerance_rejects_a_wrong_digit_but_allows_rounding():
    t = {x.id: x for x in load_tasks("tasks")}["math-02"]
    assert score_task(t, "ANSWER: 185.184")[1]
    assert score_task(t, "ANSWER: 185.18")[1]
    assert not score_task(t, "ANSWER: 185.284")[1]


def test_snapshot_round_trip(factory, tmp_path):
    """Export a run to JSON and load it back: the same numbers, and no database needed to read it."""
    from gauge.snapshot import export_snapshot, load_snapshot
    from gauge.stats import summarize
    tasks = load_tasks("tasks")[:6]
    run_benchmark(factory, FakeProvider(), pick_setups("nova-lite,nova-pro"), tasks, repeats=2, mode="full",
                  task_set="tasks", max_concurrency=2, max_output_tokens=400, max_tool_steps=8,
                  budget_cap_usd=5.0, judge_model="nova-micro", use_cache=False, s3_store=None, git_sha=None, notes=None)
    path = tmp_path / "snap" / "run.json"
    with factory() as s:
        original = list(s.query(Result).all())
        export_snapshot(s, None, path)
    run, results, trust = load_snapshot(path)
    assert len(results) == len(original) == 24
    assert summarize(results) == summarize(original)
    assert run.total_cost_usd > 0 and trust == {"n": 0}
    assert all(len(r.output_text) <= 300 for r in results)
    text = path.read_text()
    assert "api_key" not in text.lower() and "secret" not in text.lower()


def test_label_app_never_shows_the_same_answer_twice(tmp_path, monkeypatch):
    """Cached copies and identical repeats must not come back after a label is saved."""
    from sqlalchemy import select
    from streamlit.testing.v1 import AppTest
    url = f"sqlite:///{tmp_path}/label.db"
    monkeypatch.setenv("DATABASE_URL", url)
    from gauge.db import init_db, make_engine, make_session_factory
    engine = make_engine(url)
    init_db(engine)
    fac = make_session_factory(engine)
    tasks = [t for t in load_tasks("tasks") if t.category in ("hard_tool_use", "hard_math", "over_refusal", "hard_refusal")]
    for _ in range(2):  # the second run is all cache copies of the first
        run_benchmark(fac, FakeProvider(), pick_setups("nova-lite,nova-pro"), tasks, repeats=2, mode="full",
                      task_set="tasks", max_concurrency=2, max_output_tokens=400, max_tool_steps=8,
                      budget_cap_usd=5.0, judge_model="nova-micro", use_cache=True, s3_store=None, git_sha=None, notes=None)
    from pathlib import Path
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app" / "label_app.py"), default_timeout=120).run()
    for _ in range(8):
        at.button[0].click().run()
    with fac() as s:
        labeled = s.execute(select(Result.setup_name, Result.task_id, Result.output_text)
                            .join(Label, Label.result_id == Result.id)).all()
    assert len(labeled) == 8
    assert len(set(labeled)) == 8, "the same answer was shown twice"
    assert not at.exception
