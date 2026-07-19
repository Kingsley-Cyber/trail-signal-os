# TrailSignal OS

An evidence-first, agent-friendly repository for discovering overlooked outdoor and everyday product niches. The system converts repeated human activities into testable commerce hypotheses using this chain:

`activity → task → environmental constraint → friction → workaround → product territory → evidence → score → experiment`

The seed snapshot contains **460 activities**, **1,380 activity-task-friction hypotheses**, and **36 initial candidate concepts**. It is not a list of recommended products. Every seed candidate begins as a hypothesis and must pass the evidence gates before it is promoted.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -e .
niche-research validate
niche-research score --input data/niche_candidates.csv --output outputs/scored_niches.csv
niche-research queries --activity-id act-fishing-bank-fishing --output outputs/bank_fishing_queries.csv
niche-research dossier --candidate-id nc-001 --output outputs/nc-001-dossier.md
python -m unittest discover -s tests -v
```

No third-party runtime package is required. Python 3.11+ is recommended.

## Repository map

| Path | Purpose |
|---|---|
| `AGENTS.md` | Canonical operating contract for coding/research agents |
| `data/` | CSV system of record and seed ontology |
| `schemas/` | JSON Schema contracts for agent outputs |
| `prompts/` | Single-purpose agent prompts |
| `docs/` | Research methods, ontology, scoring, seasonality and RAG guidance |
| `src/niche_research/` | Validator, scorer, query generator, run creator and dossier compiler |
| `research_runs/` | Immutable per-run research workspaces |
| `templates/` | Human-readable deliverable templates |
| `manifests/` | RAG ingestion manifest and checksums |
| `outputs/` | Generated scores, query packs and dossiers |

## Core rules

1. Never convert popularity into product demand without product-specific evidence.
2. Never fabricate volume, pricing, review counts, margins or seasonality.
3. Preserve raw evidence and record retrieval dates.
4. Treat all seed scores as priors, not facts.
5. Separate observed facts, analyst inference and recommendation.
6. Reject niches whose risk, regulation, shipping or return profile overwhelms the opportunity.
7. Current-day findings expire; each record includes an `observed_at` or `last_verified_at` date.

## Primary CSVs

- `outdoor_activity_niche_seed.csv`: broad activity/task/friction seed table.
- `niche_candidates.csv`: candidate hypotheses ready for evidence collection.
- `research_evidence.csv`: append-only evidence ledger.
- `source_registry.csv`: source access modes, limitations and freshness rules.
- `seasonal_calendar.csv`: event and weather windows to test, not assumed demand.
- `scoring_rubric.csv`: scoring dimensions and observable anchors.

## Definition of an elite niche

An elite niche is not merely trending. It has a repeated task, painful and legible friction, independent workaround evidence, an underserved product interface, attractive unit economics, practical acquisition channels, manageable risk, and a defensible path to variants or adjacent products.

See `docs/02_research_pipeline.md` and `docs/05_scoring_and_gates.md` before promoting any candidate.
