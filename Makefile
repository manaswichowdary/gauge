PY ?= python

up:            ## start local Postgres
	docker compose up -d
down:
	docker compose down
test:          ## offline tests, free
	PYTHONPATH=. $(PY) -m pytest -q
test-postgres: ## same tests on the local Postgres (run `make up` first)
	PYTHONPATH=. TEST_DATABASE_URL=postgresql+psycopg2://gauge:gauge@localhost:5432/gauge $(PY) -m pytest -q
run-dev:       ## tiny real run: 2 cheapest models, 8 tasks (cents)
	$(PY) -m gauge.cli run --mode dev --judge
run:           ## full real run: all 8 Bedrock setups, 64 tasks, 2 repeats (asks first)
	$(PY) -m gauge.cli run --mode full --repeats 2 --judge
run-local:     ## free: run a local Ollama model on all tasks (needs `ollama serve` and the model pulled)
	$(PY) -m gauge.cli run --mode full --setups ollama-llama3.1 --repeats 1
run-ci:        ## small CI run (needs AWS access)
	$(PY) -m gauge.cli run --mode ci --yes
run-scheduled: ## what the weekly job runs: 2 cheapest models, all tasks, 1 repeat (about a cent)
	$(PY) -m gauge.cli run --mode scheduled --yes
dry-run:       ## fake model, free, numbers mean nothing
	$(PY) -m gauge.cli run --mode full --provider fake --judge --yes
report:
	$(PY) -m gauge.cli report
baseline:      ## save current accuracy as the baseline for the full runs
	$(PY) -m gauge.cli gate --save-baseline
baseline-ci:   ## save the latest scheduled run as baseline-ci.json (run `make run-scheduled` first)
	$(PY) -m gauge.cli gate --save-baseline --baseline baseline-ci.json --mode scheduled
gate:
	$(PY) -m gauge.cli gate
gate-ci:       ## what the weekly job checks
	$(PY) -m gauge.cli gate --baseline baseline-ci.json --mode scheduled
aiperf:        ## NVIDIA AIPerf on a local model (free; needs Ollama and `pip install -r requirements-aiperf.txt`)
	$(PY) -m gauge.cli aiperf
sagemaker-run: ## start a SageMaker endpoint, benchmark it, delete it (costs credits, asks first)
	$(PY) -m gauge.cli sagemaker-run
sagemaker-status: ## list SageMaker endpoints (anything listed is billing)
	$(PY) -m gauge.cli sagemaker-status
sagemaker-down: ## delete one endpoint: make sagemaker-down NAME=gauge-1103-120000
	$(PY) -m gauge.cli sagemaker-down --name $(NAME)
regress:       ## break nova-lite on purpose and check the gate catches it (about 2 cents)
	$(PY) -m gauge.cli regress --setup nova-lite
load:          ## load test: 1, 4, 8, 16 concurrent requests on nova-lite (about 2 cents)
	$(PY) -m gauge.cli load --setup nova-lite
export:        ## save the latest run to snapshot/run.json (for the read only deploy)
	$(PY) -m gauge.cli export
dashboard:
	PYTHONPATH=. streamlit run app/dashboard.py
dashboard-demo: ## the read only dashboard, from snapshot/run.json, no database needed
	GAUGE_READ_ONLY=1 PYTHONPATH=. streamlit run app/dashboard.py
label:
	PYTHONPATH=. streamlit run app/label_app.py
