"""Run NVIDIA AIPerf against a local OpenAI style server and keep a small summary for the dashboard.

AIPerf is a separate open source load testing tool (pip install -r requirements-aiperf.txt). It sends many streaming chat
requests and reports time to first token, request latency, inter token latency and throughput. It talks the OpenAI API, so
it works against a local Ollama server, not against Bedrock. This module runs it once per concurrency level and reads the
JSON file it writes. The harness itself never imports AIPerf, so you can use everything else without it.
"""
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# Tokenizers AIPerf needs to count tokens. The Llama one is gated: accept its license on Hugging Face first.
TOKENIZERS = {
    "ollama-qwen2.5": "Qwen/Qwen2.5-7B-Instruct",
    "ollama-llama3.1": "meta-llama/Llama-3.1-8B-Instruct",
}
METRICS = ("time_to_first_token", "request_latency", "inter_token_latency", "output_token_throughput", "request_throughput")
STATS = ("avg", "p50", "p90", "p99")


def server_url(base_url: str) -> str:
    """AIPerf wants the server address without the /v1 part."""
    return base_url.rstrip("/").removesuffix("/v1")


def build_command(model: str, url: str, tokenizer: str, concurrency: int, request_count: int, artifact_dir: str,
                  input_tokens: int = 200, output_tokens: int = 200) -> list[str]:
    return ["aiperf", "profile", "--model", model, "--url", url, "--endpoint-type", "chat", "--streaming",
            "--tokenizer", tokenizer, "--concurrency", str(concurrency), "--request-count", str(request_count),
            "--synthetic-input-tokens-mean", str(input_tokens), "--output-tokens-mean", str(output_tokens),
            "--artifact-dir", artifact_dir]


def read_export(artifact_dir: str | Path) -> dict:
    """Pull the headline numbers out of AIPerf's profile_export_aiperf.json. Metrics it does not have are left out."""
    files = sorted(Path(artifact_dir).rglob("profile_export_aiperf.json"))
    if not files:
        return {}
    data = json.loads(files[-1].read_text())
    out = {}
    for name in METRICS:
        block = data.get(name)
        if isinstance(block, dict):
            out[name] = {k: block[k] for k in (*STATS, "unit") if k in block}
    return out


def run_aiperf(setup, base_url: str, *, levels=(1, 2, 4), requests_per_level: int = 20, out_root: str = "artifacts/aiperf",
               runner=subprocess.run, which=shutil.which, progress=print) -> dict:
    """Run AIPerf at each concurrency level. `runner` and `which` can be swapped out in tests."""
    if which("aiperf") is None:
        raise RuntimeError("AIPerf is not installed. Run: pip install -r requirements-aiperf.txt (needs Python 3.11 to 3.13).")
    tokenizer = TOKENIZERS.get(setup.name)
    if tokenizer is None:
        raise RuntimeError(f"No tokenizer known for {setup.name}. Add one to TOKENIZERS in aiperf_run.py.")
    url = server_url(base_url)
    rows = []
    for level in levels:
        art = f"{out_root}/CON{level}"
        cmd = build_command(setup.model_id, url, tokenizer, level, requests_per_level, art)
        progress(f"AIPerf at concurrency {level}: {' '.join(cmd)}")
        start = time.perf_counter()
        proc = runner(cmd, check=False)
        rows.append({"concurrency": level, "returncode": proc.returncode, "wall_s": round(time.perf_counter() - start, 1),
                     "metrics": read_export(art) if proc.returncode == 0 else {}})
    try:
        from importlib.metadata import version
        aiperf_version = version("aiperf")
    except Exception:  # noqa: BLE001
        aiperf_version = None
    return {"setup": setup.name, "model": setup.model_id, "url": url, "requests_per_level": requests_per_level,
            "aiperf_version": aiperf_version, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "levels": rows}
