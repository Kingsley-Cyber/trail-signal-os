# Control Plane v3 — Gap Closure (supersedes v2)

Verified-in-conversation date: 2026-07-21. Companion to `docs/build/06_source_degradation.md`. Sections marked **[carry]** are unchanged from v2 and stated briefly; everything else is new or corrected.

---

## 0. Changelog v2 → v3

1. **LLM Gateway** — model-agnostic layer for enrichment, embeddings, and burst compute. Model choice becomes config, not code.
2. **Context-window & token-budget API design** — token accounting, chunking, bundle packing, rollups, MCP response caps.
3. **Routing engine expanded** — precedence pipeline, probe heuristics, profile TTL/decay, costed escalation.
4. **Backpressure** — inter-lane admission coupling added to phase gating.
5. **Control-plane completeness** — security, config versioning, multi-job fairness, shutdown/startup order, backup, audit.
6. **YouTube lane corrected after live verification** — `transcript_api` demoted from "fallback" to "secondary route, same failure domain." Cookies option removed entirely.
7. **Tool stack table with verification status.**

---

## 1. Runtime & memory **[carry]**

24GB split stands: Docker VM (OrbStack) ~10GB for Postgres/Redis/SearXNG/control/http/extract; native macOS for browser, yt-dlp, local LLM; Mongo/Qdrant/Neo4j capped and phase-gated; filesystem content store; no OTel collector initially.

---

## 2. Phase gating **[carry]** + backpressure (new)

Phase profiles (ACQUIRE / ENRICH / INDEX) stand. Addition — **inter-lane backpressure**, because phase gating alone doesn't stop a fast fetch lane from burying a slow enrich lane:

```
watermarks:
  extract_backlog:  high: 500   low: 200     # pages awaiting Tier-1 parse
  enrich_backlog:   high: 300   low: 100     # pages awaiting LLM enrichment
  index_backlog:    high: 1000  low: 400

rule: scheduler admits new FETCH tasks only while every downstream
      backlog is below its high watermark; resumes at low watermark.
```

Blocked tasks stay `READY` in Postgres — nothing is dropped, admission just pauses. This is what keeps disk and the enrich queue sane on one box.

---

## 3. Routing engine (expanded)

Per-URL decision pipeline, strict precedence:

```
1. POLICY GATE      robots + domain_access rules → BLOCKED or continue
2. REGISTRY         source_registry.csv: tier, access_mode, fallback chain
3. PROFILE          domain_profile if fresh → route directly
4. PROBE            no fresh profile → capability probe (1–2 URLs)
5. ESCALATION       costed ladder, budget-checked
6. CIRCUITS         per domain:route breakers + cooldowns [carry]
```

**Profile freshness:** `ttl_days: 14`. Re-probe when TTL expires **or** `failure_streak ≥ 3`. Success rates are EWMA (α=0.2), so profiles decay toward reality instead of freezing on first impression.

**Probe heuristics (HTTP sample):** text-to-markup ratio, JS-shell markers (`__NEXT_DATA__`, root div with empty body, hydration scripts), embedded JSON-LD, canonical/meta completeness. Outcome writes `preferred_route`, `requires_javascript`, `parser_schema` (or `generic`).

**Costed escalation:** http = cost 1; browser ≈ cost 50 (memory-seconds). Escalation http→browser fires only on shell/empty content, debits `max_browser_pages`, and is denied when the browser budget or ORANGE/RED pressure says no. A denied escalation is a recorded gap, not a retry loop.

---

## 4. Acquisition matrix (updated after live verification)

Unchanged lanes: search (SearXNG), http (curl_cffi/httpx + trafilatura), browser (Crawl4AI/Playwright, tiny budgets), document (MarkItDown).

**YouTube — corrected:**
- **Primary route `youtube:ytdlp`:** guest sessions rate-limit around ~300 videos/hour (~1000 webpage/player requests); add 5–10s sleeps between pulls. Your 100–150/day budget is far inside this. Pin latest release; add a weekly self-update task (extractor fixes ship constantly).
- **No cookies, ever.** Account use risks temporary or permanent bans. Deleted as an option.
- **Subtitle caveat:** some auto-caption downloads on the web client require a *subtitles PO token* and are silently discarded without one; yt-dlp defaults to clients that avoid PO requirements. **Week-2 acceptance test:** verify `--write-auto-sub --skip-download` actually yields transcript files on 10 sample videos before building on it.
- **Secondary route `youtube:transcript_api` — demoted.** Live check shows it sits in the *same* YouTube anti-bot failure domain: cloud IPs are blocked outright, a `PoTokenRequired` error class now exists, and residential throttles reportedly reset in ~24–48h (anecdotal). The library's own recommended workaround is rotating residential proxies — which violates our no-evasion rule, so it's off the table. Keep the route (own circuit, useful when only the ytdlp extractor is broken) but **the true fallback when YouTube is down is cross-source substitution via the http lane** (forums, blog reviews, retailer Q&A), exactly as the degradation ladder in doc 06 already encodes.

---

## 5. LLM Gateway — model-agnostic (new)

Nothing in the control plane names a model. One gateway module, config-bound roles:

```python
llm.generate(role, messages, json_schema=None, max_out=1500, timeout=120)
llm.embed(role, texts)
llm.count_tokens(role, text)
llm.health(role)
```

**`config/models.yaml`:**

```yaml
roles:
  enrich.primary:
    endpoint: http://localhost:11434/v1      # Ollama, OpenAI-compatible
    model_id: qwen3-4b-instruct-q4
    ctx_window: 32768
    max_out: 1500
    supports_schema: server                   # server | grammar | client_only
    cost_per_mtok: {in: 0, out: 0}
    tps_estimate: 35
  enrich.burst:                               # optional: LAN GPU box or RunPod
    endpoint: http://gpu-box.local:8000/v1    # vLLM / llama.cpp server
    model_id: qwen3-32b-awq
    supports_schema: grammar
    cost_per_mtok: {in: 0, out: 0}            # or real $ for cloud
  embed.primary:
    endpoint: http://localhost:11434/v1
    model_id: nomic-embed-text
  judge:                                      # optional QA sampling, 2% of records
    endpoint: ${ANTHROPIC_OR_ANY}/v1
    enabled: false
```

Design rules:
- **One client for everything.** Ollama, llama.cpp server, vLLM, LM Studio, and the cloud providers all speak OpenAI-compatible `/v1/chat/completions` — so a single HTTP client covers local, LAN, burst, and cloud. LiteLLM only if you later mix native APIs.
- **Structured output, layered:** prefer server-side schema enforcement (Ollama structured outputs / GBNF grammar / vLLM guided decoding); *always* Pydantic-validate client-side regardless. Validation fail → one repair reprompt → repair queue. Never trust the backend's promise alone.
- **LLM endpoints are routed resources.** They get the exact same machinery as domains: `endpoint:role` circuit breakers, health probes, RETRY_WAIT on outage. `enrich.primary` down → circuit opens → `enrich.burst` *if* the job's LLM budget allows → else the enrich queue simply holds. No special-case code.
- **Cost gates per job:** `llm_budget: {max_calls, max_tokens, max_usd}`. Burst/cloud roles debit real dollars; local roles debit tokens only. The scheduler checks before dispatch, same as URL budgets.
- **Provenance:** every `evidence.v1` record already carries `extraction: {model, version}` — extend to `{model_id, quantization, prompt_version, schema_version, role}`. Reproducibility requires knowing which brain produced which claim.

---

## 6. Context-window & token-budget API design (new)

**Token accounting at write time.** The gateway exposes `count_tokens` (backend tokenizer when available, chars/4 heuristic otherwise). Store `token_count` on every `page.v1` and `evidence.v1` at creation — packing later becomes arithmetic, not re-tokenization.

**Enrichment input sizing.** If `page_tokens > ctx_window − prompt_overhead − max_out`: chunk by document structure (headings; comment-tree nodes for forums) with ~10% overlap. Each chunk yields records tagged `chunk_id`; a post-pass merges duplicate claims across chunks via content-hash + embedding similarity.

**`evidence.bundle(job_id, query?, filters, max_tokens=6000, cursor?)` algorithm:**

```
1. CANDIDATES   filters (platform, date range, claim_type, min_confidence)
                + optional semantic query via Qdrant
2. DEDUP        exact content_hash, then near-dup collapse (cosine > 0.95)
3. RANK         score = α·relevance + β·recency + γ·diversity,
                MMR so one domain can't monopolize the bundle
4. PACK         greedy fill to max_tokens (compact JSONL, quotes trimmed)
5. RETURN       {records[], manifest}
```

**The manifest is the point:** `{included: 42, excluded: 318, token_total: 5890, coverage_by_domain: {...}, coverage_by_claim_type: {...}, next_cursor}`. The operator LLM always knows what it is *not* seeing, so it can paginate, narrow filters, or expand the job — instead of mistaking a window-sized sample for the whole evidence base.

**Beyond one window — hierarchical access, not bigger prompts:**
- `evidence.rollup(job_id, group_by=pain_point|product|domain)` → deterministic SQL/graph aggregation: claim clusters with counts, sentiment distribution, and exemplar `record_id`s. No LLM in the loop, so rollups are cheap and exact.
- Operator workflow: read rollup → pick clusters → pull targeted bundles → drill into raw records only where it matters. Map-reduce synthesis without ever needing the corpus in context.

**Hard caps:** every MCP tool response ≤ 8k tokens default (configurable), cursor-paginated. `research.status` returns counters and manifests, never content dumps. The operator's context is a scarce resource the control plane actively protects.

---

## 7. Control-plane completeness (new)

- **Security:** control-api binds `127.0.0.1` only; static bearer token from env; the MCP server is the sole client. Destructive tools (`cancel`, `deadletters.requeue`) require `confirm=true`. No inbound ports on the LAN except the optional burst endpoint, which gets its own token.
- **Config versioning:** hash `config/*` at job creation → `config_hash` on `research_jobs`. Config edits never mutate running jobs; reproducing a run = same config hash + same schema versions. Config changes emit `audit_events`.
- **Multi-job fairness:** each lane's dispatch batch picks jobs weighted-round-robin by priority, so a 5,000-URL background job cannot starve a 100-URL interactive one. (v1 named fairness; this is the concrete rule.)
- **Startup/shutdown order:** start → reconciler pass **before** scheduler admits anything. Stop → pause admission → workers drain leases → outbox flush → control loops exit. Redis is explicitly disposable; Postgres + artifacts are the backup surface (nightly `pg_dump` + rsync of `artifacts/`).
- **Audit [carry, made explicit]:** `audit_events` records operator actions, circuit transitions, budget overrides, config changes, tier demotions.
- **Time:** all timestamps UTC ISO-8601, single clock source. No exceptions; retry math depends on it.

---

## 8. Defaults **[carry + gateway additions]**

v2 worker/budget defaults stand. New: `bundle_default_tokens: 6000`, `mcp_response_cap: 8000`, `enrich max_out: 1500`, `rollup_max_clusters: 25`, watermarks per §2.

---

## 9. Tool stack — verification status (as of 2026-07-21)

| Need | Repo | Status |
|---|---|---|
| Media fetch | `yt-dlp/yt-dlp` | **Verified live** — rate limits, PO-token behavior, no-cookies rule confirmed |
| MCP framework | `PrefectHQ/fastmcp` | **Verified live** — now maintained by Prefect; the de-facto standard. Pin the version; import from `fastmcp`, not `mcp.server.fastmcp` |
| Transcript secondary | `jdepoix/youtube-transcript-api` | **Verified live** — active, but same YouTube block domain; secondary route only, never the fallback plan |
| Meta-search | `searxng/searxng` | Verify engine health at pin time |
| Generic extraction | `adbar/trafilatura` | Verify at pin |
| Tier-A parsing | `rushter/selectolax` | Verify at pin |
| Browser lane | `unclecode/crawl4ai`, `microsoft/playwright-python` | Verify at pin |
| Docs → markdown | `microsoft/markitdown` | Verify at pin |
| HTTP w/ browser TLS | `lexiforest/curl_cffi` | Verify at pin |
| Validation | `pydantic/pydantic` | Stable |
| Control API | `fastapi/fastapi` | Stable |
| Local LLM runtime | `ollama/ollama` | Verify structured-output support for chosen model |
| Multi-provider client (optional) | `BerriAI/litellm` | Only if mixing native APIs |
| Constrained decoding (optional) | `dottxt-ai/outlines` | Only if backend lacks server-side schema |
| Embeddings (optional) | `qdrant/fastembed` | Alternative to Ollama embeds |

"Verify at pin" = the build agent checks the repo's current README/issues before locking a version, per doc 06 §5.

---

## 10. Build order delta

- **Week 1:** + LLM gateway stub (Ollama role only) and token accounting land with the http lane.
- **Week 2:** + yt-dlp auto-sub acceptance test (10 videos) gates the media lane.
- **Week 3:** + `evidence.bundle` with manifest, `evidence.rollup`, MCP response caps.
- **Week 4+:** optional `enrich.burst` backend (LAN GPU or RunPod) behind the same gateway — zero code change, one YAML block.

---

## 11. Invariant (final form)

> Postgres says what should exist.
> Redis says what runs now.
> Workers do the work. The reconciler repairs disagreement.
> The LLM decides what's worth knowing next — through MCP, within token budgets it can see.
> **Model choice is config, not code. Context is a budget, not a hope.**
