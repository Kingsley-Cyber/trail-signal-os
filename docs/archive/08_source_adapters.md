> **ARCHIVED — superseded by `docs/build/06_source_degradation.md`.** Reference only; not governing.

# Source Adapters

`data/source_registry.csv` is the source authority. It distinguishes official APIs, official exports, public manual interfaces and sources requiring terms verification.

## Adapter contract

A source adapter should return normalized records with:

`source_id`, `query`, `retrieved_at`, `observed_at`, `geography`, `language`, `result_type`, `metric_name`, `metric_value`, `metric_unit`, `source_url`, `raw_artifact_path`, `limitations`.

## Access principles

- Prefer an official API or export when available.
- Do not bypass authentication, rate limits, robots directives or access controls.
- Cache within permitted limits and record retrieval time.
- Keep credentials outside the repository.
- Source interfaces and policies change; verify before each integration.
- Manual research is valid when reproducible notes and dates are recorded.

## Source roles

- Query interest: trend and keyword planning tools.
- Product supply: marketplace search and seller availability.
- Customer language: reviews, comments, forums and demonstrations.
- Creative saturation: ad libraries and creative centers.
- Seasonality: climate normals, forecasts, park visitation and calendar/event data.
- Macro participation: government recreation and time-use statistics.

No single source establishes a niche.
