# Benchmark an open model on your own SageMaker endpoint

`make sagemaker-run` starts a SageMaker endpoint serving Qwen 2.5 7B Instruct, runs the tasks against it, and deletes
everything afterwards. It is the one step in this project that bills by the hour while it runs, so read this first.

## What it costs

You pay for the instance for as long as the endpoint exists: about 10 to 15 minutes to start, a few minutes to run, then it is
deleted. Expect roughly 30 to 60 minutes of instance time. Look up the hourly price of the instance type on the
[SageMaker pricing page](https://aws.amazon.com/sagemaker/pricing/) (On-Demand, Real-Time Inference, us-east-1,
`ml.g5.2xlarge`) and do the math before you start. The command prints its own estimate and asks first.

The command deletes the endpoint, its config and its model even if something fails or you press Ctrl+C. Afterwards,
`make sagemaker-status` lists any endpoint still in the account. Anything listed is billing.

## Steps

### 1. Check your quota (the most common blocker)

New AWS accounts often have 0 allowed for GPU endpoints. AWS console, Service Quotas, Amazon SageMaker, search for
`ml.g5.2xlarge for endpoint usage`. If the value is 0, click Request increase and ask for 1. This can take from a few minutes to a day or two.

### 2. Create the role SageMaker runs as

IAM, Roles, Create role. Trusted entity: AWS service, use case SageMaker. Attach the policy `AmazonSageMakerFullAccess`. Name it
something like `sagemaker-run`. Copy its ARN into `.env`:

```
SAGEMAKER_ROLE_ARN=arn:aws:iam::YOUR_ACCOUNT_ID:role/sagemaker-run
SAGEMAKER_INSTANCE_TYPE=ml.g5.2xlarge
SAGEMAKER_HOURLY_USD=<the price you looked up>
```

### 3. Let your `gauge` IAM user manage endpoints

Add this policy to the IAM user you use for `aws configure`. Replace the role ARN with yours.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "sagemaker:CreateModel", "sagemaker:DeleteModel", "sagemaker:DescribeModel",
        "sagemaker:CreateEndpointConfig", "sagemaker:DeleteEndpointConfig", "sagemaker:DescribeEndpointConfig",
        "sagemaker:CreateEndpoint", "sagemaker:DeleteEndpoint", "sagemaker:DescribeEndpoint",
        "sagemaker:ListEndpoints", "sagemaker:InvokeEndpoint"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::YOUR_ACCOUNT_ID:role/sagemaker-run"
    }
  ]
}
```

If the deploy stops with `AccessDenied`, the error names the missing action. Add it and run again. Do not widen it to `*`.

### 4. Install and run

```
pip install -r requirements-sagemaker.txt
make sagemaker-run
make sagemaker-status
```

The status command should print "Nothing is billing".

## What you get

A new run in the database for the setup `sagemaker-qwen2.5-7b`, with accuracy, latency, and a cost per 100 tasks worked out from
the hourly price and the time each call took (startup time is not included). It shows on the dashboard next to the Bedrock setups.
Run `make export` afterwards to refresh `snapshot/run.json`.

## Limits, said plainly

- I could not run this against a real AWS account, so the first run may need a small fix. The code that deletes the endpoint is
  tested against a fake client. The deploy step is not, because it needs real AWS.
- The container may not support tool calling for this model. The command runs one smoke test first and stops (and deletes
  everything) if that fails. If tools do not work, the setup will score low on tool tasks, the same way Llama and Mistral did on Bedrock.
- One instance, one request at a time. The cost per call is exact for the compute used, but it is not a price for serving real traffic.
