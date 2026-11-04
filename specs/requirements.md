# Requirements

Written in the Kiro spec format: user stories with acceptance criteria in the form "WHEN ... THEN the system SHALL ...".
These describe what the harness does today. Each one is covered by a test or a command you can run.

## 1. Compare agent setups on the same tasks

**User story:** As someone choosing a model and prompt for an agent, I want to run the same tasks against several
setups, so that I can compare them fairly.

1. WHEN a run starts THEN the system SHALL load every task from `tasks/` and reject a task with a bad category, a missing
   expected answer or a duplicate id.
2. WHEN a setup is run THEN the system SHALL give it the same task prompt, the same tools (or none, for a no tools setup) and
   the same output limit as every other setup.
3. WHEN a run finishes THEN the system SHALL store every answer with its score, latency, token counts and cost.

## 2. Score answers the same way every time

**User story:** As a reviewer, I want scoring I can explain, so that a number on the dashboard means something.

1. WHEN a task has one correct value THEN the system SHALL score it with rules (exact, number, contains, refuse) and not with a model.
2. WHEN an answer is a number THEN the system SHALL accept rounding to the stated precision (relative tolerance 0.02%) and reject a wrong digit.
3. WHEN a task is an over refusal task THEN the system SHALL fail a model that refuses.
4. WHEN a model hits the tool step limit or sends an invalid tool name THEN the system SHALL count it as a wrong answer.
5. WHEN a call fails because of throttling or an outage THEN the system SHALL leave it out of accuracy and report it as lost to errors.

## 3. Report speed and cost honestly

1. WHEN results are summarized THEN the system SHALL report accuracy, p50 and p95 latency and cost per 100 tasks for each setup.
2. WHEN a call was retried after throttling THEN the system SHALL keep the waiting time out of its latency.
3. WHEN a repeat of a task disagrees with another repeat THEN the system SHALL list the task as unstable.

## 4. Control spend

1. WHEN a real run is about to start THEN the system SHALL print an estimated cost and ask for confirmation.
2. WHEN total cost passes the budget cap THEN the system SHALL stop starting new calls and record why.
3. WHEN the same setup, prompt, task and repeat were already answered THEN the system SHALL reuse the stored answer at no cost.

## 5. Check how far the LLM judge can be trusted

1. WHEN I label outputs by hand THEN the system SHALL store my label next to the rule score and the judge score.
2. WHEN labels exist THEN the system SHALL show percent agreement and Cohen's kappa for the rules and for the judge.

## 6. Catch regressions

1. WHEN a baseline exists and a setup's accuracy falls by more than 5 points THEN the gate SHALL fail and name the setup.
2. WHEN I run the seeded regression demo THEN the system SHALL run a setup with a deliberately bad prompt, run the gate on it,
   and report the drop, whether the gate caught it, and how long that took.
3. WHEN the weekly job or a pull request that changes prompts or tasks runs THEN the system SHALL run the cheapest models on all
   tasks and fail the job if the gate fails.

## 7. Measure behavior under load

1. WHEN I run the load test THEN the system SHALL send the same number of requests at each concurrency level and report p50, p95,
   throughput, retries and errors for each level.
2. WHEN a level has errors, retries or a much slower p95 THEN the system SHALL name it as the first level that degraded.

## 8. Run on more than one provider

1. WHEN a setup is marked local THEN the system SHALL send it to a local OpenAI style server and use the same tool loop and step limit as Bedrock.
2. WHEN a run uses only local setups THEN the system SHALL make no AWS calls and ask for no cost confirmation.

## 9. Share results safely

1. WHEN I export a run THEN the system SHALL write a snapshot with no keys and no prompts.
2. WHEN no database is configured THEN the dashboard SHALL show the snapshot read only and SHALL NOT be able to call a model.

## 10. Load test local models with AIPerf

1. WHEN I run the AIPerf command on a local setup THEN the system SHALL run AIPerf at each concurrency level and save the
   time to first token, request latency and throughput numbers it reports.
2. WHEN AIPerf is not installed or fails THEN the system SHALL say so and SHALL NOT write made up numbers.

## 11. Benchmark a hosted open model on SageMaker

1. WHEN I start a SageMaker run THEN the system SHALL print an estimated cost and ask before creating anything.
2. WHEN the run finishes, fails or is interrupted THEN the system SHALL delete the endpoint, its config and its model, then check that it is gone.
3. WHEN the first test request fails THEN the system SHALL stop without running the tasks.
4. WHEN calls are billed by the hour THEN the system SHALL report each call's cost from the hourly price and the call time.
