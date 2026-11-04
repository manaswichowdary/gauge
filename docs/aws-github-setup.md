# Run the scheduled eval from GitHub without any stored keys

The weekly eval (`.github/workflows/eval.yml`) gets short lived AWS access from GitHub through OpenID Connect.
No access key is saved anywhere. Do these steps once. Replace `YOUR_GITHUB_USER` and `YOUR_REPO` with your own.

## 1. Tell AWS to trust GitHub (once per AWS account)

AWS console, IAM, Identity providers, Add provider.

- Provider type: OpenID Connect
- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

## 2. Create the role

IAM, Roles, Create role, Custom trust policy. Paste this trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::YOUR_ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": [
            "repo:YOUR_GITHUB_USER/YOUR_REPO:ref:refs/heads/main",
            "repo:YOUR_GITHUB_USER/YOUR_REPO:pull_request"
          ]
        }
      }
    }
  ]
}
```

The `sub` condition is what keeps other repos from using the role. Do not loosen it to `*`.

Name the role something like `eval-github`. Attach this permission policy (Bedrock calls only, nothing else). The Converse API is allowed by the
`bedrock:InvokeModel` permission:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "*"
    }
  ]
}
```

Copy the role ARN. It looks like `arn:aws:iam::123456789012:role/eval-github`.

## 3. Tell GitHub

Repo, Settings, Secrets and variables, Actions.

- Secrets tab, New repository secret: name `AWS_ROLE_ARN`, value is the role ARN.
- Variables tab, New repository variable: name `SCHEDULED_EVAL_ENABLED`, value `true`.

The ARN is not a password, but keeping it as a secret keeps your account number out of the logs.

## 4. Save the baseline the weekly job compares against

On your laptop:

```
make run-scheduled
make baseline-ci
```

Commit `baseline-ci.json`. The weekly job runs the same two models on the same 64 tasks and fails if either
drops more than 5 points. Re run `make run-scheduled` and `make baseline-ci` whenever you change prompts or tasks on purpose.

## 5. Try it

Repo, Actions, eval, Run workflow. It should finish in a few minutes and cost about a cent. Open the run to see the
report that was saved.

## Cost and safety

- One run is about a cent. The budget cap in the workflow is $0.50, so a bug cannot spend more.
- Set an AWS Budget alert of a few dollars anyway (Billing, Budgets).
- To turn the job off, set the `SCHEDULED_EVAL_ENABLED` variable to `false`.
