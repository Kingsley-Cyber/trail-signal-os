> **ARCHIVED — superseded by `docs/build/control_plane_v3_24gb.md`.** Reference only; not governing.

# Control Plane v2 — 24GB M1, API-Free, LLM-Operated

> **Status:** Design specification (supersedes v1 for 24GB machines)
> **Scope:** Rescaled control plane for shared-memory operation, platform-API-free acquisition, LLM-as-operator model, hardened two-tier extraction

---

## 0. What Changed from v1 and Why

Four structural deltas:

1. **Rescaled for 24GB** shared with existing Mongo/Qdrant/Neo4j — v1 assumed 32GB and would swap.
2. **Platform-API lane deleted.** Every source has an open-source acquisition path (SearXNG, yt-dlp, HTML scraping, browser).
3. **The LLM is the operator.** The heuristic planner service and synthesis workers are deleted. You talk to an LLM; it drives the control plane through MCP tools and pulls curated evidence bundles back for reasoning.
4. **Extraction hardened for machine-readability.** Two-tier pipeline with strict per-source JSON schemas, validated before anything gets indexed.

Everything correct in v1 survives unchanged: transactional outbox, Postgres leases + fencing tokens, ack-after-commit ordering, 4-level idempotency, retry classifier, circuit breakers per domain:route, dead letters, reconciler, queue-per-lane topology, and the fault-injection suite.

---

## 1. Hardware Reality (24GB)

Three cuts and one split:

- **Drop MinIO** → plain content-addressed filesystem (`artifacts/sha256/ab/cd/...zst`). Saves ~400MB and a container. Same layout as v1.
- **Defer OpenTelemetry collector** → structlog JSON logs + Postgres counter tables + a `/metrics` endpoint. Add OTel later when debugging actually demands traces.
- **Cap the existing data plane:** Mongo wiredTiger cache 1GB, Neo4j heap 1GB + pagecache 512MB, Qdrant on mmap. Better: phase-gate Neo4j entirely (§2).
- **Split the runtime.** Containers (Postgres, Redis, SearXNG, control, http/extract workers) live in a Docker VM capped at ~10GB — use OrbStack, not Docker Desktop; it's meaningfully lighter. Browser worker, yt-dlp, and the local LLM run native on macOS: ARM-native, Metal access for MLX/Ollama, browser memory doesn't bloat the VM.

### Working Budget

| Component | Cap |
|-----------|-----|
| macOS + apps | ~4GB |
| Docker VM (Postgres 1G, Redis 512M, SearXNG 512M, control 512M, http-worker 1.5G, extract-worker 1.5G, slack) | 10GB |
| Native: browser (1 context, 1–2 pages) | 2.5–3.5GB |
| Native: yt-dlp/ffmpeg | 1GB |
| Native: local LLM (3–4B Q4, enrich phase only) | ~3GB |
| Mongo/Qdrant/Neo4j (capped, phase-gated) | 3–3.5GB |

These don't all peak simultaneously — that's the point of §2.

---

## 2. Phase-Gated Resource Scheduling (New)

v1's governor only reacts to pressure. On 24GB you must also proactively shape load by job phase:

```
ACQUIRE profile:   browser ON, http=32, local LLM OFF, Neo4j STOPPED,
                   enrich + index queues accumulate in Postgres

ENRICH profile:    browser DRAINED, http=8, local LLM ON, parsers=2

INDEX profile:     Neo4j UP, index workers=2, LLM OFF
```

The governor keeps GREEN/YELLOW/ORANGE/RED, but keys thresholds to macOS `memory_pressure` output (normal/warn/critical) plus swap-delta — not raw percent. macOS compresses memory, so raw % lies.

### Rescaled Actions

- **ORANGE** → no new browser pages, parsers 2→1, no LLM admission
- **RED** → SIGSTOP browser process, pause enrich, preserve queues

Tasks don't expire while gated — they sit READY in Postgres until their phase's resources return. This is the biggest 24GB win: the browser and the LLM never fight for the same memory.

---

## 3. API-Free Acquisition Matrix (Replaces Platform Lane)

Delete `cp:platform:*`. Add `cp:media:normal` for yt-dlp jobs.

| Source | Lane | Open-source tool | Notes |
|--------|------|------------------|-------|
| Web articles, blogs, forums | http | curl_cffi/httpx + trafilatura | unchanged from v1 |
| Search discovery | search | SearXNG (self-hosted) | rotate engines in config; replaces all "official search APIs" |
| YouTube | media | `yt-dlp --write-auto-sub --skip-download` | metadata + auto-transcripts, no key. Review-video transcripts are the single highest-value evidence class for product research. ~2 concurrent |
| Reddit | http (special profile) or offline | old.reddit HTML + selectolax schema, or `.json` URL suffix | **Honest note:** Reddit's robots.txt now disallows unregistered crawling, so `policies/robots.py` will flag it as BLOCKED. Decide deliberately: honor it and use open dumps (Arctic Shift / Academic Torrents) for historical data, or override with eyes open at `max_in_flight=1`, `0.5 req/s`, long 429 cooldowns |
| TikTok | browser (tiny budgets) + yt-dlp metadata where it works | volatile, best-effort only |
| Amazon / retail reviews | browser, tiny budgets | heavily defended. Take what polite fetching yields; backfill via complaints that surface on forums/YouTube instead. Keep v1's rule: 403 → BLOCKED + review, never auto-stealth escalation |
| PDFs / docs | document | MarkItDown / pymupdf | unchanged |

---

## 4. LLM as Operator: The MCP Control Surface (New)

Delete the planner service and synthesis lane. The LLM you're talking to (Claude, or a local model) is the planner and synthesizer. Ship a thin MCP server (stdio, runs native, talks to control-api) exposing:

```python
research.create_job(objective, seed_queries[], budgets?)  → job_id
research.status(job_id)          → stage counters, novelty %, budget spend
research.expand(job_id, queries[])   # mid-flight branch expansion
research.pause / resume / stop(job_id)
evidence.search(query, k, job_id?)   → Qdrant semantic hits (evidence records)
evidence.bundle(job_id, filter, max_tokens) → deduped, ranked JSONL sized to context
domains.profile(domain)
deadletters.list() / requeue(task_id)
```

The workflow becomes literal: state a market question → the LLM calls `create_job` → polls `status` → when the evidence quorum hits (v1's rule: ≥100 validated records, ≥10 domains), it pulls `evidence.bundle`, reasons over it, and calls `research.expand` on whatever gaps it finds. Synthesis is interactive LLM work, not a queue.

The control plane still owns all enforcement — budgets, robots, rate limits, phases. **The LLM proposes; the plane disposes. The LLM never touches Redis or Postgres directly.**

---

## 5. Machine-Readable Extraction (Hardened, Two Tiers)

### Tier 1 — Deterministic (`cp:extract`, no LLM)

trafilatura / per-domain selectolax schemas → `page.v1`:

```json
{
  "url": "...",
  "canonical_url": "...",
  "domain": "...",
  "fetched_at": "...",
  "published_at": null,
  "title": "...",
  "author": null,
  "text_md": "...",
  "links": [],
  "media": [],
  "platform_fields": {"subreddit": "...", "score": 412}
}
```

### Tier 2 — LLM Enrichment (new `cp:enrich` lane, local model, ENRICH phase only)

`page.v1` → `evidence.v1`:

```json
{
  "record_id": "ev_01J...",
  "source": {
    "url": "...",
    "domain": "...",
    "platform": "youtube",
    "published_at": "..."
  },
  "product_terms": ["portable ice maker"],
  "entities": [{"name": "GE Opal", "type": "product"}],
  "claims": [
    {
      "text": "pump fails within 6 months",
      "type": "complaint",
      "sentiment": -0.8,
      "confidence": 0.9
    }
  ],
  "pain_points": ["pump failure", "mold in reservoir"],
  "desired_outcomes": ["quieter operation"],
  "price_mentions": [{"amount": 129, "currency": "USD"}],
  "quotes": ["short verbatim fragments, ≤25 words each"],
  "language": "en",
  "schema_version": "evidence.v1",
  "content_hash": "sha256:...",
  "extraction": {
    "model": "qwen3-4b-q4",
    "version": "..."
  }
}
```

### Enforcement

- Pydantic validation + constrained JSON decoding (Ollama structured outputs or llama.cpp grammar). Invalid output → `cp:extract:repair`, never into the index.
- **Model:** Qwen3-4B-Instruct Q4 via Ollama/MLX (~3GB). One instance, one page per prompt, deep queue.
- **Storage:** both schemas as zstd JSONL in the content store; `evidence.v1` → Mongo (truth) + Qdrant (embed the claims, `bge-small`/`nomic-embed` batched during INDEX); Neo4j gets product↔pain_point↔entity edges during INDEX phase only.

This is the "fast machine-readable" target: everything downstream of Tier 1 is validated JSON the LLM can consume raw.

---

## 6. Service Collapse

v1's six services are microservice sprawl for a solo local box. **One control container** runs FastAPI plus asyncio loops:

- scheduler tick (1s)
- outbox publisher
- lease reaper
- reconciler (20s)
- governor (5s)

Separate processes only where crash isolation pays:

- **containers:** http-worker, extract-worker
- **native:** browser-worker, media-worker (yt-dlp), enrich-worker (LLM), MCP server

All workers keep v1's contracts verbatim: acquire Postgres lease with fencing token → work → persist artifact → commit → XACK.

---

## 7. Rescaled Defaults (24GB)

```yaml
search_workers: 4
http_concurrency: 24
per_domain_default: 2          # reddit: 1
parser_processes: 2
browser_pages: 1–2
browser_agents: 0
media_concurrency: 2
enrich_workers: 1
index_workers: 2
```

### Job Budgets

```yaml
max_queries: 30
max_fetched_urls: 2000
per_domain: 300
browser_pages: 60
media_items: 150
max_bytes: 5GB
deadline: 45m
attempts: 4
```

Novelty stop rule unchanged from v1.

### Queues

```
cp:search, cp:http, cp:browser, cp:media, cp:extract, cp:enrich, cp:document, cp:index
```

High/normal/bulk tiers only on search, http, extract; single tier elsewhere.

### Fault-Injection Additions

- Kill Ollama mid-enrich (task → `RETRY_WAIT`, no partial record indexed)
- Kill yt-dlp mid-download (temp file discarded, no orphan artifact)

---

## 8. Build Order

- **Week 1:** Postgres schema, control loops, http lane, SearXNG, Tier-1 extraction, JSONL store → one query end-to-end.
- **Week 2:** media lane (yt-dlp transcripts), dedup/idempotency tests, MCP v0 (`create_job`, `status`, `bundle`).
- **Week 3:** enrich lane (schemas + constrained decoding), Qdrant indexing, `evidence.search`, phase gating.
- **Week 4:** browser lane last, circuit-breaker tuning, fault-injection suite, governor polish.

**Rationale:** transcripts + web articles + forums cover the bulk of product-research evidence before any browser complexity.

---

## 9. Invariant (Updated)

> Postgres says what should exist.
> Redis says what runs now.
> Workers do the work.
> The reconciler repairs disagreement.
> The LLM decides what's worth knowing next — through MCP, never by touching the queues.
