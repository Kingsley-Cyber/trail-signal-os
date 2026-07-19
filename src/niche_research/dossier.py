from __future__ import annotations
from datetime import date
from typing import Iterable, Mapping

DIMENSIONS = (
    "behavior_frequency", "friction_severity", "workaround_strength",
    "complaint_repetition", "product_simplicity", "shipping_fit",
    "margin_potential", "return_risk_inverse", "community_reachability",
    "competition_gap", "seasonal_timing", "expansion_potential", "risk_inverse",
)


def _evidence_table(rows: list[Mapping[str, str]]) -> str:
    if not rows:
        return "No normalized evidence recorded."
    lines = [
        "| ID | Type | Polarity | Observation | Confidence |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        observation = row.get("observation", "").replace("|", "/")
        lines.append(
            f"| {row.get('evidence_id','')} | {row.get('evidence_type','')} | "
            f"{row.get('polarity','')} | {observation} | {row.get('confidence','')} |"
        )
    return "\n".join(lines)


def render_dossier(
    candidate: Mapping[str, str],
    score: Mapping[str, object],
    evidence: Iterable[Mapping[str, str]],
) -> str:
    related = [row for row in evidence if row.get("candidate_id") == candidate.get("candidate_id")]
    supporting = [row for row in related if row.get("polarity") == "supporting"]
    contradicting = [row for row in related if row.get("polarity") == "contradicting"]
    score_rows = "\n".join(f"| {d} | {candidate.get(d,'')} |" for d in DIMENSIONS)
    return f'''---
artifact_type: niche_dossier
candidate_id: "{candidate.get('candidate_id','')}"
status: "{candidate.get('research_state','')}"
generated_at: "{date.today().isoformat()}"
---

# {candidate.get('candidate_title','Untitled candidate')}

## Decision summary

- **Weighted seed score:** {score.get('weighted_score','')}/100
- **Score band:** {score.get('score_band','')}
- **Hard gates passed:** {score.get('hard_gates_passed','false')}
- **Experiment eligible:** {score.get('experiment_eligible','false')}
- **Fact status:** {candidate.get('fact_status','unknown')}

This score is a research prior unless the row contains evidence IDs and an evidence-adjusted score basis.

## Niche thesis

For **{candidate.get('target_participant','')}** in **{candidate.get('target_context','')}**, test whether the following product removes a repeated friction:

> {candidate.get('product_hypothesis','')}

## Known supporting evidence

{_evidence_table(supporting)}

## Contradictory evidence

{_evidence_table(contradicting)}

## Research gaps

- Verify repeated behavior and complaint independence.
- Verify at least three distinct workarounds.
- Map products, substitutes, current prices, review failures and return causes.
- Verify regional seasonality and current timing.
- Complete operations, safety, compliance and intellectual-property review.
- Define willingness-to-pay and falsification thresholds.

## Proposed next falsification test

{candidate.get('next_falsification_test','')}

## Score inputs

| Dimension | Seed value |
|---|---:|
{score_rows}

## Evidence appendix

{_evidence_table(related)}
'''
