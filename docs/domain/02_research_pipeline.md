> **STATUS: Non-authoritative conceptual overview.**
> Onboarding / plain-language material only. Not a Tier-2 spec.
> For implementation, defer to:
> - `docs/build/control_plane_v3_24gb.md` (control plane)
> - `docs/build/control_plane_v4_signal_engine.md` (signal infra, LAW 1 & 2)
> - `docs/build/06_source_degradation.md` (acquisition)
> - `docs/build/08_signal_engine.md` (deterministic scoring)
> Do not treat this document as governing.

# Research Pipeline (conceptual)

A plain-language map of how a niche candidate moves from seed to decision.
The durable control plane and signal engine own the real stages, gates, and artifacts.

## Stage 0 — Seed expansion

Expand activity domains into activities, tasks, contexts and friction families. No market conclusion is allowed here.

**Output:** rows in `outdoor_activity_niche_seed.csv`.

## Stage 1 — Behavioral friction mining

Search participant discussions, how-to content, repair/modification content, product reviews and field demonstrations. Capture the participant’s language and the exact task episode.

**Checkpoint:** at least ten independent complaint observations and three distinct workarounds before promotion.

## Stage 2 — Existing-solution mapping

Map direct products, adjacent substitutes, DIY solutions and “do nothing” behavior. Identify whether the gap is missing functionality, poor interface, poor packaging, poor positioning or unavailable combination.

**Checkpoint:** compare at least two product or substitute categories and record current prices from at least three sellers/listings.

## Stage 3 — Demand triangulation

Triangulate query interest, marketplace availability, review language, social creative, community repetition and observable participation. Record geography and dates. Interest is not demand; demand is not margin.

## Stage 4 — Seasonality and timing

Distinguish baseline calendar seasonality, weather-driven timing, event-driven timing, gifting and short-lived trends. Use regional timing. Record launch lead time and evidence freshness.

## Stage 5 — Operational screen

Estimate dimensional weight, fragility, defect modes, variant count, compliance, intellectual-property exposure, supplier concentration, return causes and quality-control burden.

## Stage 6 — Scoring and hard gates

Run the weighted model only after evidence normalization. A high score cannot override failed hard gates. Scoring is deterministic (`docs/build/08_signal_engine.md`); agents supply evidence, never scores [LAW 1].

## Stage 7 — Red team

Attempt to falsify the thesis. Search for declining participation, cheap substitutes, dominant incumbents, hidden certifications, user unwillingness to pay, acquisition difficulty and seasonal inventory risk.

## Stage 8 — Experiment

Choose the cheapest decisive test: concept interviews, waitlist landing page, creative click test, preorder, small batch, modified off-the-shelf prototype, or marketplace listing test that complies with platform rules.

## Stage 9 — Decision

- **Advance:** evidence and experiment cross thresholds.
- **Revise:** friction exists but product or segment is wrong.
- **Hold:** timing or evidence freshness is insufficient.
- **Reject:** workaround is weak, market is inaccessible, economics fail or risk is unacceptable.
