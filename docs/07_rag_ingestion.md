# RAG Ingestion Guidance

## Durable corpus layers

1. **Method layer:** `docs/`, prompts, schemas and templates.
2. **Ontology layer:** taxonomy and library CSVs.
3. **Evidence layer:** append-only evidence CSVs and normalized run notes.
4. **Decision layer:** scored candidates, dossiers and experiment results.

## Recommended chunks

- Markdown: split by heading path; retain file path and heading path.
- CSV: one row per chunk plus a dataset-level summary chunk.
- JSON Schema: one schema and one property-group chunk.
- Prompt: one full prompt per chunk.

## Required metadata

`artifact_id`, `artifact_type`, `path`, `title`, `domain`, `candidate_id`, `activity_id`, `research_run_id`, `status`, `observed_at`, `last_verified_at`, `evidence_polarity`, `source_class`, `version`, `checksum`.

## Retrieval priorities

- Retrieve method and schema context before asking an agent to write data.
- Retrieve candidate and supporting/contradicting evidence together.
- Filter current-day questions by verification date.
- Prefer evidence rows over narrative conclusions.
- Do not embed secrets or raw licensed exports without permission.

`manifests/rag_manifest.csv` enumerates ingestible artifacts. `checksums.sha256` supports change detection.
