# 07 — Agent Graph Engineering

**Audience:** the build agent. Companion to `docs/build/control_plane_v3_24gb.md` and `docs/build/06_source_degradation.md`. This doc elevates the system from a *task* graph to an *agent* graph and pins the model-agnostic harness contract. Supersedes archived `docs/archive/06_agent_orchestration.md`.

---

## 0. The claim, stated honestly

The control plane you're building is **already a graph engine**: tasks + `task_dependencies` + state machines + fan-out (one search → N fetches) + fan-in (evidence quorum) + explicit routing edges (retry, escalation, repair) + shared state in Postgres. That *is* graph engineering — durable, checkpointed, inspectable.

What's missing is the agent layer on top: **typed nodes whose work is an LLM loop, wired by explicit edges, with verifiers as the exit condition of every loop.** Framework vocabulary: a loop is a single node in a graph; you compose loops into a graph when one loop isn't enough. Every LLM node below is a bounded loop; the graph owns all control flow between them.

**Two graphs, never conflated:**
- **Agent graph** = control flow (this doc). Lives in Postgres as data.
- **Knowledge graph** = the evidence itself (Neo4j: product ↔ pain_point ↔ entity). Data, not control.
The agent graph *writes to* and *reads from* the knowledge graph. It is never stored in it.

---

## 1. Layer map (where each engineering layer lives here)

| Layer | Lives in |
|---|---|
| Prompt | `prompts/<node>.md`, versioned, hash recorded per record |
| Context | §6 of v3: token accounting, bundles, manifests, rollups — each node's context is *packed*, never accumulated |
| Harness | **The node contract (§2)** + LLM gateway + MCP tools. Model-independent by construction |
| Loop | Inside each node: bounded iterations + a verifier |
| Graph | `workflow_nodes` / `workflow_edges` as data, executed by the existing scheduler |

The harness answer to "work regardless of model": **a node never names a model.** It names a `role` (`enrich.primary`, `reason.primary`, …) resolved by `config/models.yaml`. Swapping Qwen for Claude for Llama is a YAML edit; the graph, prompts, schemas, and verifiers don't change.

---

## 2. Node contract (the harness spec)

Every node — LLM or deterministic — is declared as data:

```yaml
node:
  id: enrich_page
  kind: llm            # llm | deterministic | human_gate
  role: enrich.primary # gateway role tag, NEVER a model name
  input_schema: page.v1
  output_schema: evidence.v1
  prompt: prompts/enrich_page.md   # versioned; hash stored on output
  loop:
    max_iterations: 2      # 1 attempt + 1 repair reprompt
    max_tool_calls: 0      # enrichment uses no tools; reasoning nodes may
  verifier: schema_validate        # from the verifier catalog, §4
  budget: { tokens: 4000 }
  on_pass: -> next per edges
  on_fail: -> cp:extract:repair
```

Rules:
1. **Typed I/O only.** State flows along edges as schema-validated artifacts (`page.v1`, `evidence.v1`, `rollup.v1`, `decision.v1`) — never as raw transcripts. A node sees its packed input and nothing else. Clean contexts are enforced by construction, not discipline.
2. **Every loop has a verifier and an iteration ceiling.** No verifier, no node. The verifier — not the model — is the quality bottleneck, so verifiers are deterministic wherever possible.
3. **LLM nodes are just tasks.** `kind: llm` compiles to a task in the enrich/reason lane whose executor calls `llm.generate(role, …)`. They inherit leases, fencing, retries, idempotency, circuits — zero new machinery.
4. **Decisions are records.** A reasoning node's output is a `decision.v1` (`{action: expand|stop|synthesize|escalate, args, rationale, cited_manifest_hash}`) — auditable, replayable, and the *edge router* consumes it. The model proposes; the graph disposes.

---

## 3. The TrailSignal research graph

```
                    ┌─────────────┐
  objective ───────►│  PLANNER     │ llm · verifier: plan_validates(budgets)
                    └──────┬──────┘
                           │ plan.v1
                    ┌──────▼──────┐   fan-out N queries
                    │  DISCOVER    │ deterministic (SearXNG)
                    └──────┬──────┘
                           │ urls
                    ┌──────▼──────┐   fan-out per URL (routing engine v3 §3)
                    │ FETCH/PARSE  │ deterministic → page.v1
                    └──────┬──────┘
                           │ page.v1
                    ┌──────▼──────┐
                    │  ENRICH      │ llm per page · verifier: schema_validate
                    └──────┬──────┘
                           │ evidence.v1        ┌────────────────┐
                    ┌──────▼──────┐  fan-in     │  GAP ANALYST    │
                    │  INDEX/ROLLUP│───quorum──► │ llm · reads     │
                    └─────────────┘  edge       │ rollups+manifest│
                                                └───┬──────┬─────┘
                                        decision.v1 │      │ decision.v1
                                          (expand)  │      │ (synthesize)
                           ┌────────────────────────┘      │
                           ▼  back-edge, novelty-gated     ▼
                    ┌─────────────┐                 ┌─────────────┐
                    │  DISCOVER    │                 │ SYNTHESIZER  │ llm · fresh ctx,
                    └─────────────┘                 └──────┬──────┘ rollups+bundles only
                                                           │ synthesis.v1
                                                    ┌──────▼──────┐
                                                    │  REVIEWER    │ llm · fresh ctx ·
                                                    └──────┬──────┘ verifier: claim_grounding
                                                 pass │         │ fail (≤2) → back to
                                                      ▼         ▼   SYNTHESIZER w/ violations
                                                  report    reject list
```

**New node introduced here — REVIEWER.** Fresh context, sees only `synthesis.v1` + the evidence store. Its verifier is mechanical: every claim in the synthesis must cite `record_id`s that exist, and quoted numbers must match the cited records. Fail routes back to the synthesizer with the violation list (max 2 round-trips, then `COMPLETED_WITH_GAPS` + flagged report). This kills the self-rubber-stamping failure mode: the eyes that check are never the eyes that wrote.

**Edge types:** sequence, fan-out, fan-in (quorum: ≥100 validated records, ≥10 domains), conditional (novelty < 5% closes the expand back-edge), repair, and back-edges with bounded trip counts. Back-edge ceilings are what make the graph terminate — an unbounded reject loop is the classic graph failure mode.

**The MCP operator's place:** the interactive LLM you chat with sits *outside* this graph. It launches graphs, reads status/manifests, and can inject `research.expand` — which enters as an event on the GAP ANALYST's queue, not as a queue mutation. Optionally, PLANNER and GAP ANALYST can be routed to the operator itself (`role: operator.interactive`) for human-in-the-loop runs — same contract, different role binding. That's the model-agnostic harness paying off: autonomous 4B local model or interactive frontier model, identical graph.

---

## 4. Verifier catalog (loop engineering's core, made concrete)

| Verifier | Type | Used by |
|---|---|---|
| `schema_validate` | deterministic (Pydantic) | ENRICH, all typed outputs |
| `plan_validates` | deterministic (budget caps, platform allowlist) | PLANNER |
| `claim_grounding` | deterministic (record_id existence + number match) | REVIEWER |
| `quorum_met` | deterministic (SQL counts) | fan-in edge |
| `novelty_floor` | deterministic (new-entity/claim/domain %) | expand back-edge |
| `decision_valid` | deterministic (enum + args schema + manifest hash cited) | GAP ANALYST |
| `sample_judge` | LLM (`judge` role, 2% sample) | optional QA on ENRICH |

Priority order when designing any new node: deterministic verifier > cheap-LLM verifier > no node. If you can't state "done and correct" as a check, the node isn't ready to exist.

---

## 5. Execution: graph-as-data on the existing control plane (build thin)

**Do not adopt LangGraph as the runtime.** Its in-process `StateGraph` duplicates what Postgres already gives you better for this workload: durable checkpoints, crash recovery, leases, at-least-once + idempotency, multi-hour runs, and mass fan-out (5,000-edge fan-outs are normal here; in-process graph state is not built for that). Adopting it would create a second source of execution truth — the exact dual-truth failure mode we eliminated for CSV vs Postgres.

Instead:

```
workflow_defs      (id, name, version, graph_yaml_hash)
workflow_nodes     (workflow_id, node_id, kind, role, schemas, verifier, loop_budget)
workflow_edges     (workflow_id, from_node, to_node, edge_type, condition_expr, max_trips)
workflow_runs      (run_id, workflow_id, job_id, status, started_at)
node_executions    (run_id, node_id, task_id, attempt, verdict, decision_ref)
```

The scheduler compiles ready nodes into tasks; conditional edges evaluate `decision.v1` records and counter conditions; back-edge trip counts live in `node_executions`. Render the graph to Mermaid from these tables — control flow readable as a diagram, generated from the source of truth, never drawn by hand.

**Study, don't adopt (reference implementations):**

| Concept to study | Repo |
|---|---|
| StateGraph API shape, conditional edges | `langchain-ai/langgraph` |
| Graph multi-agent orchestration (GraphFlow) | `microsoft/autogen` |
| Workflow/routing/delegation patterns, A2A | `google/adk-python` |
| Typed-graph + Pydantic-native design | `pydantic/pydantic-ai` (pydantic-graph) |

Verify each at pin time per doc 06 §5. If the interactive operator side ever needs a rich in-session graph (sub-second, no durability needs), LangGraph is acceptable *there* — never for the durable research runs.

---

## 6. Gut check (run it on every future change)

Four questions; this design's answers must stay "yes":
1. **Separate specialized contexts?** Yes by construction — typed I/O, packed inputs, no shared transcript.
2. **Real fan-out/fan-in?** Yes — native to the task graph; quorum edges are the join.
3. **Control flow readable as a diagram?** Yes — generated Mermaid from `workflow_edges`.
4. **Objective and success bar defined?** Yes — per-node verifiers + job stop conditions. If a proposed node has no verifier, the answer here becomes "no," and the change is rejected.

Any change that flips an answer to "no" is a loop wearing a graph diagram — reject it in review.

---

## 7. Salvaged role inventory (from archived `06_agent_orchestration`)

Named agent roles from the superseded orchestration doc map onto this graph's nodes.
Roles are gateway tags / node identities — never model names. The old "Scorer" role is
**not** an LLM node; scoring is deterministic per LAW 1 (`docs/build/08_signal_engine.md`).

| Archived role (06) | Maps onto node(s) in §3 | Kind | Notes |
|---|---|---|---|
| Orchestrator | PLANNER, GAP ANALYST | llm | Scope, state, expand/stop/synthesize decisions |
| Activity expander | PLANNER → DISCOVER | llm → deterministic | Atomic tasks/contexts become plan.v1 + query fan-out |
| Friction miner | ENRICH | llm | Complaint / workaround extraction → evidence.v1 |
| Demand researcher | ENRICH (+ signal classify) | llm | Demand proxies only; no scoring |
| Competition analyst | ENRICH (+ signal classify) | llm | Products, substitutes, pricing as evidence |
| Seasonality analyst | ENRICH / domain `04_seasonality_engine` consumers | llm / deterministic | Windows and lead times as typed evidence |
| Operations analyst | SYNTHESIZER inputs / DECIDE consumers | llm / deterministic | Sourcing, shipping, QC as dossier inputs |
| Risk reviewer | REVIEWER + domain `09_safety_compliance_and_ethics` screen | llm + policy | claim_grounding + exclusion screen |
| Scorer | SCORE / NORMALIZE / CONFIDENCE (signal engine) | **deterministic** | LAW 1 — not an LLM role; engine only |
| Red team | REVIEWER (falsification pass) | llm | Attempts to break claims; verifier owns exit |
| Dossier writer | SYNTHESIZER | llm | Compiles traceable decision material → synthesis.v1 |

Handoff discipline from 06 still applies at the artifact boundary: every edge carries
schema-validated typed I/O (`schemas/agent_handoff.schema.json` where used), not raw
transcripts. Parallelize independent source classes; merge only after independence-group
dedup. On lawful-access failure, ambiguity, unverifiable numerics, schema failure, ToS
block, or unresolved safety issue — record the block; do not fill gaps with inference.

---

## 8. Invariant (extended)

> Postgres says what should exist. Redis says what runs now.
> Workers do the work. The reconciler repairs disagreement.
> **The graph owns control flow. Nodes own loops. Verifiers own truth.**
> **Models fill roles; roles are config; the harness never changes.**
