# Tasks

Checked means built and covered by a test or a command you can run. Unchecked means it needs you to do something outside the code.

## Core
- [x] Task format, loader and validation (64 tasks in two tiers)
- [x] Setups, Bedrock provider with backoff and a tool loop
- [x] Rule scorers, with tests that recompute every harder answer from the tool data
- [x] Runner with threads, result cache and budget cap
- [x] SQLite and Postgres storage, tests on both
- [x] Cost and latency summary, unstable tasks
- [x] LLM judge, labeling app, agreement and kappa
- [x] Dashboard, markdown report, read only snapshot mode

## Catching regressions
- [x] Baseline file and gate (fails on a drop of more than 5 points)
- [x] Seeded regression demo (`make regress`)
- [x] Scheduled and pull request eval workflow (`.github/workflows/eval.yml`)
- [ ] Set up the AWS role and GitHub variable (`docs/aws-github-setup.md`)
- [ ] Save `baseline-ci.json` from a real run (`make run-scheduled`, `make baseline-ci`)

## Performance
- [x] Load test with concurrency levels (`make load`)
- [ ] Run it once on a real account and keep `snapshot/load.json`

## Providers
- [x] Local OpenAI style provider with the same tool loop and step limit
- [ ] Run a local model once (`make run-local`) if you have Ollama installed

- [x] AIPerf wrapper and dashboard section (`make aiperf`)
- [ ] Run AIPerf once against a local model and keep `snapshot/aiperf.json`
- [x] SageMaker provider, deploy and delete helpers, one command with automatic cleanup
- [ ] Check the SageMaker quota, create the role, run `make sagemaker-run` once, confirm `make sagemaker-status` is empty

## Shipping
- [x] CI on every push: tests on SQLite and on Postgres 16
- [ ] Confirm CI is green on GitHub
- [ ] Deploy the read only dashboard
