SHELL := /bin/bash
export PYTHONPATH := $(CURDIR)
export PATH := $(HOME)/.local/bin:$(PATH)

TASKS_DIR ?= vendor/erp-bench/tasks
JOBS_DIR  ?= runs/jobs
VERIFIER  ?= harness.verifier:FlatVerifier
CONFIG    ?= C_full
# Iteration default: a dev trial costs ~$0.10 on small vs ~$0.73 on big.
# Dev-curve and eval runs pass MODEL=big explicitly.
MODEL     ?= small
SET       ?= dev5
# Docker VM is 31 GiB and a trial uses ~0.5 GiB at almost no CPU, so this is
# latency-bound rather than resource-bound.
N         ?= 12

PY ?= python3

.DEFAULT_GOAL := help

.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Install tooling and vendor the upstream repos (idempotent)
	@command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
	@command -v harbor >/dev/null || uv tool install harbor
	@mkdir -p vendor
	@[ -d vendor/erp-bench ] || git clone --depth 1 https://github.com/agentic-labs/erp-bench.git vendor/erp-bench
	@[ -d vendor/pi-mono ]   || git clone --depth 1 https://github.com/agentic-labs/pi-mono.git vendor/pi-mono
	$(PY) scripts/patch_tasks.py --tasks-dir $(TASKS_DIR)
	@[ -f .git/hooks/pre-commit ] || { printf '#!/usr/bin/env bash\nexec "$$(git rev-parse --show-toplevel)"/scripts/guard.sh\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit && echo "installed pre-commit guard hook"; }

.PHONY: guard
guard: ## Fail if eval-isolation or pattern-specificity rules are violated
	bash scripts/guard.sh

.PHONY: select
select: ## Freeze configs/eval100.txt and write the dev splits
	$(PY) scripts/select_eval100.py --tasks-dir $(TASKS_DIR)

.PHONY: run
run: guard ## Run a config: make run CONFIG=C_full MODEL=big SET=dev5
	$(PY) scripts/run.py --config $(CONFIG) --model $(MODEL) --set $(SET) -n $(N)

.PHONY: ingest
ingest: ## Convert Harbor job output into the common schema
	$(PY) scripts/ingest_harbor.py

.PHONY: aggregate
aggregate: ## Build analysis/results.csv
	$(PY) scripts/aggregate.py

.PHONY: cost
cost: ## Recompute eval100 tokens and cost from the raw records, on one price basis
	$(PY) scripts/cost.py analysis/eval100_C_full.json analysis/eval100_A_pi.json

.PHONY: stats
stats: ## Paired bootstrap and McNemar, C_full vs A_pi on eval100
	$(PY) scripts/stats.py analysis/eval100_C_full.json analysis/eval100_A_pi.json

.PHONY: figs
figs: ## Render analysis/
	$(PY) scripts/figures.py

.PHONY: docs
docs: ## Regenerate harness/prompts/library_docs.md from lib docstrings
	$(PY) scripts/gen_library_docs.py

.PHONY: test
test: ## pytest (unit tests plus live dev-container tests when ERP_DEV_CONTAINER is set)
	$(PY) -m pytest tests -q

.PHONY: devbox
devbox: ## Start the persistent dev container from the dev task image
	bash scripts/devbox.sh up

.PHONY: devbox-down
devbox-down: ## Stop and remove the persistent dev container
	bash scripts/devbox.sh down
