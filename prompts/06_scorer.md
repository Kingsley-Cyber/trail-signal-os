# Scorer Prompt

Apply `data/scoring_rubric.csv` and `config/scoring_weights.json` only after evidence gates are evaluated.

For every dimension return:

- integer score 0–5;
- evidence IDs;
- one-sentence rationale;
- uncertainty;
- any penalty.

A seed-prior score is allowed for ranking research only and must be labeled `score_basis=seed_prior`. Evidence-adjusted scoring requires evidence IDs. A failed hard gate prevents experiment promotion regardless of total score.
