.PHONY: validate test score queries dossier all bootstrap down migrate integration-check-infra integration-check-migrations integration-check-fixtures integration-check-leases integration-check-scheduler integration-check-retries integration-check-reconciler integration-check-control-api integration-check-gateway integration-check-node-executor integration-check-verifiers integration-check-compiler-executor integration-check-search-worker integration-check-http-extract integration-check-media-worker integration-check-lineage integration-check-mcp integration-check-enrich-worker integration-check-index-worker integration-check-governor integration-check-classify-normalize integration-check-confidence load-fixtures verify-guards gate-0
# Host has python3 only (docs/build/environment_profile.md §4)
ifneq (,$(wildcard .venv/bin/python))
PYTHON := .venv/bin/python
else
PYTHON ?= python3
endif
COMPOSE ?= docker compose
# Gate 0 bring-up: postgres + redis only; SearXNG stays external (Polymath :8080).
BOOTSTRAP_SERVICES ?= postgres redis

validate:
	PYTHONPATH=src $(PYTHON) -m niche_research.cli validate

test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

score:
	PYTHONPATH=src $(PYTHON) -m niche_research.cli score --input data/niche_candidates.csv --output outputs/scored_niches.csv

queries:
	PYTHONPATH=src $(PYTHON) -m niche_research.cli queries --activity-id act-fishing-bank-fishing --output outputs/bank_fishing_queries.csv

dossier:
	PYTHONPATH=src $(PYTHON) -m niche_research.cli dossier --candidate-id nc-001 --output outputs/nc-001-dossier.md

# N0 infra slice only (doc 09 §4 bootstrap is completed at Gate 0 after N2/N3).
# N2 (deferred): db/migrations apply.
# N3: fixtures + cassettes load (integration-check-fixtures).
# Control API (N10), Ollama model pull: later nodes / native host.
bootstrap:
	@set -e; \
	echo "==> N0 infra slice: docker compose up --wait ($(BOOTSTRAP_SERVICES))"; \
	if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	$(COMPOSE) up -d --wait $(BOOTSTRAP_SERVICES); \
	echo "==> N0 complete: Postgres :$${POSTGRES_PORT:-5433} and Redis :$${REDIS_PORT:-6380} healthy"; \
	$(MAKE) migrate; \
	$(MAKE) load-fixtures; \
	echo "==> Deferred: Ollama model pull on native host"; \
	echo "==> External: SearXNG via Polymath at host :8080 (no compose service)"; \
	echo "==> Gate 0 infra slice complete; full Gate 0 also requires model pull"

migrate:
	@set -e; \
	if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	PYTHONPATH=. $(PYTHON) -m db.repositories.migrate

down:
	$(COMPOSE) down

# Offline structural check for gate-verifier when Docker is unavailable.
integration-check-infra:
	PYTHONPATH=src $(PYTHON) -m unittest tests.test_infra_n0 -v

integration-check-schemas:
	PYTHONPATH=src $(PYTHON) -m unittest tests.test_schemas_n1.IntegrationCheckSchemas -v

integration-check-migrations:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_db_n2.IntegrationCheckMigrations -v

load-fixtures:
	PYTHONPATH=. $(PYTHON) -m fixtures.load

integration-check-fixtures:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_fixtures_n3.IntegrationCheckFixtures -v

integration-check-leases:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_leases_n5.IntegrationCheckLeases -v

integration-check-dispatcher:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_dispatcher_n6.IntegrationCheckDispatcher -v

integration-check-scheduler:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_scheduler_n7.IntegrationCheckScheduler -v

integration-check-retries:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_retries_n8.IntegrationCheckRetries -v

integration-check-reconciler:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_reconciliation_n9.IntegrationCheckReconciler -v

integration-check-control-api:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_control_api_n10.IntegrationCheckControlApi -v

integration-check-gateway:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_gateway_n11 -v

integration-check-node-executor:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_node_executor_n12 -v

integration-check-verifiers:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_verifiers_n13 -v

integration-check-compiler-executor:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_compiler_executor_n14 -v

integration-check-search-worker:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_search_worker_n15 -v

integration-check-http-extract:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_http_extract_n16 -v

integration-check-media-worker:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_media_worker_n18 -v

integration-check-lineage:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_lineage_n17.IntegrationCheckLineage -v

integration-check-mcp:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_mcp_n19.IntegrationCheckMcp -v

integration-check-enrich-worker:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_enrich_worker_n20.IntegrationCheckEnrichWorker -v

integration-check-index-worker:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_index_worker_n21.IntegrationCheckIndexWorker -v

integration-check-governor:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_governor_n22.IntegrationCheckGovernor -v

integration-check-classify-normalize:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_classify_normalize_n23.IntegrationCheckClassifyNormalize -v

integration-check-confidence:
	PYTHONPATH=. $(PYTHON) -m unittest tests.test_confidence_n24.IntegrationCheckConfidence -v

verify-guards:
	PYTHONPATH=. $(PYTHON) -m guards.runner

gate-0:
	@set -e; \
	echo "==> Gate 0 manifest: gates/gate-0.yaml"; \
	$(MAKE) validate; \
	$(MAKE) test; \
	$(MAKE) integration-check-infra; \
	$(MAKE) integration-check-schemas; \
	$(MAKE) integration-check-migrations; \
	$(MAKE) integration-check-fixtures; \
	$(MAKE) verify-guards; \
	echo "==> Gate 0 offline checks complete (live bootstrap: make bootstrap)"

all: validate test score queries dossier
