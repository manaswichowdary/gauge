"""Tests for the regression demo, load test, scheduled mode, local provider and routing. All offline and free."""
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gauge import cli, providers
from gauge.db import Result
from gauge.gate import check_against
from gauge.loadtest import first_degraded, run_load_test, to_markdown
from gauge.providers import FakeProvider, OpenAICompatProvider, ProviderResult, RoutingProvider
from gauge.regress import run_regression_demo
from gauge.setups import DEGRADED_PROMPT, SETUPS, pick_setups
from gauge.stats import is_infra_error
from gauge.tasks import Task, load_tasks, spread_sample

KW = dict(max_concurrency=2, max_output_tokens=400, max_tool_steps=8, budget_cap_usd=5.0)


def test_check_against_uses_a_dict_baseline():
    r = Result(setup_name="a", task_id="t", category="math", repeat_index=0, passed=True, cost_usd=0, latency_ms=1)
    ok, _ = check_against([r], {"a": 1.0})
    assert ok
    ok, msgs = check_against([r], {"a": 1.0, "zzz": 1.0})  # setups not in the run are skipped
    assert ok and len(msgs) == 1
    ok, msgs = check_against([r], {"other": 1.0})
    assert not ok and "no setups in common" in msgs[0]


def test_seeded_regression_is_caught_and_uses_the_same_setup_name(factory):
    tasks = spread_sample(load_tasks("tasks"), 24)
    setup = pick_setups("nova-lite")[0]
    out = run_regression_demo(factory, FakeProvider(), setup, tasks, repeats=1, **KW)
    assert out["caught"] and out["drop_points"] > 5
    assert out["baseline_accuracy"] > out["regressed_accuracy"]
    assert out["seconds_to_detect"] >= 0 and out["degraded_prompt"] == DEGRADED_PROMPT
    with factory() as s:
        broken = s.query(Result).filter(Result.run_id == out["degraded_run_id"]).all()
    assert {r.setup_name for r in broken} == {"nova-lite"}  # same name, so the gate compares like with like


def test_regression_demo_reports_a_miss_when_the_gate_does_not_fire(factory):
    # A provider that ignores the prompt: the "broken" run is as good as the healthy one, so nothing should be caught.
    class IgnoresPrompt(FakeProvider):
        def generate(self, setup, task):
            from dataclasses import replace
            return super().generate(replace(setup, system_prompt="plain"), task)

    tasks = spread_sample(load_tasks("tasks"), 12)
    out = run_regression_demo(factory, IgnoresPrompt(), pick_setups("nova-lite")[0], tasks, **KW)
    assert not out["caught"] and out["drop_points"] == 0


def test_load_test_structure_and_markdown():
    tasks = spread_sample(load_tasks("tasks"), 6)
    data = run_load_test(FakeProvider(), pick_setups("nova-lite")[0], tasks, levels=(1, 3), requests_per_level=5)
    assert [r["concurrency"] for r in data["levels"]] == [1, 3]
    assert all(r["requests"] == 5 and r["ok"] == 5 and r["errors"] == 0 for r in data["levels"])
    assert data["first_degraded_level"] is None
    assert "| Concurrency |" in to_markdown(data)


def test_first_degraded_level_finds_errors_retries_and_slow_p95():
    base = {"concurrency": 1, "errors": 0, "retries": 0, "p95_with_retries_ms": 1000}
    fine = {"concurrency": 4, "errors": 0, "retries": 0, "p95_with_retries_ms": 1500}
    assert first_degraded([base, fine]) is None
    assert first_degraded([base, {**fine, "retries": 2}]) == 4
    assert first_degraded([base, fine, {**fine, "concurrency": 8, "errors": 1}]) == 8
    assert first_degraded([base, {**fine, "p95_with_retries_ms": 2500}]) == 4
    assert first_degraded([]) is None


def test_throttle_retries_are_counted(monkeypatch):
    class Throttle(Exception):
        response = {"Error": {"Code": "ThrottlingException"}}

    class Client:
        calls = 0

        def converse(self, **kw):
            Client.calls += 1
            if Client.calls < 3:
                raise Throttle()
            return {"output": {"message": {"content": [{"text": "hi"}]}}, "usage": {}}

    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    prov = providers.BedrockProvider.__new__(providers.BedrockProvider)
    prov.client = Client()
    res = ProviderResult()
    prov._converse(res, modelId="x")
    assert res.retries == 2


def test_scheduled_mode_runs_all_tasks_on_the_cheap_models_and_gates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/s.db")
    monkeypatch.chdir(tmp_path)
    tasks_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tasks")
    assert cli.main(["run", "--mode", "scheduled", "--tasks", tasks_dir, "--provider", "fake", "--yes"]) == 0
    base = str(tmp_path / "baseline-ci.json")
    assert cli.main(["gate", "--save-baseline", "--baseline", base]) == 1  # no full run, and it says so
    assert cli.main(["gate", "--save-baseline", "--baseline", base, "--mode", "scheduled"]) == 0
    data = json.loads((tmp_path / "baseline-ci.json").read_text())
    assert set(data) == {"nova-micro", "nova-lite"}
    assert cli.main(["gate", "--baseline", base, "--mode", "scheduled"]) == 0
    assert cli.main(["gate", "--baseline", str(tmp_path / "missing.json"), "--mode", "scheduled"]) == 1


def test_regress_and_load_commands_write_their_files(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/r.db")
    monkeypatch.chdir(tmp_path)
    tasks_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tasks")
    assert cli.main(["regress", "--provider", "fake", "--yes", "--tasks", tasks_dir, "--tasks-limit", "24"]) == 0
    reg = json.loads((tmp_path / "snapshot" / "regression.json").read_text())
    assert reg["caught"] and reg["fake"]
    assert cli.main(["load", "--provider", "fake", "--yes", "--tasks", tasks_dir, "--levels", "1,2", "--requests", "4"]) == 0
    load = json.loads((tmp_path / "snapshot" / "load.json").read_text())
    assert len(load["levels"]) == 2 and load["fake"]


# A tiny fake OpenAI style server: first it asks for a tool, then it answers.
class _Handler(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.bodies.append(body)
        if len(_Handler.bodies) == 1:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"kind": "product", "key": "SKU-1001"})}}]}
        else:
            msg = {"role": "assistant", "content": "ANSWER: 89.5"}
        payload = json.dumps({"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


@pytest.fixture
def local_server():
    _Handler.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


def test_local_provider_runs_the_tool_loop(local_server):
    setup = pick_setups("ollama-llama3.1")[0]
    task = Task(id="t", category="tool_use", prompt="Price of SKU-1001?", check_type="number", expected=89.5,
                tools_allowed=["lookup"])
    out = OpenAICompatProvider(local_server, 400, 8, 10).generate(setup, task)
    assert out.error is None and out.text == "ANSWER: 89.5"
    assert [c["name"] for c in out.tool_calls] == ["lookup"] and out.tool_calls[0]["output"]["key"] == "SKU-1001"
    assert out.input_tokens == 20 and out.output_tokens == 10
    first, second = _Handler.bodies
    assert first["messages"][0]["role"] == "system" and first["model"] == "llama3.1:8b"
    assert first["tools"][0]["function"]["name"] == "lookup" and first["max_tokens"] == 400
    assert second["messages"][-1]["role"] == "tool" and second["messages"][-1]["tool_call_id"] == "c1"


def test_local_provider_sends_no_tools_when_they_are_off(local_server):
    from dataclasses import replace
    setup = replace(pick_setups("ollama-llama3.1")[0], tools_on=False)
    task = Task(id="t", category="math", prompt="2+2?", check_type="number", expected=4, tools_allowed=["calculator"])
    OpenAICompatProvider(local_server, 400, 8, 10).generate(setup, task)
    assert "tools" not in _Handler.bodies[0]


def test_local_server_down_is_an_infrastructure_error_not_a_wrong_answer():
    setup = pick_setups("ollama-llama3.1")[0]
    task = Task(id="t", category="math", prompt="2+2?", check_type="number", expected=4, tools_allowed=[])
    out = OpenAICompatProvider("http://127.0.0.1:9/v1", 400, 8, 2).generate(setup, task)
    assert out.error.startswith("ConnectionError")
    assert is_infra_error(Result(error=out.error))


def test_router_creates_only_the_provider_it_needs():
    made = []

    class P:
        def __init__(self, name):
            self.name = name

        def generate(self, setup, task):
            return self.name

        def complete(self, *a):
            return self.name

    router = RoutingProvider({"bedrock": lambda: made.append("bedrock") or P("bedrock"),
                              "local": lambda: made.append("local") or P("local")})
    assert router.generate(pick_setups("ollama-llama3.1")[0], None) == "local"
    assert made == ["local"]  # no Bedrock client was built, so no AWS needed
    assert router.generate(pick_setups("nova-lite")[0], None) == "bedrock"
    assert router.complete("m", "s", "u") == "bedrock"


def test_local_setups_are_free_and_not_part_of_all():
    assert all(s.provider == "bedrock" for s in SETUPS)
    assert pick_setups("ollama-qwen2.5")[0].provider == "local"
    from gauge.costs import cost_usd
    assert cost_usd("ollama-llama3.1", 1000, 1000) == 0.0


def test_local_only_runs_never_ask_for_confirmation(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: (_ for _ in ()).throw(AssertionError("should not ask")))
    assert cli.confirm_cost(pick_setups("ollama-llama3.1"), "bedrock", uses_judge=False, yes=False)
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    assert not cli.confirm_cost(pick_setups("nova-lite"), "bedrock", uses_judge=False, yes=False)
    assert cli.confirm_cost(pick_setups("nova-lite"), "bedrock", uses_judge=False, yes=True)


def test_report_shows_run_time_and_flags_cached_answers(factory):
    from gauge.report import build_report
    from gauge.runner import run_benchmark
    tasks = spread_sample(load_tasks("tasks"), 8)
    setups = pick_setups("nova-lite")
    run_benchmark(factory, FakeProvider(), setups, tasks, **KW)
    summary = run_benchmark(factory, FakeProvider(), setups, tasks, **KW)  # second run is all cache
    from gauge.db import Run
    with factory() as s:
        run = s.get(Run, summary.run_id)
        rows = list(s.query(Result).filter(Result.run_id == run.id))
    text = build_report(run, rows)
    assert "Run time:" in text and "came from the cache" in text


def test_export_also_writes_a_markdown_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/e.db")
    monkeypatch.chdir(tmp_path)
    tasks_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tasks")
    assert cli.main(["run", "--mode", "dev", "--tasks", tasks_dir, "--provider", "fake", "--yes"]) == 0
    assert cli.main(["export"]) == 1  # the default is the latest full run, and there is none
    assert cli.main(["export", "--mode", "dev"]) == 0
    assert (tmp_path / "snapshot" / "run.json").exists()
    assert "Results by setup" in (tmp_path / "results" / "run-1.md").read_text()


# ---- SageMaker ----
class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps(self.data).encode()


class FakeRuntime:
    """Stands in for the sagemaker-runtime client: asks for a tool once, then answers."""

    def __init__(self):
        self.calls = []

    def invoke_endpoint(self, **kw):
        self.calls.append(kw)
        if len(self.calls) == 1:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": json.dumps({"kind": "product", "key": "SKU-1001"})}}]}
        else:
            msg = {"role": "assistant", "content": "ANSWER: 89.5"}
        return {"Body": _Body({"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})}


def _sm_task():
    return Task(id="t", category="tool_use", prompt="Price of SKU-1001?", check_type="number", expected=89.5, tools_allowed=["lookup"])


def test_sagemaker_provider_calls_the_endpoint_and_charges_by_the_hour():
    from gauge.providers import SageMakerProvider
    from gauge.setups import SAGEMAKER_SETUP
    rt = FakeRuntime()
    prov = SageMakerProvider("us-east-1", None, "ep-1", 3.6, 400, 8, 10, client=rt)
    out = prov.generate(SAGEMAKER_SETUP, _sm_task())
    assert out.error is None and out.text == "ANSWER: 89.5" and len(out.tool_calls) == 1
    assert rt.calls[0]["EndpointName"] == "ep-1" and rt.calls[0]["ContentType"] == "application/json"
    body = json.loads(rt.calls[0]["Body"])
    assert body["model"] == "Qwen/Qwen2.5-7B-Instruct" and body["tools"][0]["function"]["name"] == "lookup"
    # $3.60 an hour is $0.001 a second, so the charge is latency in seconds times 0.001
    assert out.cost_usd == pytest.approx(out.latency_ms / 1000 * 0.001)


def test_runner_uses_the_provider_cost_when_it_sets_one(factory):
    from gauge.runner import run_benchmark

    class Billed(FakeProvider):
        def generate(self, setup, task):
            res = super().generate(setup, task)
            res.cost_usd = 0.5
            return res

    tasks = spread_sample(load_tasks("tasks"), 2)
    summary = run_benchmark(factory, Billed(), pick_setups("nova-lite"), tasks, **KW)
    assert summary.total_cost_usd == pytest.approx(1.0)


class FakeSM:
    """Stands in for the sagemaker client."""

    def __init__(self, exists=True):
        self.deleted, self.exists = [], exists

    def describe_endpoint(self, EndpointName):
        if not self.exists:
            raise _NotFound()
        return {"EndpointConfigName": "cfg-1"}

    def delete_endpoint(self, EndpointName):
        self.deleted.append(("endpoint", EndpointName))

    def describe_endpoint_config(self, EndpointConfigName):
        if not self.exists:
            raise _NotFound()
        return {"ProductionVariants": [{"ModelName": "model-1"}]}

    def delete_endpoint_config(self, EndpointConfigName):
        self.deleted.append(("config", EndpointConfigName))

    def delete_model(self, ModelName):
        self.deleted.append(("model", ModelName))

    def get_paginator(self, name):
        sm = self

        class P:
            def paginate(self):
                return [{"Endpoints": [{"EndpointName": "e1", "EndpointStatus": "InService"}] if sm.exists else []}]
        return P()


class _NotFound(Exception):
    response = {"Error": {"Code": "ValidationException", "Message": "Could not find endpoint"}}


def test_delete_endpoint_everything_removes_endpoint_config_and_model_and_is_safe_twice():
    from gauge.sagemaker_ops import delete_endpoint_everything
    sm = FakeSM()
    done = delete_endpoint_everything(sm, "ep-1")
    assert sm.deleted == [("endpoint", "ep-1"), ("config", "cfg-1"), ("model", "model-1")] and len(done) == 3
    # endpoint and config already gone: it does not crash, and still tries the model that shares the name
    assert delete_endpoint_everything(FakeSM(exists=False), "ep-1") == ["model ep-1"]


def test_with_endpoint_always_deletes_even_when_the_work_fails_or_is_interrupted():
    from gauge.sagemaker_ops import with_endpoint
    log = []
    with pytest.raises(RuntimeError):
        with with_endpoint(lambda n: log.append(("up", n)), lambda n: log.append(("down", n)), "x"):
            raise RuntimeError("boom")
    assert log == [("up", "x"), ("down", "x")]
    log.clear()
    with pytest.raises(KeyboardInterrupt):
        with with_endpoint(lambda n: log.append(("up", n)), lambda n: log.append(("down", n)), "y"):
            raise KeyboardInterrupt
    assert log[-1] == ("down", "y")
    log.clear()
    with pytest.raises(RuntimeError):  # even a failed deploy gets cleaned up (it may be half created)
        with with_endpoint(lambda n: (_ for _ in ()).throw(RuntimeError("deploy failed")), lambda n: log.append(("down", n)), "z"):
            pass
    assert log == [("down", "z")]


def _sm_env(monkeypatch, tmp_path, hourly="3.6", role="arn:aws:iam::1:role/x"):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/sm.db")
    monkeypatch.setenv("SAGEMAKER_HOURLY_USD", hourly)
    monkeypatch.setenv("SAGEMAKER_ROLE_ARN", role)
    monkeypatch.chdir(tmp_path)


def test_sagemaker_run_refuses_without_a_role_or_an_hourly_price(monkeypatch, tmp_path):
    _sm_env(monkeypatch, tmp_path, role="")
    assert cli.main(["sagemaker-run", "--yes"]) == 1
    _sm_env(monkeypatch, tmp_path, hourly="0")
    assert cli.main(["sagemaker-run", "--yes"]) == 1


def test_sagemaker_run_deploys_benchmarks_and_deletes(monkeypatch, tmp_path, capsys):
    from gauge.providers import SageMakerProvider
    _sm_env(monkeypatch, tmp_path)
    sm, deployed = FakeSM(), []
    monkeypatch.setattr(cli, "sagemaker_client", lambda s: sm)
    monkeypatch.setattr(cli, "deploy_endpoint", lambda *a, **k: deployed.append(a[0]))
    monkeypatch.setattr(cli, "SageMakerProvider", lambda *a, **k: SageMakerProvider(*a, client=_Answerer(), **k))
    tasks_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tasks")
    assert cli.main(["sagemaker-run", "--yes", "--tasks", tasks_dir, "--tasks-limit", "4"]) == 0
    assert deployed and sm.deleted[0][0] == "endpoint"  # it was cleaned up
    assert "Compute cost" in capsys.readouterr().out


class _Answerer:
    def invoke_endpoint(self, **kw):
        body = {"choices": [{"message": {"role": "assistant", "content": "ANSWER: 1"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
        return {"Body": _Body(body)}


def test_sagemaker_run_stops_and_still_deletes_when_the_smoke_test_fails(monkeypatch, tmp_path, capsys):
    from gauge.providers import SageMakerProvider
    _sm_env(monkeypatch, tmp_path)
    sm = FakeSM()

    class Broken:
        def invoke_endpoint(self, **kw):
            raise RuntimeError("endpoint is not ready")

    monkeypatch.setattr(cli, "sagemaker_client", lambda s: sm)
    monkeypatch.setattr(cli, "deploy_endpoint", lambda *a, **k: None)
    monkeypatch.setattr(cli, "SageMakerProvider", lambda *a, **k: SageMakerProvider(*a, client=Broken(), **k))
    tasks_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tasks")
    assert cli.main(["sagemaker-run", "--yes", "--tasks", tasks_dir, "--tasks-limit", "4"]) == 1
    assert "Smoke test failed" in capsys.readouterr().out and sm.deleted[0][0] == "endpoint"


def test_sagemaker_status_and_down_commands(monkeypatch, tmp_path, capsys):
    _sm_env(monkeypatch, tmp_path)
    sm = FakeSM()
    monkeypatch.setattr(cli, "sagemaker_client", lambda s: sm)
    assert cli.main(["sagemaker-status"]) == 0 and "e1: InService" in capsys.readouterr().out
    assert cli.main(["sagemaker-down", "--name", "e1"]) == 0 and ("endpoint", "e1") in sm.deleted


# ---- AIPerf ----
def test_aiperf_command_and_url():
    from gauge.aiperf_run import build_command, server_url
    assert server_url("http://localhost:11434/v1") == "http://localhost:11434"
    cmd = build_command("qwen2.5:7b", "http://localhost:11434", "Qwen/Qwen2.5-7B-Instruct", 4, 20, "art/CON4")
    assert cmd[:3] == ["aiperf", "profile", "--model"] and "--streaming" in cmd
    assert cmd[cmd.index("--concurrency") + 1] == "4" and cmd[cmd.index("--artifact-dir") + 1] == "art/CON4"


def test_aiperf_reads_the_export_and_skips_metrics_it_does_not_have(tmp_path):
    from gauge.aiperf_run import read_export
    d = tmp_path / "CON1"
    d.mkdir()
    (d / "profile_export_aiperf.json").write_text(json.dumps({
        "time_to_first_token": {"unit": "ms", "avg": 120.5, "p50": 110.0, "p90": 150.0, "p99": 190.0, "min": 90.0},
        "output_token_throughput": {"unit": "tokens/sec", "avg": 42.0},
        "something_else": {"avg": 1}}))
    out = read_export(d)
    assert out == {"time_to_first_token": {"avg": 120.5, "p50": 110.0, "p90": 150.0, "p99": 190.0, "unit": "ms"},
                   "output_token_throughput": {"avg": 42.0, "unit": "tokens/sec"}}
    assert read_export(tmp_path / "nothing") == {}


def test_run_aiperf_runs_each_level_and_reports_failures(tmp_path):
    from types import SimpleNamespace
    from gauge.aiperf_run import run_aiperf
    ran = []

    def runner(cmd, check):
        ran.append(cmd)
        art = Path(cmd[cmd.index("--artifact-dir") + 1])
        art.mkdir(parents=True, exist_ok=True)
        (art / "profile_export_aiperf.json").write_text(json.dumps({"request_throughput": {"avg": 2.5, "unit": "requests/sec"}}))
        return SimpleNamespace(returncode=1 if "--concurrency" in cmd and cmd[cmd.index("--concurrency") + 1] == "4" else 0)

    setup = pick_setups("ollama-qwen2.5")[0]
    data = run_aiperf(setup, "http://localhost:11434/v1", levels=(1, 4), requests_per_level=5, out_root=str(tmp_path / "a"),
                      runner=runner, which=lambda n: "/bin/aiperf", progress=lambda *_: None)
    assert [r["concurrency"] for r in data["levels"]] == [1, 4] and len(ran) == 2
    assert data["levels"][0]["metrics"]["request_throughput"]["avg"] == 2.5
    assert data["levels"][1]["returncode"] == 1 and data["levels"][1]["metrics"] == {}


def test_run_aiperf_explains_when_it_is_not_installed():
    from gauge.aiperf_run import run_aiperf
    with pytest.raises(RuntimeError, match="pip install -r requirements-aiperf.txt"):
        run_aiperf(pick_setups("ollama-qwen2.5")[0], "http://x/v1", which=lambda n: None)


def test_aiperf_command_refuses_a_bedrock_setup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/a.db")
    assert cli.main(["aiperf", "--setup", "nova-lite"]) == 1
    assert "local setup" in capsys.readouterr().out


def test_export_and_gate_default_to_the_latest_full_run_not_a_later_regression_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/m.db")
    monkeypatch.chdir(tmp_path)
    tasks_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "tasks")
    assert cli.main(["run", "--mode", "full", "--setups", "nova-lite", "--repeats", "1", "--tasks", tasks_dir,
                     "--provider", "fake", "--yes"]) == 0                      # run 1, full
    assert cli.main(["regress", "--provider", "fake", "--yes", "--tasks", tasks_dir, "--tasks-limit", "24"]) == 0  # runs 2 and 3
    assert cli.main(["export"]) == 0
    assert json.loads((tmp_path / "snapshot" / "run.json").read_text())["run"]["id"] == 1
    assert cli.main(["gate", "--save-baseline"]) == 0
    assert cli.main(["gate"]) == 0  # checks run 1 against itself, not the broken run
