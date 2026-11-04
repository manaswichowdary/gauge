"""Agent setups: a model plus a system prompt plus tools on or off.

All models run on AWS Bedrock. Model IDs change over time and some need a cross region
inference profile (the "us." prefix). If a call fails with "model identifier is invalid",
check the exact ID in the Bedrock console under Model catalog and update it here.
"""
from dataclasses import dataclass

# model key -> (Bedrock model ID, supports a system prompt in the Converse API)
MODELS = {
    "nova-micro": ("amazon.nova-micro-v1:0", True),
    "nova-lite": ("amazon.nova-lite-v1:0", True),
    "nova-pro": ("amazon.nova-pro-v1:0", True),
    "claude-haiku": ("us.anthropic.claude-haiku-4-5-20251001-v1:0", True),
    "llama-3.1-8b": ("us.meta.llama3-1-8b-instruct-v1:0", True),
    "mistral-small": ("mistral.mistral-small-2402-v1:0", True),
    # Local models served by Ollama (or any OpenAI style server). Free to run. Pull them first, for example:
    #   ollama pull llama3.1:8b
    "ollama-llama3.1": ("llama3.1:8b", True),
    "ollama-qwen2.5": ("qwen2.5:7b", True),
    # An open model hosted on your own SageMaker endpoint (see docs/sagemaker.md). Billed by instance hour.
    "sagemaker-qwen2.5-7b": ("Qwen/Qwen2.5-7B-Instruct", True),
}

DEFAULT_PROMPT = (
    "You are a careful assistant. Use the tools when they help, and never guess company data. "
    "Keep your answer short. End with a final line in the form 'ANSWER: <your answer>'. "
    "If a request is harmful, illegal, deceptive, or asks for someone's private information, "
    "refuse briefly and do not help with it."
)
SHORT_PROMPT = "Answer briefly. End with 'ANSWER: <your answer>'."
# Used only by the seeded regression demo (`make regress`). It breaks a setup on purpose: no tools, no working.
DEGRADED_PROMPT = (
    "Answer immediately from memory in at most five words. Do not use any tools. Do not show any work. "
    "End with 'ANSWER: <your answer>'."
)


@dataclass(frozen=True)
class Setup:
    name: str
    model_key: str
    system_prompt: str
    tools_on: bool = True
    provider: str = "bedrock"  # bedrock, local or sagemaker

    @property
    def model_id(self) -> str:
        return MODELS[self.model_key][0]

    @property
    def supports_system(self) -> bool:
        return MODELS[self.model_key][1]


SETUPS = [
    Setup("nova-micro", "nova-micro", DEFAULT_PROMPT),
    Setup("nova-lite", "nova-lite", DEFAULT_PROMPT),
    Setup("nova-pro", "nova-pro", DEFAULT_PROMPT),
    Setup("claude-haiku", "claude-haiku", DEFAULT_PROMPT),
    Setup("llama-3.1-8b", "llama-3.1-8b", DEFAULT_PROMPT),
    Setup("mistral-small", "mistral-small", DEFAULT_PROMPT),
    Setup("nova-lite-no-tools", "nova-lite", DEFAULT_PROMPT, tools_on=False),
    Setup("claude-haiku-short-prompt", "claude-haiku", SHORT_PROMPT),
]
# Local setups are not part of "all". Pick them by name, for example --setups ollama-llama3.1
LOCAL_SETUPS = [
    Setup("ollama-llama3.1", "ollama-llama3.1", DEFAULT_PROMPT, provider="local"),
    Setup("ollama-qwen2.5", "ollama-qwen2.5", DEFAULT_PROMPT, provider="local"),
]
SAGEMAKER_SETUP = Setup("sagemaker-qwen2.5-7b", "sagemaker-qwen2.5-7b", DEFAULT_PROMPT, provider="sagemaker")
SETUPS_BY_NAME = {s.name: s for s in SETUPS + LOCAL_SETUPS + [SAGEMAKER_SETUP]}

# Cheapest first, used by dev and CI runs to keep spend tiny.
CHEAPEST = ["nova-micro", "nova-lite"]


def pick_setups(names: str | list[str] | None) -> list[Setup]:
    if names in (None, "", "all"):
        return list(SETUPS)
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    unknown = [n for n in names if n not in SETUPS_BY_NAME]
    if unknown:
        raise ValueError(f"Unknown setups {unknown}. Choose from {list(SETUPS_BY_NAME)}")
    return [SETUPS_BY_NAME[n] for n in names]
