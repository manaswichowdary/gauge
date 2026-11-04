"""Settings for Gauge, read from environment variables (and a .env file)."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    aws_region: str
    aws_profile: str | None
    s3_bucket: str | None
    save_raw_to_s3: bool
    max_concurrency: int
    max_output_tokens: int
    max_tool_steps: int
    request_timeout_s: int
    budget_cap_usd: float
    judge_model: str
    local_base_url: str = "http://localhost:11434/v1"
    sagemaker_endpoint: str | None = None
    sagemaker_role_arn: str | None = None
    sagemaker_instance_type: str = "ml.g5.2xlarge"
    sagemaker_hourly_usd: float = 0.0


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load settings. Fails early with a clear message instead of deep inside a run."""
    if env_file:
        load_dotenv(env_file, override=False)

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ConfigError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill it in "
            "(make up starts a local Postgres that matches the example value)."
        )

    save_raw = _bool(os.getenv("SAVE_RAW_TO_S3", "false"))
    bucket = os.getenv("S3_BUCKET") or None
    if save_raw and not bucket:
        raise ConfigError("SAVE_RAW_TO_S3 is true but S3_BUCKET is empty.")

    return Settings(
        database_url=database_url,
        aws_region=os.getenv("AWS_REGION", "us-east-1"),
        aws_profile=os.getenv("AWS_PROFILE") or None,
        s3_bucket=bucket,
        save_raw_to_s3=save_raw,
        max_concurrency=int(os.getenv("MAX_CONCURRENCY", "4")),
        max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "400")),
        max_tool_steps=int(os.getenv("MAX_TOOL_STEPS", "8")),
        request_timeout_s=int(os.getenv("REQUEST_TIMEOUT_S", "60")),
        budget_cap_usd=float(os.getenv("BUDGET_CAP_USD", "3.0")),
        # The judge only grades open answers, so the cheapest model is enough.
        judge_model=os.getenv("JUDGE_MODEL", "nova-lite"),
        local_base_url=os.getenv("LOCAL_BASE_URL", "http://localhost:11434/v1"),
        sagemaker_endpoint=os.getenv("SAGEMAKER_ENDPOINT_NAME") or None,
        sagemaker_role_arn=os.getenv("SAGEMAKER_ROLE_ARN") or None,
        sagemaker_instance_type=os.getenv("SAGEMAKER_INSTANCE_TYPE", "ml.g5.2xlarge"),
        sagemaker_hourly_usd=float(os.getenv("SAGEMAKER_HOURLY_USD", "0") or 0),
    )
