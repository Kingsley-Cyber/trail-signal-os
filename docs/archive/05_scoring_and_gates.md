> **ARCHIVED — superseded by `docs/build/08_signal_engine.md`.**
> Business interpretation bands salvaged to `config/constraints.yaml`. Reference only; not governing.

# Scoring and Evidence Gates

## Why both are required

Scores prioritize candidates; gates prevent numerical confidence from replacing evidence. A candidate with attractive economics but no verified repeated friction remains a hypothesis.

## Scoring dimensions

All dimensions use 0–5 anchors defined in `data/scoring_rubric.csv`:

- behavior frequency;
- friction severity;
- workaround strength;
- complaint repetition;
- product simplicity;
- shipping fit;
- margin potential;
- inverse return risk;
- community reachability;
- competition gap;
- seasonal timing;
- expansion potential;
- inverse safety/regulatory risk.

The CLI normalizes the weighted mean to 0–100 and applies explicit penalties.

## Hard evidence gates

See `config/evidence_gates.json`. Default gates require independent complaints, workaround examples, source diversity, competitor analysis, current price checks, risk review and a defined falsification experiment.

## Interpretation

- `<50`: reject or archive.
- `50–64.99`: continue research only when evidence collection is inexpensive.
- `65–79.99`: eligible for a controlled experiment after gates pass.
- `80–100`: priority experiment, not automatic inventory purchase.

## Scoring discipline

Scores without evidence IDs are analyst priors. Label them `seed_prior`. Evidence-adjusted scores must name supporting evidence IDs and scoring date.
