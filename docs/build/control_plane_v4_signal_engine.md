# Control Plane v4 — Signal Engine Extension

Extends `docs/build/control_plane_v3_24gb.md`. This is the **infrastructure layer only** — the durable plumbing, state, and traceability the signal engine runs on. The scoring *model* (source→signal matrix, normalization math, weights) is deliberately **not** here; it lands in `docs/build/08_signal_engine.md` and plugs into the slots this doc defines. Building infra first is intentional: a scoring model on untraceable plumbing is unauditable by construction.

Nothing in v1–v3 is repealed. Leases, fencing, ack-after-commit, idempotency, phase gating, the degradation ladder (doc 06), and graph-as-data (doc 07) all carry forward unchanged and are referenced by section, not restated.

---

## 0. The anti-slop contract (read first — it constrains everything below)

Two laws. Every design choice in this doc exists to enforce one of them.

**LAW 1 — The determinism boundary.** Work is partitioned into exactly two classes, and the boundary is enforced in code, not convention:

| Class | What | Properties |
|---|---|---|
| **Probabilistic** (LLM) | evidence extraction (claims, pain themes, sentiment); the natural-language *explanation* attached to a result | versioned by `prompt_version` + `model_id`; **always** schema-validated; never trusted raw |
| **Deterministic** (pure code) | metric normalization (raw → 0–1); signal confidence weighting; the blended opportunity score; coverage-gate evaluation; constraint-fit re-ranking | versioned by `code_version`; pure function of typed inputs; reproducible byte-for-byte |

> **No score is ever emitted by an LLM.** An LLM may *explain* a score; it may never *compute* one. `score(signals[], weights_version) → (value, subscores)` must reproduce exactly from its recorded inputs. This single rule is what separates an auditable engine from slop. If a future node wants an LLM to "just rank these," it is rejected in review.

**LAW 2 — Total lineage.** Every derived artifact carries an immutable edge to every input that produced it, back to the leaf `query_spec`s and source URLs. Any final opportunity is fully reconstructable and replayable (§6). No orphan outputs, ever.

---

## 1. What the signal engine demands that v3 doesn't provide

Honest gap list — these are the only genuinely new control-plane problems:

1. **A 2-D completeness condition.** Evidence quorum (v1: ≥100 records, ≥10 domains) is a *count*. Scoring needs *coverage*: for niche X, signal types {demand, growth, pain, competition, content} present across required source tiers. Different join → §5.
2. **Two-level job nesting.** A niche dossier spawns sub-jobs (per source class), then scoring, then a per-pain validation sub-graph, then a decision pass. Deeper than the flat research job → §3.
3. **Lineage as a first-class subsystem**, not a log → §6.
4. **Versioned re-derivation.** Re-scoring with a new weights version must produce a *new, diffable* opportunity, not overwrite → §8.
5. **Signal decay.** This is a *trend* engine; signals expire and must be re-collected on cadence → §10.
6. **Source-tier admission** (legality + confidence) coupled to the degradation ladder → §9.

Everything else is v3 machinery reused.

---

## 2. New lanes & task types

Add to the queue topology (v3 §8 rules unchanged — high/normal/bulk only where noted):

```
cp:signal:normal      signal_classify   evidence.v1  → signal.v1   (LLM classify + deterministic normalize; split, see §4)
cp:score:normal       score             signal[]     → opportunity.v1   (deterministic ONLY)
cp:validate:{h,n}     validate_fanout   opportunity  → validation sub-graph   (LLM+deterministic, doc 07 nested graph)
cp:decide:normal      decide            opportunity  → decision.v1   (deterministic re-rank + LLM rationale, separated per LAW 1)
```

Each inherits lease/heartbeat/fencing/retry verbatim (v3 §6–7, §10). Lease suggestions: signal 120s/30s, score 60s/20s (pure, fast), validate 240s/60s (fans out), decide 60s/20s.

---

## 3. Two-level job hierarchy

`research_jobs` gains `parent_job_id` (nullable) + `job_kind ∈ {dossier, collection, scoring, validation, decision}`. A dossier is the root; the scheduler expands it into child jobs and rolls their counters up.

```
DOSSIER job  (niche="camping", constraints_ref)
  ├─ COLLECTION job × source-class     # each is a v3 research job, tier-gated (§9)
  │     └─ (v3 discover→fetch→enrich→index pipeline, unchanged)
  ├─ SCORING job                        # admitted only when COVERAGE_GATE passes (§5)
  ├─ VALIDATION job × shortlisted pain  # nested sub-graph (doc 07), fan-out solution search
  └─ DECISION job                       # constraint-fit re-rank + rationale
```

Child-job failure degrades the dossier to `COMPLETED_WITH_GAPS` (v1 semantics), never halts it. Dossier stop conditions = union of child budgets + a dossier deadline. Fairness (v3 §7) applies across dossiers so one niche can't starve another.

---

## 4. Signal node split (enforcing LAW 1 inside one logical step)

`signal_classify` is **two tasks**, not one, so the boundary is physical:

```
cp:signal → SIGNAL_CLASSIFY (LLM, role=enrich.primary)   evidence.v1 → signal_raw
              verifier: schema_validate
          → SIGNAL_NORMALIZE (deterministic, code_version) signal_raw  → signal.v1
              verifier: normalize_invariants (0≤score≤1, window set, confidence set)
```

The LLM tags *what kind* of signal this evidence is and pulls the raw metric; deterministic code maps raw→normalized within category+window. An LLM never sees the normalization constants and never outputs the normalized value. Repair path per lane (v3 §5): classify failure → `cp:signal:repair`; normalize failure → hard error (deterministic code that fails validation is a bug, not a retry).

---

## 5. Coverage-matrix quorum (the new join)

Per niche, maintain a grid `signal_type × source_tier` of best normalized confidence seen. `niche_coverage` rows are updated as `signal.v1`s land (idempotent upsert). The **COVERAGE_GATE** node (deterministic) admits the SCORING job only when required cells (defined in doc 08's matrix, referenced here) meet `min_cell_confidence`.

```
niche_coverage(niche_id, signal_type, source_tier, best_confidence, contributing_signal_ids[], updated_at)

gate rule: for each required (signal_type, source_tier) in matrix:
             best_confidence >= min_cell_confidence
           OR dossier_deadline reached
```

On deadline with unfilled cells: SCORE still runs, but records `coverage_gaps[]` and applies a confidence penalty on the resulting `opportunity.v1`. Same philosophy as evidence quorum + `COMPLETED_WITH_GAPS`, extended to two dimensions. A score is never silently emitted on thin coverage — the gap is always on the record.

---

## 6. Lineage & provenance subsystem (the core of "code traceability")

**The chain.** Every hop is an append-only edge:

```
query_spec → search_task → discovered_url → fetch_task → page.v1
  → enrich_task → evidence.v1 → signal_classify/normalize → signal.v1
  → score_task → opportunity.v1 → decide_task → decision.v1
```

**Storage — two layers, both required:**
- **Inline parent refs** on every artifact (denormalized, fast reads): e.g. `signal.v1.derived_from = [evidence_id...]`, `opportunity.v1.scored_from = [signal_id...]`.
- **`lineage_edges`** table (append-only, full traversal, never updated):
  ```
  lineage_edges(child_kind, child_id, parent_kind, parent_id, relation, code_or_prompt_version, created_at)
  UNIQUE(child_kind, child_id, parent_kind, parent_id)   -- idempotent
  ```

**Trace API (MCP + control API):**
- `lineage.trace(artifact_id)` → transitive parent DAG. For an `opportunity_id` this returns **every** contributing signal, evidence record, source URL, and — at the leaves — the **exact `query_spec`s that generated the evidence**. This is the "surface the queries that produced this" requirement, delivered as a graph walk rather than a bespoke feature.
- `lineage.diff(opportunity_a, opportunity_b)` → what changed between two scorings (inputs and/or versions).
- `lineage.replay(opportunity_id, pin_versions=true)` → re-emits leaf `query_spec`s as a fresh dossier with `extractor_version`/`scoring_version`/`weights_version` pinned → **deterministic re-derivation**. Web drift is absorbed because raw artifacts are content-addressed and retained (v1 §16), so replay can run against cached bytes for a true reproduction or against live web for a refresh.

**Why two layers:** inline refs keep the hot path fast; the edge table makes `trace`/`diff`/`replay` exact and cheap. They must agree — a reconciler check (v1 §2, extended) flags any inline ref without a matching edge.

**Provenance stamp on every artifact** (extends v3 §5): `{code_version | (model_id, quantization, prompt_version), schema_version, weights_version?, config_hash, created_at}`. No artifact is written without it. This is what makes LAW 1 checkable after the fact: you can prove which class produced any value.

---

## 7. New typed artifacts (control-plane fields only)

Only the fields the *control plane* needs are specified here; the semantic/scoring fields are doc 08's. Both validate against JSON Schema before persistence (doc 06 §4).

```json
// signal.v1  — one normalized signal for one niche from one source
{"signal_id":"sig_...", "niche_id":"...", "signal_type":"demand|growth|pain|competition|content",
 "source":{"domain":"...","tier":"open|defended|hostile"}, "window":{"from":"...","to":"..."},
 "normalized_score":0.0, "confidence":0.0, "observed_at":"...", "expires_at":"...",
 "derived_from":["ev_..."], "provenance":{...}, "schema_version":"signal.v1"}

// opportunity.v1 — one scored candidate (score is deterministic; explanation is LLM, kept in separate field)
{"opportunity_id":"opp_...", "niche_id":"...", "candidate":{...},
 "score":0.0, "subscores":{"demand":0.0,"growth":0.0,"pain":0.0,"competition":0.0,"content":0.0},
 "confidence":0.0, "coverage_gaps":[], "constraint_fit":null,
 "scored_from":["sig_..."], "generating_queries":["query_spec_id..."],
 "explanation":{"text":"...", "provenance":{"model_id":"...","prompt_version":"..."}},
 "provenance":{"scoring_version":"...","weights_version":"...","config_hash":"..."},
 "as_of":"...", "schema_version":"opportunity.v1"}
```

`score`/`subscores` and `explanation` are separate fields with separate provenance **on purpose**: the number is reproducible code output; the prose is LLM output. They are never merged.

---

## 8. Idempotency & versioned re-scoring

Extends the 4-level scheme (v1 §15):

```
signal_key      = sha256(niche_id + source + signal_type + window + extractor_version)
opportunity_key = sha256(niche_id + candidate_id + scoring_version + weights_version + input_signal_set_hash)
```

Consequence: identical inputs + identical versions → identical `opportunity_id` (dedup via unique constraint, duplicate work is a no-op). **Change the weights or scoring version → new `opportunity_id`, old one retained** → `lineage.diff` shows exactly what the model change did. This is how you tune the engine without destroying history or trust. Re-scoring is cheap because §4 already persisted normalized signals — you re-run only the deterministic `score()` over existing `signal.v1`s.

---

## 9. Source-tier admission gating

Every source in `source_registry.csv` (doc 06 §4) carries `tier ∈ {open, defended, hostile}` + `legality_note`. The scheduler enforces:

- **Load-bearing rule:** a SCORE may reach full confidence only if its required cells are satisfiable from `open` (+ optionally `defended`) sources. `hostile`/ToS-sensitive sources contribute **best-effort only** and their signals carry a tier-confidence discount, so a score can never *depend* on a source that the degradation ladder will lose by week two.
- Blocked source → circuit opens (doc 06 §2), coverage cell stays unfilled, COVERAGE_GATE routes to substitution or deadline-with-gaps. The no-evasion rule (doc 06 §2.7) is absolute here — a defended source failing never escalates to stealth; it degrades the score's confidence and flags the gap.

The tier of every contributing signal is recorded on `opportunity.v1.subscores` provenance, so "this score leans 60% on a hostile source" is queryable, not hidden.

---

## 10. Signal freshness & re-collection scheduler

Trends decay, so signals are time-boxed control-plane state, not permanent facts:

- Each `signal.v1` has `observed_at` + `expires_at` (half-life per signal_type, config). Expired signals are excluded from scoring and flagged for re-collection.
- A **refresh scheduler** (new control loop, sibling to reconciler) re-emits collection jobs for *tracked* niches on cadence, reusing content-addressed dedup so unchanged pages cost nothing.
- `opportunity.v1.as_of` + a TTL: stale opportunities are marked `EXPIRED` and auto-re-scored (cheap, §8) when fresh signals arrive. The operator LLM sees freshness in `research.status` and can force-refresh.

This is domain-profile TTL (v3 §3) generalized to signals and scores.

---

## 11. Workflow graph additions (nodes as data, doc 07)

New rows in `workflow_nodes` / `workflow_edges`:

```
SIGNAL_CLASSIFY  (llm,  verifier=schema_validate)      → SIGNAL_NORMALIZE
SIGNAL_NORMALIZE (det,  verifier=normalize_invariants) → niche_coverage upsert
COVERAGE_GATE    (det,  verifier=coverage_met)         fan-in → SCORE        [conditional: gate rule §5]
SCORE            (det,  verifier=score_invariants)     → EXPLAIN, DECIDE
EXPLAIN          (llm,  verifier=grounding)            → attaches explanation only (no score mutation)
VALIDATE         (llm+det, nested fan-out sub-graph)   per shortlisted pain point (doc 07 §3 VALIDATOR)
DECIDE           (det re-rank + llm rationale, split)  → decision.v1
```

Back-edge (doc 07): DECIDE may request expanded collection for a niche whose top candidates all fail constraint-fit — novelty-gated, bounded trip count, exactly as the research graph's expand edge. Terminates by the same trip ceiling.

---

## 12. Postgres schema delta (concrete)

```
ALTER research_jobs ADD parent_job_id, job_kind, niche_id, constraints_ref, as_of, ttl_seconds;

niches              (niche_id, name, definition, tracked bool, refresh_cadence, created_at)
niche_coverage      (niche_id, signal_type, source_tier, best_confidence,
                     contributing_signal_ids jsonb, updated_at,
                     UNIQUE(niche_id, signal_type, source_tier))
signals             (signal_id, niche_id, signal_type, source_domain, source_tier,
                     window_from, window_to, normalized_score, confidence,
                     observed_at, expires_at, derived_from jsonb, provenance jsonb,
                     idempotency_key UNIQUE)
opportunities       (opportunity_id, niche_id, candidate_id, score, subscores jsonb,
                     confidence, coverage_gaps jsonb, constraint_fit jsonb,
                     scored_from jsonb, generating_queries jsonb, explanation jsonb,
                     provenance jsonb, as_of, status, idempotency_key UNIQUE)
lineage_edges       (child_kind, child_id, parent_kind, parent_id, relation,
                     version_tag, created_at,
                     UNIQUE(child_kind, child_id, parent_kind, parent_id))
query_specs         (query_spec_id, job_id, text, engine, params jsonb, created_at)  -- lineage leaves
scoring_runs        (run_id, niche_id, scoring_version, weights_version, config_hash,
                     input_signal_set_hash, opportunity_ids jsonb, created_at)       -- diff/replay anchor

INDEX ON signals (niche_id, signal_type, expires_at);
INDEX ON lineage_edges (child_kind, child_id);   -- upward walk
INDEX ON lineage_edges (parent_kind, parent_id); -- downward walk
INDEX ON opportunities (niche_id, status, as_of);
```

`query_specs` and `scoring_runs` exist specifically to make LAW 2 (lineage) and §8 (versioned diff/replay) queryable in O(index), not O(scan).

---

## 13. Fault-injection additions (contract isn't met until these pass)

Extends doc 06 §7 / v1 §22:

1. **LAW 1 guard** — attempt to persist an `opportunity.v1` whose `score` provenance shows a `model_id` (i.e. an LLM produced the number) → rejected at write, alert fired. The boundary is machine-enforced, not documented-and-hoped.
2. **Lineage completeness** — for a random `opportunity_id`, `lineage.trace` reaches ≥1 `query_spec` leaf and every hop has both an inline ref and a matching `lineage_edges` row; a deliberately orphaned artifact is flagged by the reconciler.
3. **Deterministic replay** — `lineage.replay(opp, pin_versions=true)` against cached artifacts reproduces identical `score`/`subscores` byte-for-byte.
4. **Version diff** — re-score a niche with a bumped `weights_version`; old opportunity retained, `lineage.diff` returns the exact subscore deltas.
5. **Coverage deadline** — starve one required cell; SCORE emits with `coverage_gaps` populated and confidence penalized, never silently.
6. **Tier-loss** — force a hostile source offline mid-dossier; score completes on open sources, confidence discounted, gap flagged, no stealth escalation attempted.
7. **Signal expiry** — age a signal past `expires_at`; it drops from scoring and triggers re-collection; unchanged pages re-dedup to zero fetch cost.

---

## 14. Build order delta (slots into v3 §10 after week 3)

- **Week 4:** `signals`/`opportunities`/`lineage_edges`/`query_specs` schema; the split SIGNAL_CLASSIFY→NORMALIZE nodes; `lineage.trace`. Prove LAW 2 on a single niche before any scoring exists.
- **Week 5:** COVERAGE_GATE + niche_coverage; deterministic SCORE stub (trivial weights) + `score_invariants`; LAW-1 write guard. Prove the boundary and the gate.
- **Week 6:** `lineage.replay`/`diff`, `scoring_runs`, versioned re-scoring; source-tier gating wired to doc 06 circuits.
- **Week 7:** refresh scheduler + expiry; VALIDATE sub-graph + DECIDE. **Only now** does `docs/build/08_signal_engine.md`'s real normalization + weights drop into the SCORE/NORMALIZE slots — onto plumbing already proven traceable and reproducible.

Rationale: every scoring capability is added *after* the traceability that makes it auditable exists. You never have an unexplainable number in the system, even transiently.

---

## 15. Invariant (extended)

> Postgres says what should exist. Redis says what runs now. Workers do the work. The reconciler repairs disagreement.
> The graph owns control flow; nodes own loops; verifiers own truth. Models fill roles; roles are config.
> **LLMs extract and explain; deterministic code scores. No number is emitted by a model.**
> **Every result traces to the queries that made it, and can be replayed. An output you can't trace is a bug, not a result.**
