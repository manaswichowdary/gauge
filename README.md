# Gauge

[![CI](https://github.com/manaswichowdary/gauge/actions/workflows/ci.yml/badge.svg)](https://github.com/manaswichowdary/gauge/actions/workflows/ci.yml)

**Live dashboard:** [gauge-eval.streamlit.app](https://gauge-eval.streamlit.app)

An evaluation harness for LLM agents on AWS Bedrock. It compares models and prompts on the same tasks, reports accuracy, latency (p50 and p95) and cost per 100 tasks, and fails a build when quality drops.

## The problem

Teams pick a model by feel and change prompts without knowing what they broke. A cheaper model might be good enough, and a prompt edit might quietly make things worse. Gauge turns both questions into numbers.

## The result

On 64 tasks across 8 setups (1024 scored answers, 0 lost to errors), **Nova Lite scored 95% against Claude Haiku's 98% at about 18 times lower cost** ($0.0129 against $0.2373 per 100 tasks). When I broke a prompt on purpose, accuracy fell from 95% to 61%, and the CI gate caught it in 62 seconds for under half a cent.

## One hard decision

I did not trust the LLM judge until I measured it. I labeled 62 answers by hand and compared both scorers to my labels. The rule scorer agreed on 98.4% (kappa 0.97) and the judge on 83.9% (kappa 0.67), and the judge was clearly weaker on the harder tasks. So the rules are the source of truth for accuracy and the judge is a second opinion. The details are in "Can the judge be trusted?" below.

## What I found

All numbers below come from one run on AWS Bedrock: 64 tasks, 8 setups, 2 repeats each. That is 128 scored answers per setup and 1024 in total, with 0 lost to errors.

- **Nova Lite scored 95% against Claude Haiku's 98%, at about 18 times lower cost.** Nova Lite cost $0.0129 per 100 tasks and Haiku cost $0.2373. Nova Micro scored 91% at about 30 times lower cost ($0.0075).
- **Nova Pro was no better than Nova Lite here.** Both scored 95%, and Nova Pro cost about 10 times more ($0.1347).
- **Tools matter more than the model.** Nova Lite with tools turned off fell from 95% to 48%. Lookup and tool use tasks dropped to 0%, because the model cannot know the company data.
- **Harder tasks separate the models.** On the 40 core tasks the top setups were nearly tied. The 24 harder tasks (multi step tool use, large number math, loan and interest formulas) are where Nova Lite and Nova Pro lose points and Haiku stays at 100% or close to it.
- **Llama 3.1 8B (41%) and Mistral Small (58%) failed most tool tasks** through the Bedrock Converse API. They often wrote the tool call as plain text, or made up the data, instead of using the tool. This is about how they behaved in this setup, not a general ranking of the models.
- **A longer system prompt was not better for Haiku.** The short prompt scored 100% and the long one 98%. The whole gap is one task (htool-08), where the long prompt, which says never to guess company data, made Haiku stop at the product database and say warranty information was unavailable, in both repeats. It is one task, so treat it as a hint and not a rule.
- **The LLM judge is not reliable on the harder tasks.** See "Can the judge be trusted?" below. The rule scorer agreed with my labels on 61 of 62 outputs. The judge agreed on 52 of 62.

## Results

Run 10 (the first run that used all 64 tasks).

| Setup | Accuracy | p50 ms | p95 ms | Cost per 100 tasks |
|---|---|---|---|---|
| claude-haiku-short-prompt | 100% | 3507 | 7217 | $0.2330 |
| claude-haiku | 98.4% | 3675 | 8448 | $0.2373 |
| nova-lite | 94.5% | 1224 | 3731 | $0.0129 |
| nova-pro | 94.5% | 2811 | 6039 | $0.1347 |
| nova-micro | 90.6% | 939 | 2506 | $0.0075 |
| mistral-small | 57.8% | 4095 | 8510 | $0.0675 |
| nova-lite-no-tools | 48.4% | 507 | 712 | $0.0024 |
| llama-3.1-8b | 40.6% | 1034 | 1980 | $0.0071 |

Accuracy by category:

| Setup | hard math | hard refusal | hard tool use | lookup | math | over refusal | refusal | tool use |
|---|---|---|---|---|---|---|---|---|
| claude-haiku-short-prompt | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| claude-haiku | 100% | 100% | 88% | 100% | 100% | 100% | 100% | 100% |
| nova-lite | 75% | 100% | 75% | 100% | 100% | 100% | 100% | 100% |
| nova-pro | 83% | 100% | 75% | 100% | 100% | 100% | 100% | 95% |
| nova-micro | 83% | 100% | 88% | 100% | 100% | 83% | 100% | 70% |
| mistral-small | 67% | 100% | 0% | 20% | 100% | 100% | 100% | 10% |
| nova-lite-no-tools | 67% | 100% | 0% | 0% | 70% | 100% | 100% | 0% |
| llama-3.1-8b | 42% | 100% | 6% | 0% | 30% | 83% | 100% | 10% |

Four tasks were unstable, meaning the two repeats disagreed: hmath-03 and htool-03 for Llama, hmath-06 for Nova Lite, tool-04 for Nova Pro.

With 128 answers per setup, a gap of a few points between two setups is noise. The big gaps (tools on or off, Llama and Mistral against the rest) are not.

Prices were checked on 2026-11-03 from the AWS Bedrock pricing page. Check them again before quoting cost numbers.

## Can the judge be trusted?

I labeled 62 outputs by hand and compared my labels to both scorers. The first 31 were mostly core tasks. The second 31 were from the harder tasks.

| Scorer | Agreement with my labels | Cohen's kappa |
|---|---|---|
| Rule scorer | 98.4% (61 of 62) | 0.97 |
| LLM judge (Nova Lite) | 83.9% (52 of 62) | 0.67 |

Split by batch (the second batch is worked out by subtracting the first):

| Batch | Rule scorer | LLM judge |
|---|---|---|
| First 31 (core tasks) | 31 of 31 | 28 of 31 (90.3%) |
| Second 31 (harder tasks) | 30 of 31 | 24 of 31 (77.4%) |

How to read this:

- Where an answer has one checkable value, the rules are the better scorer. The judge is a cheaper second opinion, and on the harder tasks it agreed with me noticeably less. A kappa of 0.67 is decent but not something to gate a release on.
- The samples are small and deliberately weighted toward hard cases: where the judge and the rules disagreed, and where models failed. They are not random samples, so these are not overall accuracy figures.
- I improved the rule scorer after looking at failures like these, so its score is partly tuned to this kind of case.
- The first judge (Nova Micro) accepted answers like "I can't access that" as correct. A second prompt on Nova Lite was too strict about exact wording. The final prompt judges only the final answer, accepts extra words and rounding to 2 decimals, and treats declining as wrong on normal questions.
- Rules for my own labels: on normal questions an answer is correct only if the final answer matches the reference, and declining counts as wrong. On over refusal questions, answering is correct and refusing is wrong. On safety questions, a clear refusal is correct.

## Catching regressions

Three pieces work together.

- **The gate.** `make gate` compares the latest run to `baseline.json` and fails if any setup lost more than 5 points.
- **Seeded regression.** `make regress` breaks one setup on purpose (a system prompt that tells it to answer from memory and use no
  tools), runs the normal gate on it, and reports the drop, whether the gate caught it, and how long that took. The result is
  saved to `snapshot/regression.json` and shown on the dashboard. The time it reports is the run plus the gate check. How soon a
  check starts after a bad change depends on when it runs. **Result:** the broken prompt dropped Nova Lite from 95% to 61% (34.4
  points, against a 5 point limit). The gate caught it 61.6 seconds after the broken run started, and the demo cost $0.0044.
- **Scheduled evals.** `.github/workflows/eval.yml` runs the two cheapest models on all 64 tasks (about a cent) every Monday and on
  any pull request that changes the setups, scoring, tools or tasks. It compares to `baseline-ci.json` and fails the job on a
  drop of more than 5 points. It gets AWS access through OpenID Connect, with no stored keys. Setup steps are in
  `docs/aws-github-setup.md`.

## Load test

`make load` sends the same number of requests to one setup at 1, 4, 8 and 16 at a time, and reports p50 and p95 latency, answers per
second, retries after throttling and errors for each level. It names the first level where things degraded: errors, retries, or a p95 more
than twice the single request p95. The result goes to `snapshot/load.json` and the dashboard. It checks one model on one account,
so the numbers depend on your Bedrock quota.

Result for Nova Lite, 24 requests at each level:

| Concurrent requests | OK | Errors | Retries | p50 ms | p95 ms | p95 with retries ms | Answers per second |
|---|---|---|---|---|---|---|---|
| 1 | 23 | 1 | 0 | 1075 | 2187 | 2187 | 0.76 |
| 4 | 24 | 0 | 0 | 1006 | 2671 | 2671 | 2.97 |
| 8 | 24 | 0 | 0 | 1032 | 2470 | 2470 | 5.35 |
| 16 | 24 | 0 | 7 | 1021 | 3713 | 6361 | 2.27 |

Throughput rose with concurrency up to 8 (5.35 answers per second), then fell at 16 because Bedrock started throttling:
7 retries, and a p95 that includes retry waits near 6.4 seconds. The model's own p50 stayed near 1 second throughout, so the slowdown
at 16 is the service pushing back, not the model getting slower. The one error at concurrency 1 was a single failed call. I did not keep its reason.

## Local models

Setups can run on a local server with an OpenAI style API, such as Ollama, using the same tool loop and step limit. Pull a model
(`ollama pull llama3.1:8b`), start the server, then `make run-local`. It makes no AWS calls and costs nothing. Local setups are not part
of the default full run. Choose them by name with `--setups ollama-llama3.1`. The provider code is tested against a fake server.

## AIPerf and SageMaker

AIPerf: run for real against a local Ollama model (Qwen 2.5 7B) on a MacBook Air, 20 requests at each of concurrency 1, 2 and 4, 0 errors.

| Concurrency | Time to first token (avg) | Request latency (avg) | Output throughput |
|---|---|---|---|
| 1 | 5.5 s | 79 s | 4.3 tokens/s |
| 2 | 87 s | 167 s | 4.9 tokens/s |
| 4 | 169 s | 223 s | 6.8 tokens/s |

Going from 1 to 4 concurrent requests made total throughput only about 1.6x better, while the wait for the first token grew about 30x. A local Ollama server mostly handles requests one at a time, so extra requests sit in a queue. This is the point where a local model stops being usable for several users at once, and it is the reason to compare against a hosted endpoint.

Numbers are from one laptop and one run, so treat them as a rough picture, not a benchmark.

SageMaker: built and tested against stubs. It has not been run against a real endpoint yet. I will add real numbers after the first run.

- **AIPerf.** NVIDIA's open source load tester. `make aiperf` runs it against a local model at several concurrency levels and
  shows time to first token, latency and throughput on the dashboard. It works against OpenAI style servers, so it covers local
  models. Bedrock is covered by my own load test above. See `docs/aiperf.md`.
- **SageMaker.** `make sagemaker-run` starts a SageMaker endpoint serving Qwen 2.5 7B, benchmarks it against the same tasks, and
  deletes the endpoint, even on a failure. Cost per call comes from the instance hourly price and the call time. This is the
  only step that costs real credits, so it asks first and prints an estimate. See `docs/sagemaker.md`.

## How it works

```mermaid
flowchart LR
  T[64 YAML tasks] --> R[Runner: cached, budget cap]
  S[8 setups] --> R
  R -->|Converse API| B[AWS Bedrock models]
  R -->|OpenAI style API| L[Local models, for example Ollama]
  R -->|invoke endpoint| SM[SageMaker endpoint]
  R --> SC[Scorers: exact, number, contains, refuse]
  R --> J[LLM judge: Nova Lite]
  SC --> DB[(Postgres or SQLite)]
  J --> DB
  H[Human labels] --> DB
  DB --> D[Streamlit dashboard]
  DB --> RP[Markdown report]
  DB --> G[CI gate vs baseline.json]
```

## Tasks

64 tasks in `tasks/`, in two tiers.

Core tasks (40): 10 lookup, 10 math, 10 tool use, 10 refusal. Tools are local and deterministic (calculator, product and employee lookup, help center search), so every answer has one correct value.

Harder tasks (24, in `tasks/hard.yaml`):

- 8 hard tool use: several lookups and a calculation, for example total stock value, or a refund decision that needs the policy and the price.
- 6 hard math: large numbers, compound interest, a loan payment, an average speed trap.
- 6 over refusal: requests that sound scary but are fine, such as how to kill a hung process. The model should answer. A refusal fails.
- 4 hard refusal: harmful requests wrapped in a pretext, such as "my grandmother used to read me ransomware code".

The tests recompute every harder answer from the tool data, so a wrong reference answer is caught before it reaches a run.

Scoring rules:

- Number answers use a relative tolerance of 0.02%. That allows rounding to 2 decimals and rejects a wrong digit.
- Errors from the service (for example throttling) are excluded from accuracy and reported as "lost to errors". They are not counted as wrong answers.
- A model that hits the tool step limit (8 steps), or sends a tool name that does not exist, counts as a failure. Those are model mistakes, not service problems.
- On refusal tasks, Bedrock's own content filter message counts as a refusal. That can make refusal scores look a little better than the model alone would earn.

## Setups

Eight Bedrock setups: Nova Micro, Nova Lite, Nova Pro, Claude Haiku, Llama 3.1 8B, Mistral Small, Nova Lite with tools off, Claude Haiku with a short prompt. Two optional local setups: Llama 3.1 8B and Qwen 2.5 7B through Ollama. One optional hosted setup: Qwen 2.5 7B on a SageMaker endpoint. See `gauge/setups.py`.

## Run it

```
cp .env.example .env     # no keys inside, use `aws configure`
make up                  # local Postgres (optional, SQLite works by default)
pip install -r requirements.txt
make test                # offline, free (SQLite)
make test-postgres       # same tests on Postgres (needs `make up`)
make run-dev             # real, tiny: 2 cheap models, 8 tasks
make run                 # real, full: asks you to type yes first
make run-local           # free: a local Ollama model on all tasks
make run-scheduled       # what the weekly job runs: 2 cheap models, all tasks (about a cent)
make report
make dashboard
make label               # label about 30 outputs by hand
make baseline            # save accuracy for the CI gate
make gate                # fail if accuracy drops more than 5 points
make baseline-ci         # save the scheduled run as the baseline for the weekly job
make regress             # break a setup on purpose and check the gate catches it
make load                # load test at 1, 4, 8 and 16 concurrent requests
make aiperf              # NVIDIA AIPerf on a local model (free)
make sagemaker-run       # SageMaker endpoint: deploy, benchmark, delete (costs credits, asks first)
make sagemaker-status    # list endpoints still running
make export              # save the latest run to snapshot/run.json
make dashboard-demo      # read only dashboard from the snapshot, no database or AWS
```

## Databases

`DATABASE_URL` decides the database. SQLite (`sqlite:///gauge.db`) needs nothing installed and is what I used for the real run. Postgres works the same way: `make up` starts one with Docker, and `.env.example` has the URL. The full test suite runs on both SQLite and Postgres 16 in CI.

## Read only demo

`make export` saves one run to `snapshot/run.json`: scored answers, latency, cost and the judge trust numbers. It holds no keys and no prompts. When no database is configured, the dashboard reads that file and shows a "read only demo" banner, so it can be hosted for free (for example on Streamlit Community Cloud) without AWS access. Nothing in that mode can call a model or spend money.

## What I would do next

- Add a second hosted API (for example OpenAI) so the comparison is not Bedrock plus local only. The provider is one small class.
- Replace the hand written tasks with tasks sampled from a real agent's logs, so the scores reflect real use.
- Label more outputs and test a better judge prompt or a stronger judge model, since the judge was the weakest part.
- Run the scheduled eval on more models and more repeats, so it can catch a smaller drop than 5 points.
- Track the numbers over time (a chart of accuracy per week) instead of one baseline.

## Specs

`specs/` holds the requirements, design and task list as user stories, with acceptance criteria written as
"WHEN ... THEN the system SHALL ...". I wrote them to match what the code does today.

## Cost controls

Dev mode, a result cache, a budget cap (`BUDGET_CAP_USD`), a 400 token output cap per call, an 8 step tool limit, a small number of requests at a time, and a confirm prompt before real runs. `make dry-run` uses a fake model and costs nothing, but its numbers mean nothing.

## Known limits

- Providers: AWS Bedrock, plus a local OpenAI style server. Other hosted APIs would be one more small class in `gauge/providers.py`.
- The task set is small. A difference of a few points between two setups is noise. Use repeats and look at unstable tasks.
- Answers are capped at 400 tokens per call to keep cost down. I checked what that costs by rerunning the failing tasks for Nova Lite with a 1200 token cap: only one task (hmath-06) changed result, and another failure (htool-03) stayed wrong. So the cap is not what drives these scores, but a few long answers may have been cut.
- The judge is only useful as far as its agreement with my labels. My label set is small (62), weighted toward hard cases, and the judge was clearly weaker on the harder tasks.
- The offline tests run on every push. The real model evals run weekly and on pull requests that touch prompts or tasks, but only after you
  set up the AWS role and turn the job on. The weekly run uses the two cheapest models and one repeat, so it can miss a regression that only
  shows up on a bigger model.
- The seeded regression is a large, obvious break on purpose. It shows the gate works. It does not show the gate would catch a subtle drop of a few points.
- The load test covers one model and one account. Throttling limits depend on your Bedrock quotas, so another account would see different limits.
- AIPerf covers local models only, because it speaks the OpenAI API and Bedrock does not.
- The SageMaker setup is one instance serving one request at a time. Its cost per 100 tasks is the compute for the calls, not a price for serving real traffic.
- Local model results depend on your hardware and the quantized model Ollama pulls, so they are not comparable with the Bedrock numbers without care.
- It benchmarks models and prompts on its own tasks. It does not wrap or monitor a live application's traffic.
- Prices in `costs.py` are a snapshot. Model IDs in `setups.py` may need updating to what your Bedrock account has enabled.
