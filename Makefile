# Everything runs through uv against uv.lock. A PYTHONPATH from the calling
# shell can shadow the virtualenv's packages, so it is dropped.
unexport PYTHONPATH
.DEFAULT_GOAL := help

UV ?= uv
RUN := $(UV) run --frozen
SCENARIO ?= bad-deploy-error-spike
KIND_CTX := --context kind-aiops-triage

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Create the virtualenv from uv.lock (dev tools included)
	$(UV) sync --frozen

.PHONY: lint
lint: ## ruff lint + format check
	$(RUN) ruff check src tests evals
	$(RUN) ruff format --check src tests evals

.PHONY: fmt
fmt: ## Auto-format and apply safe lint fixes
	$(RUN) ruff format src tests evals
	$(RUN) ruff check --fix src tests evals

.PHONY: typecheck
typecheck: ## mypy --strict on the package
	$(RUN) mypy

.PHONY: test
test: ## Unit and integration tests (offline)
	$(RUN) pytest

.PHONY: eval
eval: ## Offline eval with the FakeLLM; writes evals/reports/offline.md
	$(RUN) python evals/run_evals.py --min-accuracy 0.9

.PHONY: eval-live
eval-live: ## Eval against Claude (needs ANTHROPIC_API_KEY; costs money)
	$(RUN) python evals/run_evals.py --live

.PHONY: demo
demo: ## Triage a recorded scenario end to end, offline (SCENARIO=...)
	$(RUN) triage-agent demo --scenario $(SCENARIO)

.PHONY: check
check: lint typecheck test eval ## Everything CI runs

.PHONY: serve
serve: ## Run the webhook service locally on :8080
	$(RUN) triage-agent serve --port 8080

# --- Docker compose ----------------------------------------------------------

.PHONY: compose-up
compose-up: ## Start agent + demo app + Prometheus + Alertmanager + Loki
	docker compose up -d --build

.PHONY: compose-break
compose-break: ## Make the compose demo app fail (MODE=errors|slow|healthy)
	curl -s -X POST localhost:$${DEMO_APP_PORT:-8081}/mode/$${MODE:-errors}; echo

.PHONY: compose-down
compose-down: ## Stop the compose stack and delete its volumes
	docker compose down -v

# --- kind ----------------------------------------------------------------------

.PHONY: kind-up
kind-up: ## Create a kind cluster with the full stack and demo app
	./deploy/kind/up.sh

.PHONY: kind-break-crash
kind-break-crash: ## Roll out a config change that crash-loops the demo app
	kubectl $(KIND_CTX) -n demo set env deployment/demo-app MODE=crash
	kubectl $(KIND_CTX) -n demo annotate deployment/demo-app kubernetes.io/change-cause="set MODE=crash" --overwrite

.PHONY: kind-break-leak
kind-break-leak: ## Roll out a memory leak (OOMKilled within seconds)
	kubectl $(KIND_CTX) -n demo set env deployment/demo-app MODE=leak
	kubectl $(KIND_CTX) -n demo annotate deployment/demo-app kubernetes.io/change-cause="set MODE=leak" --overwrite

.PHONY: kind-break-errors
kind-break-errors: ## Roll out a version whose downstream calls are refused
	kubectl $(KIND_CTX) -n demo set env deployment/demo-app MODE=errors
	kubectl $(KIND_CTX) -n demo annotate deployment/demo-app kubernetes.io/change-cause="set MODE=errors" --overwrite

.PHONY: kind-heal
kind-heal: ## Put the demo app back to healthy
	kubectl $(KIND_CTX) -n demo set env deployment/demo-app MODE=healthy

.PHONY: kind-down
kind-down: ## Delete the kind cluster
	kind delete cluster --name aiops-triage
