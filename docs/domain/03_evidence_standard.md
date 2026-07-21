# Evidence Standard

## Evidence ledger requirements

Every observation must contain:

- stable evidence ID;
- candidate and claim IDs;
- evidence type and polarity;
- source class and source identifier;
- query or navigation path;
- observed date and retrieval date;
- geography and language;
- paraphrased observation or compliant excerpt;
- metric value/unit when applicable;
- independence group;
- confidence and limitations;
- raw artifact path when retained.

## Evidence classes

1. **Behavior:** participant demonstrates or describes the task/friction.
2. **Workaround:** modification, repurposing or compensating routine.
3. **Demand:** transaction, availability, query or conversion evidence.
4. **Competition:** product coverage, reviews, positioning and price.
5. **Seasonality:** climate, calendar, query or sales timing.
6. **Operations:** size, materials, shipping, manufacturing and returns.
7. **Risk:** safety, legal, policy, IP and compliance.
8. **Contradiction:** evidence that weakens the hypothesis.

## Independence

Twenty reposts of one video are one observation. Ten reviews from one listing may indicate repetition but remain one platform/listing cluster. Use `independence_group` to prevent false counts.

## Freshness

- Live price and availability: normally recheck within 30 days.
- Social and marketplace trend signals: normally recheck within 14 days during active research.
- Product specifications and platform rules: verify at decision time.
- Stable behavior research: retain longer but review context and population.
- Seasonal facts: record year, region and whether the source is a normal, forecast or observed event.

## Quotes and copyright

Store short excerpts only when necessary. Prefer faithful paraphrase with source coordinates. Do not bulk-copy protected text.

## Confidence

- `high`: direct, specific and independently corroborated.
- `medium`: relevant but limited by sample, source or context.
- `low`: weak proxy or exploratory lead.

The evidence ledger preserves low-confidence leads but they do not satisfy hard gates unless explicitly approved.
