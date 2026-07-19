# Agent Orchestration

## Recommended roles

- **Orchestrator:** selects scope, state and handoffs.
- **Activity expander:** generates atomic tasks and contexts.
- **Friction miner:** gathers complaint and workaround evidence.
- **Demand researcher:** collects current demand proxies and limitations.
- **Competition analyst:** maps products, reviews, substitutes and pricing.
- **Seasonality analyst:** models regional windows and lead times.
- **Operations analyst:** evaluates sourcing, shipping, quality and returns.
- **Risk reviewer:** checks safety, regulation, IP and platform constraints.
- **Scorer:** applies rubric after gates are evaluated.
- **Red team:** attempts falsification.
- **Dossier writer:** compiles traceable decision material.

## Handoff contract

Each handoff validates against `schemas/agent_handoff.schema.json` and includes:

- task ID and candidate ID;
- role and requested action;
- input artifact paths;
- expected output schema;
- evidence requirements;
- blocked assumptions;
- completion checks.

## Parallelism

Parallelize independent source classes, not interpretations of the same evidence. Merge only after deduplication by independence group.

## Failure behavior

Agents must stop promotion when:

- a required source cannot be accessed lawfully;
- the evidence is ambiguous or stale;
- numeric fields cannot be verified;
- a schema fails validation;
- source terms prohibit intended collection;
- a material safety or compliance issue is unresolved.

Record the block rather than filling the gap with inference.
