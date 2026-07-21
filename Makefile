.PHONY: validate test score queries dossier all bootstrap down integration-check-infra
# Host has python3 only (docs/build/environment_profile.md §4)
PYTHON ?= python3
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
# N3 (deferred): fixtures + cassettes load.
# Control API (N10), Ollama model pull: later nodes / native host.
bootstrap:
	@set -e; \
	echo "==> N0 infra slice: docker compose up --wait ($(BOOTSTRAP_SERVICES))"; \
	if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	$(COMPOSE) up -d --wait $(BOOTSTRAP_SERVICES); \
	echo "==> N0 complete: Postgres :$${POSTGRES_PORT:-5433} and Redis :$${REDIS_PORT:-6380} healthy"; \
	echo "==> Deferred (N2): migrate — db/migrations/ not yet available"; \
	echo "==> Deferred (N3): load fixtures + cassettes"; \
	echo "==> Deferred: Ollama model pull on native host"; \
	echo "==> External: SearXNG via Polymath at host :8080 (no compose service)"; \
	echo "==> Gate 0 not complete until migrate + fixtures (N2, N3)"

down:
	$(COMPOSE) down

# Offline structural check for gate-verifier when Docker is unavailable.
integration-check-infra:
	PYTHONPATH=src $(PYTHON) -m unittest tests.test_infra_n0 -v

integration-check-schemas:
	PYTHONPATH=src $(PYTHON) -m unittest tests.test_schemas_n1.IntegrationCheckSchemas -v

all: validate test score queries dossier
