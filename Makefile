# Convenience targets.
#
# Everything here is a shorthand for a command that also works on its own -- the
# Makefile is a keyboard-shortcut layer, not a build system, and nothing in the
# project depends on it. That matters because `make` is not installed by default
# on Windows, and a project whose setup requires `make` is a project that excludes
# people.
#
#   make help

PYTHON ?= .venv/bin/python
ifeq ($(OS),Windows_NT)
    PYTHON := .venv/Scripts/python.exe
endif

.DEFAULT_GOAL := help
.PHONY: help setup run test lint fmt check smoke drill stack stack-down load seed \
        rules rules-check clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
# Local development
# --------------------------------------------------------------------------- #
setup:  ## Create the venv, install dependencies, write .env, migrate
	bash scripts/bootstrap.sh

run:  ## Start the development server
	$(PYTHON) manage.py runserver

seed:  ## Create sample tasks so the dashboards are not empty
	$(PYTHON) manage.py seed_tasks --count 40

# --------------------------------------------------------------------------- #
# Quality gates -- the same set CI runs, in the same order
# --------------------------------------------------------------------------- #
test:  ## Run the test suite
	$(PYTHON) -m pytest

lint:  ## Lint
	$(PYTHON) -m ruff check .

fmt:  ## Fix anything the linter can fix automatically
	$(PYTHON) -m ruff check . --fix

rules:  ## Regenerate the alert rules from the SLO definition
	$(PYTHON) scripts/render_rules.py

rules-check:  ## Fail if the alert rules have drifted from the SLO definition
	$(PYTHON) scripts/render_rules.py --check

check: lint rules-check test  ## Everything CI runs

# --------------------------------------------------------------------------- #
# Verification against a running instance
# --------------------------------------------------------------------------- #
smoke:  ## Run the end-to-end smoke test (needs the service to be up)
	$(PYTHON) scripts/smoke_test.py

drill:  ## Run the incident drills
	$(PYTHON) scripts/run_drill.py --drill unhandled-exception
	$(PYTHON) scripts/run_drill.py --drill unknown-path-scan

load:  ## Generate traffic so the dashboards have data
	$(PYTHON) scripts/loadgen.py

# --------------------------------------------------------------------------- #
# The full stack
# --------------------------------------------------------------------------- #
stack:  ## Build and start web + db + prometheus + grafana
	docker compose up -d --build

stack-demo:  ## ...and add the load generator
	docker compose up -d --build
	docker compose --profile demo up -d

stack-down:  ## Stop the stack and remove its volumes
	docker compose --profile demo down -v

clean:  ## Remove caches and local runtime artefacts
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -f app.log app.log.* db.sqlite3
