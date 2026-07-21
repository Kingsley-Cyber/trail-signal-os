.PHONY: validate test score queries dossier all
# Host has python3 only (docs/build/environment_profile.md §4)
PYTHON ?= python3

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

all: validate test score queries dossier
