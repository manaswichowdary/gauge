"""Create, check and delete a SageMaker endpoint for the benchmark, so it can never be left running by mistake.

`with_endpoint` is the safety net: whatever happens inside it (an error, Ctrl+C), the endpoint, its config and its
model are deleted afterwards. Deleting uses plain boto3 and never needs the SageMaker SDK.
"""
import time
from contextlib import contextmanager

PREFIX = "gauge-"
MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"  # open model, no Hugging Face token needed


def new_endpoint_name() -> str:
    return f"{PREFIX}{time.strftime('%m%d-%H%M%S')}"


def _not_found(exc) -> bool:
    err = getattr(exc, "response", {}).get("Error", {})
    return "Could not find" in err.get("Message", "") or err.get("Code") == "ValidationException"


def list_endpoints(client) -> list[dict]:
    """Every endpoint in the account and region, with its status. Use this to check that nothing is left running."""
    rows = []
    for page in client.get_paginator("list_endpoints").paginate():
        rows += [{"name": e["EndpointName"], "status": e["EndpointStatus"]} for e in page["Endpoints"]]
    return rows


def delete_endpoint_everything(client, name: str) -> list[str]:
    """Delete the endpoint, its endpoint config and its model(s). Safe to run twice. Returns what it deleted."""
    done, config_name, models = [], None, []
    try:
        config_name = client.describe_endpoint(EndpointName=name)["EndpointConfigName"]
        client.delete_endpoint(EndpointName=name)
        done.append(f"endpoint {name}")
    except Exception as exc:  # noqa: BLE001
        if not _not_found(exc):
            raise
    config_name = config_name or name
    try:
        cfg = client.describe_endpoint_config(EndpointConfigName=config_name)
        models = [v["ModelName"] for v in cfg.get("ProductionVariants", [])]
        client.delete_endpoint_config(EndpointConfigName=config_name)
        done.append(f"config {config_name}")
    except Exception as exc:  # noqa: BLE001
        if not _not_found(exc):
            raise
    for model in models or [name]:
        try:
            client.delete_model(ModelName=model)
            done.append(f"model {model}")
        except Exception as exc:  # noqa: BLE001
            if not _not_found(exc):
                raise
    return done


def deploy_endpoint(name: str, role_arn: str, instance_type: str, region: str, profile: str | None = None) -> None:
    """Start an endpoint serving MODEL_ID with the Hugging Face Text Generation Inference container.

    Uses the SageMaker Python SDK version 2 (pip install -r requirements-sagemaker.txt). Takes about 10 to 15 minutes.
    """
    import boto3
    import sagemaker
    from sagemaker.huggingface import HuggingFaceModel, get_huggingface_llm_image_uri

    boto_session = boto3.Session(profile_name=profile, region_name=region) if profile else boto3.Session(region_name=region)
    session = sagemaker.Session(boto_session=boto_session)
    model = HuggingFaceModel(
        role=role_arn,
        image_uri=get_huggingface_llm_image_uri("huggingface", session=session),
        env={"HF_MODEL_ID": MODEL_ID, "SM_NUM_GPUS": "1", "MAX_INPUT_LENGTH": "4096", "MAX_TOTAL_TOKENS": "5000",
             "MESSAGES_API_ENABLED": "true"},
        name=name,
        sagemaker_session=session,
    )
    model.deploy(initial_instance_count=1, instance_type=instance_type, endpoint_name=name,
                 container_startup_health_check_timeout=900)


@contextmanager
def with_endpoint(deploy, delete, name: str):
    """Deploy, hand control back, and always delete afterwards, even on an error or Ctrl+C."""
    try:
        deploy(name)
        yield name
    finally:
        delete(name)
