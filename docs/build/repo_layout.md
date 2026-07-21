# Repo Layout — trail-signal-os

> **Status:** Authoritative repository structure
> **Principle:** One module = one responsibility = one doc that governs it. If a file can't name its governing doc, it doesn't belong.

---

## Dual-Truth Resolution

**Postgres is authoritative.** `source_registry.csv` and `research_evidence.csv` become generated append-only exports from Postgres, not the system of record — preserving the human-readable ledger without a second source of truth.

---

## Full Layout

```
trail-signal-os/
├── AGENTS.md                      # root agent operating instructions
├── README.md
├── docker-compose.yml             # control VM only (Postgres, Redis, SearXNG, control, http/extract workers); OrbStack
├── pyproject.toml
│
├── config/                        # ALL tunables; every file hashed into config_hash
│   ├── models.yaml                # LLM gateway roles (model-agnostic; NEVER a model name in code)
│   ├── sources.yaml               # per-source access mode, fallback chain, rate limits, tier
│   ├── parsers/<domain>.yaml      # Tier-A selector maps
│   ├── phases.yaml                # ACQUIRE/ENRICH/INDEX resource profiles
│   ├── limits.yaml                # token buckets, max_in_flight, budgets
│   ├── queues.yaml                # stream names, priorities, consumer groups
│   ├── weights.yaml               # scoring weights + λ, VERSIONED
│   └── constraints.yaml           # store constraints re-ranker (margin, ship-time, channel)
│
├── schemas/                       # JSON Schema — validated before ANY persistence
│   ├── page.v1.schema.json        # deterministic extraction
│   ├── evidence.v1.schema.json    # LLM-enriched
│   ├── signal.v1.schema.json      # normalized signal
│   ├── opportunity.v1.schema.json # scored candidate
│   ├── decision.v1.schema.json    # constraint-fit verdict
│   ├── job.v1.schema.json
│   ├── task.v1.schema.json
│   ├── budget.v1.schema.json
│   ├── domain_profile.v1.schema.json
│   └── degradation_event.v1.schema.json
│
├── prompts/                       # versioned per node; hash stamped on every output
│   ├── enrich_page.md             # evidence.v1 extraction
│   ├── signal_classify.md         # evidence → raw signal TYPE (LLM side of the boundary)
│   ├── explain_opportunity.md     # rationale ONLY, never the score (LAW 1)
│   ├── planner.md                 # graph nodes
│   ├── gap_analyst.md
│   ├── reviewer.md
│   ├── validator.md
│   └── search_engine.md           # lifted from agent-zero prompts/agent.system.tool.search_engine.md
│
├── harness/                       # LIFTED core, rebound — ADR-001
│   ├── gateway.py                 # models.py + helpers/litellm_transport.py, hooks stripped → roles from models.yaml
│   ├── node_executor.py           # agent.py loop reduced to node-executor scope (no spawning/terminal/memory)
│   └── litellm_adapter.py         # multi-provider adapter (only if mixing native APIs)
│
├── control/                       # THE control process — one container, asyncio loops
│   ├── api/                       # FastAPI, 127.0.0.1 only, bearer token
│   │   ├── app.py
│   │   ├── routes_jobs.py
│   │   ├── routes_workers.py
│   │   ├── routes_domains.py
│   │   ├── routes_dead_letters.py
│   │   └── routes_lineage.py      # trace / diff / replay
│   ├── scheduler/                 # dependency resolve, priority, fairness, admission
│   ├── dispatcher/                # transactional outbox → Redis Streams, batching
│   ├── leases/                    # acquire, heartbeat, fencing, reaper
│   ├── retries/                   # classifier, backoff, circuit_breaker, dead_letter
│   ├── routing/                   # domain profiles, probe, route select, escalation
│   ├── limits/                    # token_bucket.lua, rate_limiter, concurrency, budgets
│   ├── resources/                 # governor, host_metrics (macOS memory_pressure), backpressure
│   └── reconciliation/            # task/counter/stream/artifact + lineage reconciler
│
├── graph/                         # agent graph as DATA
│   ├── defs/                      # workflow YAML → compiled to workflow_defs/nodes/edges
│   │   ├── research.yaml          # discover→fetch→enrich→index→gap→synth→review
│   │   └── dossier.yaml           # signal→coverage→score→explain→validate→decide
│   ├── compiler.py                # YAML → Postgres rows; renders Mermaid from edges
│   ├── executor.py                # compiles ready nodes → tasks
│   └── verifiers/                 # deterministic verifiers catalog
│       ├── schema_validate.py
│       ├── normalize_invariants.py
│       ├── score_invariants.py
│       ├── coverage_met.py
│       ├── claim_grounding.py
│       ├── novelty_floor.py
│       └── decision_valid.py
│
├── signal_engine/                 # THE anti-slop core
│   ├── classify.py                # LLM: evidence.v1 → raw signal (probabilistic side)
│   ├── normalize.py               # DETERMINISTIC: raw → 0–1, winsorize, percentile  [code_version]
│   ├── confidence.py              # DETERMINISTIC: sample × tier × recency
│   ├── coverage.py                # niche_coverage grid + gate rule
│   ├── score.py                   # DETERMINISTIC: geo-mean + interactions + dead-niche gate  [code_version]
│   ├── explain.py                 # LLM: rationale over the score, cites record_ids, NEVER mutates
│   ├── tiers.py                   # tier discount + hostile-dependence cap
│   ├── freshness.py               # half-lives, expiry, re-collection cadence
│   └── decide.py                  # constraint-fit re-rank (det) + rationale (llm, split)
│
├── lineage/                       # code traceability subsystem
│   ├── edges.py                   # append-only lineage_edges writer (idempotent)
│   ├── trace.py                   # transitive parent DAG → leaf query_specs + source URLs
│   ├── diff.py                    # two scorings, what changed (inputs/versions)
│   └── replay.py                  # re-emit leaf queries, pinned versions → deterministic re-derivation
│
├── workers/                       # DATA plane — split by crash isolation
│   ├── http_worker.py             # curl_cffi/httpx + trafilatura (Tier-B) / selectolax (Tier-A)  [container]
│   ├── browser_worker.py          # Crawl4AI/Playwright, tiny budgets, native macOS  [native]
│   ├── media_worker.py            # yt-dlp transcripts, trickle + daily cap  [native]
│   ├── extract_worker.py          # Tier-1 deterministic → page.v1  [container]
│   ├── enrich_worker.py           # local LLM → evidence.v1, constrained decode  [native]
│   ├── document_worker.py         # MarkItDown/pymupdf → page.v1  [container]
│   ├── index_worker.py            # Mongo + Qdrant + Neo4j writes (INDEX phase)  [container]
│   └── search_worker.py           # SearXNG queries → query_specs + urls  [native/container]
│
├── db/
│   ├── migrations/                # Postgres DDL
│   ├── repositories/
│   └── exports/                   # GENERATED append-only CSVs (source_registry, research_evidence)
│
├── mcp/                           # operator surface — FastMCP
│   └── server.py                  # research.* / evidence.* / lineage.* / domains.* / deadletters.*
│
├── storage/                       # content-addressed artifacts (filesystem, not MinIO on 24GB)
│   └── artifacts/{raw,json,markdown,documents,screenshots,extraction}/sha256/…
│
├── observability/                 # structlog JSON + Postgres counters + /metrics (OTel deferred)
│
├── docs/
│   ├── AGENT_BUILD_CONTRACT.md    # build-process contract (Tier 1)
│   ├── adr/
│   │   └── 001_no_framework_fork.md
│   ├── domain/                    # Tier 2 under AGENTS.md
│   │   ├── 00_mission_and_thesis.md
│   │   ├── 01_ontology_and_data_model.md
│   │   ├── 02_research_pipeline.md   # NON-AUTHORITATIVE onboarding only
│   │   ├── 03_evidence_standard.md
│   │   ├── 04_seasonality_engine.md
│   │   ├── 09_safety_compliance_and_ethics.md
│   │   ├── 10_expansion_roadmap.md
│   │   ├── 11_data_dictionary.md
│   │   └── 12_query_grammar.md
│   ├── build/                     # Tier 2 under AGENT_BUILD_CONTRACT
│   │   ├── 06_source_degradation.md
│   │   ├── 07_agent_graph_engineering.md
│   │   ├── 08_signal_engine.md
│   │   ├── 09_verification_harness.md
│   │   ├── control_plane_v3_24gb.md
│   │   ├── control_plane_v4_signal_engine.md
│   │   ├── environment_profile.md    # physical host truth: memory, ports, coexistence
│   │   ├── KICKSTART_PROMPT.md       # operational build-start prompt (not a spec)
│   │   └── repo_layout.md
│   ├── archive/                   # superseded; never governing
│   ├── parallel_acquisition_architecture.md  # design; control-plane wins on conflict
│   └── speed_first_stack.md                  # design; control-plane wins on conflict
│
└── tests/
    ├── fault_injection/           # v1 §22 + doc 06 §7 + LAW-1 guard, lineage completeness, replay, tier-loss
    ├── integration/
    ├── load/
    └── contracts/
```

---

## Origin Summary

- **Lifted-and-rebound per ADR-001** = `harness/` + `prompts/search_engine.md` (~6 files, surgically untangled from Agent Zero's plugin hooks).
- **Everything else is net-new.**
- The ~90% of Agent Zero in the REJECT list never enters the tree.

---

## The Boundary Visible in the Layout

`signal_engine/normalize.py`, `confidence.py`, `score.py`, `tiers.py`, `decide.py` (deterministic, `[code_version]`) sit beside `classify.py`, `explain.py` (LLM).

**Two files, two classes, LAW 1 visible in the filesystem itself.**
