# Ontology and Data Model

## Core nodes

- **Activity domain:** broad behavior family such as fishing or gardening.
- **Activity:** recognizable participant behavior such as bank fishing.
- **Task:** atomic action such as changing a lure.
- **Actor:** participant segment and skill state.
- **Context:** place, weather, lighting, posture, occupied hands, companions and equipment.
- **Friction:** delay, discomfort, contamination, loss, restriction, setup burden or failure.
- **Workaround:** modification or compensating behavior already used.
- **Product territory:** solution family rather than a final SKU.
- **Candidate:** a falsifiable product-market hypothesis.
- **Evidence:** time-stamped observation supporting or contradicting a claim.
- **Seasonal signal:** recurring or current timing evidence.
- **Experiment:** low-cost test with decision thresholds.

## Core edges

`Activity HAS_TASK Task`

`Task OCCURS_IN Context`

`Task PRODUCES Friction`

`Actor USES Workaround`

`Workaround IMPLIES ProductTerritory`

`Evidence SUPPORTS|CONTRADICTS Candidate`

`SeasonalSignal MODULATES Candidate`

`Candidate TESTED_BY Experiment`

## Stable identifiers

IDs are lowercase kebab-case with a namespace prefix:

- `act-` activity
- `task-` task
- `fr-` friction
- `pt-` product territory
- `nc-` niche candidate
- `ev-` evidence
- `sig-` seasonal signal
- `run-` research run

IDs must remain stable after publication. Rename display labels, not IDs.

## Fact status

Every material claim uses one status:

- `observed`: directly represented by recorded evidence.
- `inferred`: reasoned from evidence and clearly labeled.
- `hypothesis`: proposed for investigation.
- `unknown`: not yet established.
- `contradicted`: reliable evidence opposes the claim.

## Granularity test

A candidate is sufficiently narrow when it specifies participant, task, context, friction and proposed interface. “Hiking accessories” fails. “A glove-operable shoulder-strap pouch for hikers repeatedly accessing a phone in freezing rain” passes.
