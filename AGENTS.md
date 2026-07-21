# Agent Operating Contract

> **Scope.** This contract is authoritative for **runtime reasoning and evidence discipline** — how the
> built system discovers, evidences, and reasons about product opportunities. It does **not** govern how
> the system is constructed; that is `AGENT_BUILD_CONTRACT.md`.
> **Above this contract:** LAW 1 (no LLM-produced score) and LAW 2 (total lineage), defined in
> `docs/build/control_plane_v4_signal_engine.md §0`, bind this contract and override it on any conflict.

This file is authoritative for every agent working in this repository.

## Mission

Discover and validate overlooked product niches associated with repeated outdoor or field-adjacent human behavior. Produce reproducible research artifacts, not persuasive speculation.

## Required reasoning chain

For every candidate, preserve:

1. `activity`: what people repeatedly do.
2. `task`: the atomic action within the activity.
3. `context`: body state, environment, equipment and secondary responsibility.
4. `friction`: measurable failure, delay, discomfort, loss or workaround.
5. `workaround`: evidence users already modify behavior or equipment.
6. `product hypothesis`: the smallest product interface that could remove friction.
7. `evidence`: source, date, excerpt/observation, metric and limitations.
8. `score`: produced by the deterministic scoring engine (docs/build/08_signal_engine.md),
   NOT by you. You supply fields 1–7 as structured evidence; the engine computes the normalized
   sub-scores, confidence, and blended opportunity score. You may *explain* a score after it is
   computed (citing record_ids); you may never assign one. [LAW 1]
9. `experiment`: cheapest falsification test.

## Non-negotiable evidence rules

- Do not invent metrics, reviews, prices, demand, competition, margins or quotations.
- Mark unsupported statements as `hypothesis`, `inference` or `unknown`.
- Store every external observation in `data/research_evidence.csv` or a run-local `evidence.csv`.
- Include source URL/identifier, retrieval date, geography, query and evidence type.
- Preserve contradictory evidence.
- Do not delete evidence. Supersede it with a newer row and link `supersedes_evidence_id`.
- Never output a numeric opportunity or rubric score yourself. Scores come only from the deterministic
  engine (docs/build/08_signal_engine.md). Producing a score is a LAW 1 violation.
- Do not infer annual demand from a brief social trend.
- Do not promote a candidate until every hard gate in `config/evidence_gates.json` passes.

## File behavior

- CSV is the shared system of record; JSON is the agent interchange format; Markdown is the narrative layer.
- Use UTF-8, LF line endings, ISO-8601 dates and stable IDs.
- Append research runs under `research_runs/YYYY-MM-DD_slug/`.
- Never overwrite `raw/` evidence.
- Generated files belong in `outputs/` and must name their inputs.
- Update `manifests/rag_manifest.csv` when adding durable knowledge files.

## Agent workflow

1. Read `README.md`, this file, `docs/domain/01_ontology_and_data_model.md`, and `docs/domain/03_evidence_standard.md`.
2. Run `niche-research validate` before changes.
3. Create a research run with `niche-research new-run --slug <slug>`.
4. Generate queries from a seed activity and friction family.
5. Gather evidence using only lawful, permitted source access.
6. Normalize evidence into the run ledger.
7. Score only after required evidence exists.
8. Run red-team review.
9. Compile a dossier and define a falsification experiment.
10. Run validation and tests before committing.

## Promotion states

`seed → researching → evidence_ready → red_team → experiment_ready → validated → rejected → stale`

An agent may not skip states. `validated` means evidence supports a market test, not guaranteed commercial success.

## Verification commands

```bash
niche-research validate
python -m unittest discover -s tests -v
niche-research score --input data/niche_candidates.csv --output outputs/scored_niches.csv
```

Success requires exit code 0, zero validation errors, and all tests passing.
