# 06 — Source Degradation & Coverage Contract

> **Audience:** the coding agent building TrailSignal's acquisition layer autonomously, in loops.
> **Scope:** Policy and build contract for source failure handling and coverage guarantees.
> **Read alongside:** `AGENTS.md` and `docs/domain/02_research_pipeline.md` (non-authoritative overview). Where this doc says "you," it means the build agent.

---

## 1. Failure Model (Design Assumption)

**Every source will fail. Design for it; do not hope around it.**

### yt-dlp Specifically

Assume:

- Extractor breakage several times per year (usually fixed upstream within days — keep on latest release and re-check weekly)
- IP rate-limiting at sustained volume (near-certain)
- Occasional token/auth challenges

Residential IPs degrade slower than datacenter IPs. Low volume + sleep intervals is the primary mitigation, not a guarantee.

### Core Invariant

A blocked source degrades the run; it never corrupts it, halts it, or requires manual babysitting. **The correct cost of a source failure is hours of delay and a gap flag — never data loss.**

---

## 2. Degradation Ladder (What the Control Plane Does on Failure)

1. **Classify.** Retry classifier tags the failure (`HTTP_429`, `EXTRACTOR_BROKEN`, `ROBOTS_DISALLOWED`, …). Never a generic `failed=true`.
2. **Open the circuit** for that `domain:route` only (e.g. `youtube:ytdlp` — 12h, then 24h, then 48h on repeat). Pending tasks → `RETRY_WAIT` with `retry_at = cooldown end`.
3. **Consult fallback chain** from the domain profile (e.g. `youtube:transcript_api`, a lighter endpoint with its own circuit). Fallbacks are alternate legitimate routes, never evasion.
4. **Notify the operator LLM** via `research.status` ("media circuit open until X"). Operator substitutes http-lane sources — blog reviews, forum threads, retailer Q&A — via `research.expand`.
5. **Complete the run as `COMPLETED_WITH_GAPS`.** Ledger rows for missing evidence carry `source_gap=true`.
6. **Backfill.** Half-open probe → circuit closes → scheduler re-dispatches waiting tasks → results append to `research_evidence.csv` (append-only; never rewrite). Content-addressed dedup guarantees nothing is fetched twice.
7. **Never:** proxy rotation, fingerprint spoofing, or automatic robots overrides. `403` → `BLOCKED` + human review. **This is a hard rule.**

---

## 3. Coverage Model — Is Every Website Accounted For?

No system parses every website, and this one does not claim to. Coverage is guaranteed generically, not per-site.

### Three Tiers

| Tier | Meaning | Fidelity |
|------|---------|----------|
| **A — Profiled** | Domain has a hand/agent-written parser schema (YAML selector map). Target: top ~20 evidence domains only. | High — `platform_fields` populated |
| **B — Generic** | Everything else. trafilatura main-content extraction → `page.v1` with empty `platform_fields`. | Medium — always available |
| **C — Blocked** | Robots/policy-denied or hard-blocked. Recorded in `source_registry.csv` with reason; substitutes found via SearXNG. | None — gap flagged |

### What "Accounted For" Means

A never-before-seen website is "accounted for" in this sense: the unknown-domain flow samples 1–2 URLs, classifies HTTP vs browser capability, writes a `domain_profile`, then routes, rate-limits, and extracts it at Tier B. It is **not** accounted for in the sense of a bespoke parser existing. You never write per-site code speculatively — only when a domain proves high-value in the evidence ledger.

### Parser Rot

Parser rot is expected. Every Tier A schema carries `schema_version`. If a domain's extraction-validation ratio drops below 90%, auto-demote it to Tier B and open a repair task. **This is a monitored event, not an error.**

---

## 4. What You (the Build Agent) Must Design — Nothing Ships Prebuilt

### JSON Schemas → `schemas/` (extend the repo's existing JSON Schema pattern)

- `page.v1.schema.json` — deterministic extraction output
- `evidence.v1.schema.json` — align fields with `research_evidence.csv` columns
- `domain_profile.v1.schema.json`
- `degradation_event.v1.schema.json` — circuit open/close, fallback used, gap recorded
- `job.v1`, `task.v1`, `budget.v1`

**Validate every artifact against its schema before indexing.** Invalid → repair queue, never the ledger.

### YAML Configs → `config/`

- `sources.yaml` — per source: access modes, fallback chain, rate limits, cooldown policy, robots stance, tier
- `parsers/<domain>.yaml` — Tier A selector maps
- `phases.yaml` — ACQUIRE / ENRICH / INDEX resource profiles (24GB phase gating)
- `limits.yaml` — token buckets, `max_in_flight`, default budgets
- `queues.yaml` — stream names, priorities, consumer groups

### Postgres Migrations

Tables per the control-plane doc §5, plus a `degradation_events` table.

### `source_registry.csv` New Columns

```
access_mode, fallback_mode, cooldown_policy, tier, failure_streak, last_verified_at
```

### MCP Server

FastMCP-based. Tools: `research.create_job` / `status` / `expand` / `pause` / `resume` / `stop`, `evidence.search` / `bundle`, `domains.profile`, `deadletters.list` / `requeue`. Tool input/output schemas mirror the JSON Schemas above. **Tools are thin wrappers over the control API — they never touch Redis or Postgres directly.**

---

## 5. Research Tasks — Verify at Build Time, Do Not Assume

Training knowledge goes stale. Before wiring each component, check current state:

- yt-dlp issue tracker — current rate-limit behavior, PO-token requirements, recommended flags this month
- youtube-transcript-api — still viable as fallback route?
- SearXNG engine config — which upstream engines currently tolerate self-hosted instances (test, don't trust defaults)
- trafilatura vs alternatives — benchmark on your actual top-20 domains
- FastMCP current API surface (moves fast)
- Ollama structured outputs — confirm grammar/JSON-mode support for the chosen enrichment model

---

## 6. GitHub References

| Need | Repo |
|------|------|
| Media fetch | yt-dlp/yt-dlp |
| Transcript fallback route | jdepoix/youtube-transcript-api |
| Meta-search discovery | searxng/searxng |
| Generic extraction (Tier B) | adbar/trafilatura |
| Fast HTML parsing (Tier A) | rushter/selectolax |
| Browser lane | unclecode/crawl4ai, microsoft/playwright-python |
| Docs → markdown | microsoft/markitdown |
| HTTP client w/ browser TLS | lexiforest/curl_cffi |
| MCP framework | jlowin/fastmcp, modelcontextprotocol/python-sdk |
| Schema validation | pydantic/pydantic |
| Control API | fastapi/fastapi |
| Local enrichment LLM | ollama/ollama |

---

## 7. Acceptance Tests (Degradation-Specific — Add to Fault-Injection Suite)

1. **429 storm on `youtube:ytdlp`** → circuit opens, transcript tasks → `RETRY_WAIT`, run proceeds on other lanes, finishes `COMPLETED_WITH_GAPS`, ledger contains `source_gap=true` rows.
2. **Half-open probe succeeds** → waiting tasks re-dispatch, backfill appends, idempotency prevents duplicate ledger records.
3. **Both YouTube routes blocked** → both circuits open, operator notified via `research.status`, substitution branch created through `research.expand`.
4. **Unknown domain end-to-end** → profile written, Tier B extraction passes schema validation.
5. **Deliberately broken Tier A parser** → validation ratio <90% → auto-demotion event fires + repair task opened, run continues at Tier B.

**If all five pass, the degradation contract holds.**
