# CSV Layer

CSV is the shared, diffable system of record. Do not reorder or rename columns casually. Add new columns at the end and update `docs/11_data_dictionary.md`, validators, schemas, tests and the version field when semantics change.

- Use UTF-8 and comma delimiters.
- Use stable IDs and ISO-8601 dates.
- Separate multi-valued tags with semicolons.
- Never place commas in numeric fields.
- Empty numeric cells mean unknown; zero is a measured value.
- `outdoor_activity_niche_seed.csv` and seed-prior scores are hypotheses, not observations.
- `research_evidence.csv` is append-only.
