# 09 — Verification Harness & Autonomous Build Gates

The layer that lets an agent build this repo unattended without silently breaking it. Every other doc is a *specification*; this one makes the specifications **machine-enforced**, because an autonomous agent won't fail to write code — it'll write plausible code that violates an invariant in a way that compiles, passes a naive test, and looks done. The only defense when no one is watching is deterministic ground truth the agent cannot fake.

**Central idea:** gates are deterministic and offline. LLM nondeterminism — the one thing that would make gates flaky and fakeable — is quarantined behind recorded cassettes (§3). A green gate therefore *means something*, every time.

Governs `tests/`, `gates/`, `fixtures/`, `Makefile`, `docker-compose.yml` healthchecks. Consumes the invariants of v1/v3, v4 (LAW 1 & 2), doc 06 (no-evasion), doc 07 (verifier-per-node), doc 08 (deterministic scoring).

---

## 1. Invariant guards — three layers, each with a poison test

An invariant stated in prose is a suggestion. An invariant with a **static** check (fails CI), a **runtime** guard (fails the write), or a **schema** check (fails validation) is a law. And a guard you don't test can be silently stubbed by the agent — so **every guard ships with a poison test**: a deliberate violation that asserts the guard *fires*. `make verify-guards` runs all poison tests; if any guard fails to catch its poison, the build is not trustworthy.

| # | Invariant (source) | Guard type | Mechanism | Poison test asserts |
|---|---|---|---|---|
| 1 | Ack **after** commit (v1 §7) | static | workers must use the `process_task()` template that orders commit→XACK; lint bans raw `.xack(` outside it | a worker acking pre-commit → lint fails |
| 2 | Fencing token on result writes (v1 §6) | static+runtime | every `UPDATE tasks SET state=…` includes `lease_owner`+`lease_generation` in WHERE; a 0-row result update raises `StaleLeaseError` | stale-generation write → 0 rows → `StaleLeaseError`, result discarded |
| 3 | Idempotency keys unique (v1 §15, v4 §8) | schema | DB unique constraints on task/signal/opportunity keys; migration test asserts they exist | duplicate key insert → one row, duplicate is no-op |
| 4 | Outbox atomicity+ordering (v1 §2) | static+runtime | no `XADD` to `cp:*` outside `dispatcher/`; task+outbox insert in one transaction | crash before XADD → reconciler republishes (restart-Redis gate) |
| 5 | **LAW 1** — no LLM score (v4 §0) | static+runtime | import guard: `score/normalize/confidence/coverage/tiers` import no gateway/network; write guard rejects `opportunity.score` provenance containing `model_id` | opportunity whose score provenance shows a model → **rejected + alert** |
| 6 | **LAW 2** — total lineage (v4 §6) | runtime | derived-artifact insert requires non-empty parent refs **and** writes a `lineage_edges` row; reconciler flags inline-ref-without-edge | signal with empty `derived_from` → rejected; orphan artifact → reconciler flags |
| 7 | Provenance stamp on every artifact (v3 §5) | static+runtime | all artifact writes go through `persist_artifact(provenance=…)`; lint bans direct inserts to artifact tables | artifact insert missing provenance → rejected |
| 8 | Every LLM node has a verifier; back-edges bounded (07 §2/§4) | schema | workflow YAML schema requires `verifier` for `kind: llm` and `max_trips` on back-edges | node without verifier → YAML validation fails |
| 9 | Deterministic modules are import-pure (08) | static | import-graph assertion over `signal_engine/normalize|score|confidence|coverage|tiers.py` | adding a gateway import to `score.py` → guard fails |
| 10 | No-evasion (06 §2.7) | static+runtime | dependency denylist (no proxy-rotation/fingerprint libs); `403` handler routes to `BLOCKED`, never browser escalation | injected `403` → task `BLOCKED`, no stealth/escalation fired |
| 11 | Normalize invariants (08 §4) | runtime | assert `0≤s≤1`, window set, direction applied | out-of-range normalized value → hard error (bug, not retry) |
| 12 | Score reproducibility (v4 §13, 08 §7) | test | `score()` over fixture signals == golden constant, identical across two runs | any nondeterministic op in the score path → reproducibility test fails |

Guards 1, 4, 7, 9, 10 are **static** (run in pre-commit + CI, block merge). Guards 2, 5, 6, 7, 10, 11 are **runtime** (fail the write, alert). Guard 8 is **schema** (workflow compile-time). This is the LAW-1 write-guard pattern (v4 §13) generalized to every invariant.

---

## 2. Acceptance-gate harness — phase gates the agent cannot skip

Each gate is a runnable manifest of checks. **The agent cannot advance to gate N+1 until gate N is green.** Gates are the agent's ground truth against its own biggest failure mode: hallucinated progress ("lease system done" when it's stubbed). Progression maps to the build order (v3 §10, v4 §14).

Runner: `make gate-N` → runs the manifest offline → writes verdict to the progress ledger (§5). A gate's manifest = *infra precondition* + *fault-injection tests for that phase* (referenced by doc location, not restated) + *the vertical-slice or reproducibility assertion* + *which guards from §1 must be active by now*.

```
Gate 0  INFRA          bootstrap green (§4): Postgres/Redis/SearXNG/Ollama healthy; migrations applied;
                       fixtures + cassettes loaded; model pulled. Guards active: —
Gate 1  SKELETON       ONE query → fetch → extract → page.v1 persisted → lineage_edges row →
                       lineage.trace reaches the query_spec leaf. Kill-HTTP-worker (v1 §22): reaper
                       reclaims, exactly one artifact. Guards: 1,2,3,4,7.
Gate 2  MEDIA+MCP      duplicate-message → one result (v1 §22); yt-dlp auto-sub yields a transcript on
                       fixtures (doc 06 wk-2 test); MCP create_job/status/bundle round-trip. Guards: +schema val.
Gate 3  ENRICH+INDEX   evidence.v1 via cassette validates; invalid → repair, NOT index; Qdrant search
                       returns; phase gating holds under memory pressure (v3 §2). Guards: +8.
Gate 4  LINEAGE+SIGNAL classify→normalize split (v4 §4); LAW-2 trace complete on a signal; LAW-1 write
                       guard fires on poison. Guards: +5,6,9,11.
Gate 5  COVERAGE+SCORE coverage gate admits only when hard cells met, else scores-with-gaps (v4 §5);
                       deterministic score reproduces byte-for-byte (guard 12); score_invariants. Guards: +12.
Gate 6  REPLAY+TIERS   lineage.replay reproduces a score from cached artifacts (v4 §13); lineage.diff shows
                       delta on weights bump; tier-loss degrades confidence, no evasion (v4 §13, doc 06). Guards: +10.
Gate 7  FULL DOSSIER    freshness expiry triggers re-collection (v4 §10); VALIDATE sub-graph + DECIDE run;
                       doc 08's real weights drop in; **fixture niche reproduces opportunity 0.72; dead-niche
                       variant scores ~0.34** (doc 08 §12). Guards: all 12.
```

A gate never passes by weakening the gate or editing a golden to match broken output — see the goalpost guard (§5).

---

## 3. Fixture corpus + LLM cassettes — deterministic, offline, budget-zero

Gates must run without live web and without live LLM calls: live web drifts (flaky gates), and live LLM output is nondeterministic (unfakeable-green becomes impossible) *and* burns budget on every agent dev loop. Both are quarantined.

```
fixtures/
├── pages/                  # one raw file per source class + its golden extraction
│   ├── article.html                 → golden/article.page.v1.json
│   ├── forum_thread.html            → golden/forum_thread.page.v1.json
│   ├── marketplace_listing.html     → golden/…page.v1.json
│   ├── review_page.html             → golden/…page.v1.json
│   └── youtube_meta.json + youtube_transcript.vtt → golden/…page.v1.json
├── search/
│   └── searxng_<query>.json         # frozen SearXNG responses → deterministic DISCOVER
├── cassettes/                       # recorded LLM req→resp, keyed by input hash
│   ├── enrich/<hash>.json  classify/<hash>.json  explain/<hash>.json
└── niches/
    └── camping-fixture/             # synthetic niche with a KNOWN signal set
        ├── signals.json             # → deterministic score
        └── expected_opportunity.json# score 0.72, subscores, confidence 0.65 (doc 08 §12)
```

**Golden files** (extraction): checked in; a test diffs actual vs golden. Changing an extractor requires `make goldens` to regenerate — the diff shows in the PR, so extraction changes are *intentional and visible*, never silent drift.

**Cassettes** (LLM): the enrich/classify/explain workers run in **replay-only mode during gates** — a missing cassette is a hard failure, never a silent live call. This is what makes an LLM pipeline deterministically testable: record once, replay forever. Budget during autonomous build is therefore ~zero.

**The one place the real model runs:** `make smoke-live` (non-gating) executes the actual model on a couple of fixtures and asserts *schema-validity + expected field presence* (not exact text — LLM output varies). This is where a model swap (v3 §5) gets sanity-checked, and it's bounded spend, run on demand, never in the gate loop.

**The scoring gate is fully deterministic** because signals are fixtures and scoring is pure code: `camping-fixture/signals.json` → `score()` → must equal `expected_opportunity.json` exactly. Doc 08's worked example *is* the acceptance test.

---

## 4. Infra bootstrap — one command, health-gated

Local infra bring-up (Postgres, Redis, SearXNG, Ollama, OrbStack) is non-code setup agents fumble — and if it doesn't come up, the agent stalls before writing logic. So it's a single idempotent command with health checks, and it *is* Gate 0.

```
docker-compose.yml:  every service has a healthcheck; dependents use
                     depends_on: {condition: service_healthy}
  postgres  → pg_isready        redis → redis-cli PING
  searxng   → GET /healthz      control → GET /readyz  (ready only AFTER first reconciler pass, v4 §7)
Ollama (native): bootstrap pulls the model, waits on /api/tags

make bootstrap:  up --wait  →  migrate  →  load fixtures+cassettes  →  pull model  →  verify all healthy
                 (idempotent; re-runnable; Gate 0 is `make bootstrap` returning green)
```

`/healthz` (liveness) vs `/readyz` (readiness — gated on the reconciler's first pass) encodes the startup order from v4 §7: nothing is dispatched before the reconciler has run once.

---

## 5. Autonomous-safety loop protocol — surface, don't barrel

The agent's build loop is not fire-and-forget; it self-gates and stops honestly:

```
for gate G in [0..7]:
    attempts = 0
    while True:
        implement / fix toward G
        r = run_gate(G)                      # deterministic, offline (§2,§3)
        if r.PASS:
            ledger.append(G, PASS, commit_sha, checks); break
        attempts += 1
        ledger.append(G, FAIL, commit_sha, r.failing_checks)
        if attempts >= MAX_FIX_ATTEMPTS[G]:
            write BLOCKED.md {gate:G, failing_checks:r, last_error, hypotheses}
            HALT                             # do NOT proceed. do NOT fake. surface.
    # advance only on PASS
```

- **Progress ledger** (`progress_ledger.csv`, append-only): gate, verdict, timestamp, commit SHA, checks passed/failed. The audit trail of unattended work — mirrors your evidence-ledger philosophy, and it's how you review what the agent actually did.
- **BLOCKED report:** on genuine inability (infra won't come up, spec ambiguity, an invariant that can't be satisfied), the agent writes a structured stop and halts. "Unattended" means you review these — not that they never happen.
- **Self-budget:** `LLM_BUILD_BUDGET` caps the agent's own spend; cassettes keep it near zero; `smoke-live` is the only real spend and is bounded.
- **Goalpost guard (critical for unattended):** `gates/` and `fixtures/**/golden/` and `expected_opportunity.json` are baseline-hashed; any modification to a gate or a golden is flagged in the ledger and blocks merge unless explicitly acknowledged. The agent **cannot** turn a gate green by moving the goalpost — the one failure mode that would make all of this worthless.

---

## 6. Definition of done for autonomous build

The build is verifiably complete — not "looks done" — when **all four hold**:

1. Gates 0–7 all `PASS` in `progress_ledger.csv`.
2. `make verify-guards` green — every one of the 12 poison tests confirms its guard fires.
3. Offline end-to-end on fixtures: `camping-fixture` → full dossier → reproducible **opportunity 0.72**, `lineage.trace` complete to `query_spec` leaves, `lineage.replay` reproduces the score byte-for-byte.
4. No outstanding `BLOCKED.md`, and no un-acknowledged goalpost-guard flag.

That tuple is the machine-checkable proof the agent built the system correctly, not plausibly.

---

## 7. What this deliberately does NOT do (honest scope)

- **It does not guarantee live-web quality.** Fixtures are frozen; real sites drift and defend. After the build passes, *you* still spot-check a live dossier — the gates prove the pipeline is correct, not that today's Amazon HTML matches last month's golden.
- **It does not tune the scoring constants.** Weights/λ/half-lives remain yours to calibrate (doc 08 §13); the gate only proves the math is *reproducible*, not that 0.72 is the *right* answer for that niche.
- **It does not judge whether an opportunity is real.** It proves the number was computed correctly and traceably — market truth is validated by your VALIDATE sub-graph and, ultimately, by selling.
- **It does not remove you from the loop entirely.** It removes you from *watching keystrokes*. Reviewing BLOCKED reports and the progress ledger is the irreducible minimum of "autonomous, not unmonitored."

> The agent writes the code. The gates decide whether it's real. The guards make the invariants impossible to break. The cassettes make truth deterministic. What can't be traced or reproduced is a build failure, not a result.
