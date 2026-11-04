"""Model providers. BedrockProvider makes real calls (costs money). FakeProvider is offline for tests."""
import hashlib
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .setups import DEGRADED_PROMPT, Setup
from .tasks import Task
from .tools import TOOL_SPECS, run_tool


@dataclass
class ProviderResult:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    wait_ms: float = 0.0  # time spent waiting between retries, kept out of latency
    retries: int = 0  # how many times the call was retried after throttling or a temporary outage
    cost_usd: float | None = None  # set by providers that bill by time (SageMaker). None means price by tokens.
    error: str | None = None


RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
             "InternalServerException", "ModelTimeoutException"}
MAX_TRIES = 7  # waits 2, 4, 8, 16, 32, 60 seconds between tries


class BedrockProvider:
    """Calls the Bedrock Converse API and runs the tool loop (capped at max_tool_steps)."""

    def __init__(self, region: str, profile: str | None, max_output_tokens: int, max_tool_steps: int, timeout_s: int):
        import boto3
        from botocore.config import Config

        session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
        self.client = session.client(
            "bedrock-runtime",
            # botocore makes one attempt only. Retrying is done in _converse so waits stay out of latency.
            config=Config(read_timeout=timeout_s, connect_timeout=10, retries={"max_attempts": 1}),
        )
        self.max_output_tokens = max_output_tokens
        self.max_tool_steps = max_tool_steps

    def _converse(self, res: ProviderResult, **kwargs):
        """Call Converse. On throttling or a temporary outage, wait and try again."""
        delay = 2.0
        for attempt in range(MAX_TRIES):
            try:
                return self.client.converse(**kwargs)
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code not in RETRYABLE or attempt == MAX_TRIES - 1:
                    raise
                pause = delay + random.random()
                time.sleep(pause)
                res.wait_ms += pause * 1000
                res.retries += 1
                delay = min(delay * 2, 60)

    def generate(self, setup: Setup, task: Task) -> ProviderResult:
        res = ProviderResult()
        start = time.perf_counter()
        messages = [{"role": "user", "content": [{"text": task.prompt}]}]
        kwargs = {
            "modelId": setup.model_id,
            "system": [{"text": setup.system_prompt}],
            "inferenceConfig": {"maxTokens": self.max_output_tokens, "temperature": 0.0},
        }
        tools = [TOOL_SPECS[t] for t in task.tools_allowed] if setup.tools_on else []
        if tools:
            kwargs["toolConfig"] = {"tools": tools}
        try:
            for _ in range(self.max_tool_steps + 1):
                resp = self._converse(res, messages=messages, **kwargs)
                usage = resp.get("usage", {})
                res.input_tokens += usage.get("inputTokens", 0)
                res.output_tokens += usage.get("outputTokens", 0)
                msg = resp["output"]["message"]
                messages.append(msg)
                texts = [b["text"] for b in msg["content"] if "text" in b]
                uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
                if resp.get("stopReason") != "tool_use" or not uses:
                    res.text = "\n".join(texts).strip()
                    break
                if len(res.tool_calls) >= self.max_tool_steps:
                    res.text = "\n".join(texts).strip()
                    res.error = "hit max tool steps"
                    break
                results = []
                for use in uses:
                    out = run_tool(use["name"], use.get("input", {}))
                    res.tool_calls.append({"name": use["name"], "input": use.get("input", {}), "output": out})
                    results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": out}]}})
                messages.append({"role": "user", "content": results})
        except Exception as exc:  # noqa: BLE001 - record any API failure on the result instead of crashing the run
            res.error = f"{type(exc).__name__}: {exc}"
        res.latency_ms = (time.perf_counter() - start) * 1000 - res.wait_ms
        return res

    def complete(self, model_id: str, system: str, user: str, max_tokens: int = 200) -> ProviderResult:
        """Single call with no tools. Used by the judge."""
        res = ProviderResult()
        start = time.perf_counter()
        try:
            resp = self._converse(
                res,
                modelId=model_id,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.0},
            )
            usage = resp.get("usage", {})
            res.input_tokens = usage.get("inputTokens", 0)
            res.output_tokens = usage.get("outputTokens", 0)
            res.text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
        except Exception as exc:  # noqa: BLE001
            res.error = f"{type(exc).__name__}: {exc}"
        res.latency_ms = (time.perf_counter() - start) * 1000 - res.wait_ms
        return res


class FakeProvider:
    """Offline stand in for tests. Answers are made up, so results say NOTHING about real models.

    Each setup gets a fixed pretend skill level. The answer for a task is deterministic
    (seeded by setup and task), so tests are repeatable.
    """

    SKILL = {"nova-micro": 0.55, "nova-lite": 0.65, "nova-pro": 0.8, "claude-haiku": 0.9,
             "llama-3.1-8b": 0.5, "mistral-small": 0.6}

    def __init__(self, max_output_tokens: int = 400, **_):
        self.max_output_tokens = max_output_tokens
        self.calls = 0

    def _rng(self, *parts) -> random.Random:
        return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16))

    def generate(self, setup: Setup, task: Task) -> ProviderResult:
        self.calls += 1
        rng = self._rng(setup.name, task.id)
        skill = self.SKILL.get(setup.model_key, 0.6) - (0.15 if not setup.tools_on and task.tools_allowed else 0)
        if setup.system_prompt == DEGRADED_PROMPT:  # lets tests check that the regression demo catches a bad prompt
            skill -= 0.5
        right = rng.random() < skill
        if task.check_type == "refuse":
            text = "I can't help with that request." if right else "Sure, here is how you do it: step 1..."
        elif task.check_type == "number":
            val = float(task.expected) + (0 if right else 7)
            text = f"ANSWER: {val:g}"
        else:
            exp = task.expected if isinstance(task.expected, list) else [task.expected]
            text = f"ANSWER: {exp[0]}" if right else "ANSWER: I am not sure"
        in_tok = 120 + len(task.prompt) // 4
        out_tok = min(20 + len(text) // 4, self.max_output_tokens)
        calls = [{"name": task.tools_allowed[0], "input": {}, "output": {}}] if (task.tools_allowed and setup.tools_on) else []
        return ProviderResult(text=text, tool_calls=calls, input_tokens=in_tok, output_tokens=out_tok,
                              latency_ms=200 + rng.random() * 800)

    def complete(self, model_id: str, system: str, user: str, max_tokens: int = 200) -> ProviderResult:
        # Fake judge: calls it correct when the answer line is not the "not sure" placeholder.
        verdict = "0" if "not sure" in user.lower() else "1"
        return ProviderResult(text=f'{{"verdict": {verdict}, "reason": "fake judge"}}', input_tokens=150, output_tokens=15, latency_ms=100)


def _openai_tool(spec: dict) -> dict:
    """Turn a Bedrock style tool spec into the OpenAI style used by Ollama and similar servers."""
    t = spec["toolSpec"]
    return {"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["inputSchema"]["json"]}}


class OpenAICompatProvider:
    """Talks to any server with an OpenAI style /chat/completions endpoint, such as Ollama on your own machine.

    Same tool loop and the same step cap as the Bedrock provider, so results are comparable. Standard library
    only, no extra install. It costs nothing to run, but the answers depend on your hardware and the model you pulled.
    """

    def __init__(self, base_url: str, max_output_tokens: int, max_tool_steps: int, timeout_s: int):
        self.base_url = base_url.rstrip("/")
        self.max_output_tokens = max_output_tokens
        self.max_tool_steps = max_tool_steps
        self.timeout_s = timeout_s

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read())

    def generate(self, setup: Setup, task: Task) -> ProviderResult:
        res = ProviderResult()
        start = time.perf_counter()
        messages = [{"role": "system", "content": setup.system_prompt}, {"role": "user", "content": task.prompt}]
        tools = [_openai_tool(TOOL_SPECS[t]) for t in task.tools_allowed] if setup.tools_on else []
        try:
            for _ in range(self.max_tool_steps + 1):
                body = {"model": setup.model_id, "messages": messages, "temperature": 0.0,
                        "max_tokens": self.max_output_tokens}
                if tools:
                    body["tools"] = tools
                data = self._post(body)
                usage = data.get("usage", {})
                res.input_tokens += usage.get("prompt_tokens", 0)
                res.output_tokens += usage.get("completion_tokens", 0)
                msg = data["choices"][0]["message"]
                messages.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    res.text = (msg.get("content") or "").strip()
                    break
                if len(res.tool_calls) >= self.max_tool_steps:
                    res.text = (msg.get("content") or "").strip()
                    res.error = "hit max tool steps"
                    break
                for call in calls:
                    fn = call["function"]
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    out = run_tool(fn["name"], args)
                    res.tool_calls.append({"name": fn["name"], "input": args, "output": out})
                    messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(out)})
        except urllib.error.URLError as exc:
            # Server not running or unreachable. This is not the model's fault, so it is excluded from accuracy.
            res.error = f"ConnectionError: {exc.reason}"
        except Exception as exc:  # noqa: BLE001 - record any failure on the result instead of crashing the run
            res.error = f"{type(exc).__name__}: {exc}"
        res.latency_ms = (time.perf_counter() - start) * 1000
        return res


class SageMakerProvider(OpenAICompatProvider):
    """Calls a SageMaker endpoint that serves a model with the Text Generation Inference Messages API.

    The request and the tool loop are the same as the OpenAI style provider. SageMaker bills by instance hour,
    not by token, so each call is charged hourly price x the time it took. The harness runs it one request at a time,
    so that charge is exact for the compute used. It does not include the minutes the endpoint spends starting up.
    """

    def __init__(self, region: str, profile: str | None, endpoint_name: str, hourly_usd: float,
                 max_output_tokens: int, max_tool_steps: int, timeout_s: int, client=None):
        super().__init__("sagemaker", max_output_tokens, max_tool_steps, timeout_s)
        if client is None:
            import boto3
            from botocore.config import Config

            session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
            client = session.client("sagemaker-runtime", config=Config(read_timeout=timeout_s, retries={"max_attempts": 1}))
        self.client = client
        self.endpoint_name = endpoint_name
        self.hourly_usd = hourly_usd

    def _post(self, body: dict) -> dict:
        resp = self.client.invoke_endpoint(EndpointName=self.endpoint_name, ContentType="application/json",
                                           Body=json.dumps(body))
        return json.loads(resp["Body"].read())

    def generate(self, setup: Setup, task: Task) -> ProviderResult:
        res = super().generate(setup, task)
        res.cost_usd = res.latency_ms / 3_600_000 * self.hourly_usd
        return res


class RoutingProvider:
    """Sends each setup to the right provider (Bedrock or a local server) and creates each one only when needed.

    A run with only local setups never touches AWS. The judge always uses Bedrock.
    """

    def __init__(self, factories: dict):
        self._factories = factories
        self._made: dict = {}

    def _get(self, name: str):
        if name not in self._made:
            self._made[name] = self._factories[name]()
        return self._made[name]

    def generate(self, setup: Setup, task: Task) -> ProviderResult:
        return self._get(setup.provider).generate(setup, task)

    def complete(self, model_id: str, system: str, user: str, max_tokens: int = 200) -> ProviderResult:
        return self._get("bedrock").complete(model_id, system, user, max_tokens)
