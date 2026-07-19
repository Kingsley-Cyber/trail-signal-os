# Data Dictionary

## `outdoor_activity_niche_seed.csv`

One row per activity/task/context/friction hypothesis. It is an ideation corpus, not evidence.

Key fields: `activity_id`, `domain`, `activity`, `participant`, `task`, `context`, `environmental_constraints`, `body_or_hand_state`, `friction_family`, `friction_hypothesis`, `observed_workaround_hypothesis`, `product_territory`, `seasonal_tags`, `risk_flags`, `research_status`.

## `niche_candidates.csv`

One row per product hypothesis and seed-prior score. `hard_gates_passed` must remain false until evidence is evaluated.

## `research_evidence.csv`

Append-only normalized evidence. `polarity` is `supporting`, `contradicting` or `neutral`. `independence_group` controls duplicate counting.

## `source_registry.csv`

Current source access assumptions. Always verify the source and terms at collection time.

## `seasonal_calendar.csv`

Research hypotheses for timing. Rows are not proof of sales seasonality.

## Empty values

Use empty string for unavailable optional values. Use `unknown` when the absence itself is analytically meaningful. Never use zero to mean unknown.
