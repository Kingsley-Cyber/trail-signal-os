# Parallel Acquisition Architecture

> **Status:** Design specification (kept)
> **Authority:** Not Tier-2. On conflict, `docs/build/control_plane_v3_24gb.md`, `docs/build/control_plane_v4_signal_engine.md`, and `docs/build/06_source_degradation.md` win. This file retains unique parallel-lane / throughput detail only.
> **Scope:** High-throughput crawler architecture for mass research
> **Replaces:** Serial per-URL escalation chains (HTTP → Crawl4AI → Playwright → Browser Use)
> **Principle:** Classify once, route thousands of times.

---

## TL;DR

Serial per-URL escalation is too slow for mass research. Instead, build **parallel acquisition lanes** — each URL routes to the correct worker pool based on a cached domain profile, and every lane runs asynchronously. Escalation still exists for unknown domains, but it never stops the fast lanes.

---

## Architecture

```
Research task
      │
      ▼
Query fan-out: 10–50 searches concurrently
      │
      ▼
URL classifier + cached domain profiles
      │
      ├──────── Platform API lane ──────────────┐
      │          Reddit / YouTube / others       │
      │                                          │
      ├──────── Bulk HTTP lane ──────────────────┤
      │          Scrapy + curl_cffi               │
      │                                          │
      ├──────── Known-JS browser lane ────────────┤
      │          Crawl4AI + Playwright             │
      │                                          │
      └──────── Unknown-site probe lane ──────────┤
                one HTTP sample + one browser     │
                                               ▼
                                     Raw response spool
                                               │
                         ┌─────────────────────┴─────────────┐
                         ▼                                    ▼
               Fast HTML extraction                 Document extraction
            Selectolax + Trafilatura                     MarkItDown
                         │                                    │
                         └─────────────────────┬─────────────┘
                                               ▼
                                 Schema validation + JSONL
                                               │
                                               ▼
                                MongoDB / Qdrant / Neo4j
```

The central idea: **classify once, route thousands of times.**

---

## 1. Domain Profile Registry

Create a persistent `domain_profiles` registry. Only the first encounter with an unknown domain pays the classification cost. All remaining URLs route directly to the correct worker pool.

**Schema:**

```json
{
  "domain": "youtube.com",
  "preferred_connector": "youtube_api",
  "requires_browser": false,
  "supports_http": false,
  "parser_schema": "youtube_video_v2",
  "max_concurrency": 16,
  "last_verified_at": "2026-07-21T08:00:00Z",
  "profile_ttl_days": 14
}
```

**Routing examples:**

| Domain | Lane |
|--------|------|
| `youtube.com` | YouTube API immediately |
| `reddit.com` | Approved Reddit connector immediately |
| `docs.python.org` | Scrapy HTTP immediately |
| `medium.com` | HTTP + Trafilatura immediately |
| Known SPA | Crawl4AI browser lane immediately |
| Unknown domain | One short probe race |

### Unknown Domain Probe

For an unknown domain, run two samples concurrently:

```
Sample URL A → Scrapy HTTP
Sample URL B → prewarmed browser
```

The system learns whether the domain is:

- Static HTML
- Embedded JSON / API-driven
- JavaScript rendered
- Login dependent
- Document heavy
- Unsupported or policy restricted

The rest of the crawl proceeds through the winning route.

---

## 2. Scrapy as Mass-Acquisition Engine

Use Scrapy for the overwhelming majority of URLs. It is asynchronous and designed to crawl many domains concurrently. Scrapy's official broad-crawl guidance recommends starting around 100 global concurrent requests and tuning until the crawler reaches approximately 80–90% CPU utilization.

Because the Mac is also running MongoDB, Neo4j, Qdrant, and Redis, begin lower:

```python
# settings.py

CONCURRENT_REQUESTS = 64
CONCURRENT_REQUESTS_PER_DOMAIN = 4

REACTOR_THREADPOOL_MAXSIZE = 20

COOKIES_ENABLED = False
LOG_LEVEL = "INFO"

DOWNLOAD_TIMEOUT = 20
RETRY_TIMES = 2

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.5
AUTOTHROTTLE_START_DELAY = 0.25
AUTOTHROTTLE_MAX_DELAY = 20.0

MEMUSAGE_ENABLED = True
MEMUSAGE_WARNING_MB = 1400
MEMUSAGE_LIMIT_MB = 1800
```

Scrapy AutoThrottle adjusts request spacing independently for each download slot based on server latency, while still honoring per-domain concurrency limits. Many domains can be crawled quickly without one slow domain blocking the entire job.

For sites you own or where higher concurrency is explicitly permitted, create separate domain settings rather than raising the global per-domain limit indiscriminately.

---

## 3. Separate Fetching from Extraction

**Do not** fetch a URL and perform all parsing, Markdown generation, LLM extraction, and database ingestion inside the same worker.

Use two independent stages:

### Stage A: Download quickly

Writes:

```json
{
  "url": "https://example.com/page",
  "status": 200,
  "content_type": "text/html",
  "body_path": "raw/sha256...",
  "body_sha256": "...",
  "fetched_at": "...",
  "headers": {}
}
```

### Stage B: Parse asynchronously

Consumes the stored response and performs:

- HTML parsing
- Boilerplate removal
- Metadata extraction
- Markdown conversion
- JSON schema extraction
- RAG chunking

This prevents CPU-heavy extraction from occupying network concurrency slots.

---

## 4. Fast HTML Parser Before Crawl4AI

For ordinary HTML, use:

- **Selectolax** → DOM and CSS extraction (compiled parser)
- **Trafilatura fast mode** → article/main-text extraction

**Routing logic:**

```python
if known_structured_site:
    parse_with_selectolax(html, cached_schema)

elif article_like_page:
    parse_with_trafilatura(html, fast=True)

elif complex_layout_or_js:
    send_to_crawl4ai(url)
```

Do not convert every response to Markdown immediately. Preserve:

- Raw HTML
- Normalized text
- Structured JSON
- Markdown only when useful

Markdown conversion can run behind the raw-data acquisition pipeline.

---

## 5. Crawl4AI as a Parallel Pool, Not a Fallback Loop

Crawl4AI supports `arun_many()` for concurrent URL batches and provides memory-adaptive or semaphore-based dispatchers.

**Always-warm Crawl4AI service:**

```python
from crawl4ai import (
    AsyncWebCrawler,
    CrawlerRunConfig,
    MemoryAdaptiveDispatcher,
    CacheMode,
)

dispatcher = MemoryAdaptiveDispatcher(
    memory_threshold_percent=72.0,
    check_interval=1.0,
    max_session_permit=2,
    memory_wait_timeout=120.0,
)

config = CrawlerRunConfig(
    cache_mode=CacheMode.ENABLED,
    wait_until="domcontentloaded",
    wait_for_images=False,
    screenshot=False,
    pdf=False,
)

async with AsyncWebCrawler() as crawler:
    results = await crawler.arun_many(
        urls=urls,
        config=config,
        dispatcher=dispatcher,
    )
```

**Speed rules:**

- Use `domcontentloaded`, not `networkidle`
- Do not wait for images
- Do not capture screenshots
- Do not generate PDFs
- Do not beautify HTML
- Do not scroll unless required
- Reuse sessions for same-site traversals
- Keep browser processes warm

Crawl4AI supports browser-session reuse, browser pools, and cached results. Its self-hosted monitoring exposes browser reuse and memory information — useful for ensuring the browser is not relaunched for every page.

---

## 6. Reusable Extraction Schemas

For Amazon-like product listings, Reddit-style threads, directories, news sites, and marketplaces, inspect one representative page and generate a reusable CSS/XPath schema. Then thousands of pages use deterministic extraction.

**Cost model:**

- First page: LLM generates selector schema
- Remaining pages: CSS/XPath extraction with **zero LLM calls**

**Schema directory layout:**

```
schemas/
├── domains/
│   ├── example.com/
│   │   ├── product_listing.v1.json
│   │   ├── product_page.v3.json
│   │   └── review_page.v2.json
│   └── another-site.com/
└── generic/
    ├── article.json
    ├── forum_thread.json
    └── ecommerce_product.json
```

When schema validation starts failing, send only a small sample back to the schema agent for repair.

---

## 7. Platform APIs Run Immediately

Known platform URLs should never be probed through generic HTTP first.

### YouTube

Start the YouTube API worker immediately and paginate multiple videos concurrently. `commentThreads.list` costs one quota unit and returns up to 100 threads per request.

**Example throughput:**

```
50 video IDs
× 5 concurrent video pagers
× 100 comments per response
```

Each video's pages are sequential because of `nextPageToken`, but different videos can be processed concurrently.

- **YouTube API** → comments and metadata
- **yt-dlp** → permitted captions and media metadata

### Reddit

Use a policy-gated Reddit connector rather than generic browser scrolling. Keep post and comment pagination asynchronous, preserve comment-tree structure, and write each retrieved page to the queue immediately.

> **Policy note:** Current Reddit developer rules contain special conditions for using Reddit data with LLMs, including research and training scenarios. This connector needs its own authorization and data-retention policy.

### Amazon

Amazon should not be treated as an unrestricted mass-ingestion source. Amazon's current Associates policies restrict automated extraction, storage, model development, and use of Product Advertising Content — including limitations around reviews, ratings, and caching.

```
Amazon connector
├── authorized API/data source
├── permitted product-page research
└── explicit policy failure
```

**Do not** let the agent silently substitute aggressive browser crawling when an Amazon data route is not authorized.

---

## 8. Browser Use Must Not Block the Job

Browser Use operates as a separate queue:

```
browser_agent_queue
```

When a page requires unknown interaction:

```json
{
  "url": "...",
  "reason": "unknown_interaction_flow",
  "priority": "low",
  "max_actions": 20
}
```

The main research job continues processing all remaining URLs. Browser Use results are merged later if they arrive before the research deadline.

**Reserved for:**

- Unknown filter interfaces
- Multi-step authorized login flows
- Hidden downloads
- Complex infinite scrolling
- Forms that require semantic decisions

**One Browser Use worker on the machine.** Do not launch multiple autonomous browser agents alongside local RAG services.

---

## 9. M1 Max Worker Configuration

### Coexist Mode (RAG services active)

| Worker lane | Initial concurrency |
|-------------|---------------------|
| Search/query workers | 8–16 |
| Platform API requests | 16–32 |
| Scrapy HTTP requests | 64 global |
| Requests per ordinary domain | 2–4 |
| HTML parser processes | 4 |
| Crawl4AI browser pages | 2 |
| Browser Use sessions | 1 |
| MarkItDown document workers | 1–2 |
| Database writer workers | 4 |

**Estimated incremental scraper allocation:**

| Component | Memory |
|-----------|--------|
| Scrapy + HTTP | 1–2 GB |
| Parser workers | 1–2 GB |
| Crawl4AI / browser pool | 3–6 GB |
| Browser Use when active | 1.5–3 GB |
| Queues and raw buffers | 0.5–1.5 GB |
| **Typical scraper budget** | **8–12 GB** |
| **Possible peak** | **12–16 GB** |

> These are capacity-planning estimates, not guaranteed measurements.

### Dedicated Scrape Mode (RAG services stopped)

| Setting | Value |
|---------|-------|
| Scrapy global concurrency | 100 |
| Parser workers | 6–8 |
| Crawl4AI browser pages | 3 |
| Platform API workers | 32 |
| Memory threshold | 78% |

Scrapy's documentation uses 100 as the initial broad-crawl concurrency benchmark, but the correct final value is determined by measured CPU, memory, latency, and error rates.

---

## 10. curl_cffi — Selective Transport

`curl_cffi` supports asynchronous requests, HTTP/2, HTTP/3, and modern browser-style TLS/HTTP fingerprints. Performance is comparable to other high-performance asynchronous clients.

**Routing:**

| Case | Transport |
|------|-----------|
| Normal website | Scrapy downloader |
| HTTP compatibility issue | curl_cffi |
| JavaScript required | Crawl4AI / Playwright |

**Do not** send every request through multiple clients. That wastes bandwidth and duplicates work.

> **Policy note:** Transport fingerprint compatibility is not authorization to bypass access controls or platform restrictions.

---

## 11. Duplicate Work Prevention

Canonical request fingerprint:

```python
sha256(
    normalized_url
    + method
    + relevant_query_parameters
    + body_hash
    + authentication_scope
)
```

**Before fetching, check:**

- Fresh response cache
- Active-job deduplication
- Persistent historical cache
- Content hash cache

Scrapy provides HTTP caching middleware, persistent request queues, duplicate filtering, and resumable job state.

**Cache policies:**

| Content type | TTL |
|--------------|-----|
| Search results | 1–6 hours |
| News articles | 1–24 hours |
| Documentation | 1–7 days |
| Static documents | content-hash based |
| Product data | source-policy dependent |
| Failed URLs | 5–30 minutes |
| Domain profile | 7–30 days |

---

## 12. Final Stack

```
DISCOVERY
├── SearXNG
└── platform search APIs

MASS FETCH
├── Scrapy
└── curl_cffi as alternate transport

FAST EXTRACTION
├── Selectolax
├── Trafilatura
└── cached CSS/XPath schemas

JAVASCRIPT
├── Crawl4AI
└── persistent Playwright pool

AGENTIC EXCEPTIONS
└── Browser Use, asynchronous and nonblocking

PLATFORM CONNECTORS
├── Reddit connector
├── YouTube Data API
├── yt-dlp
└── approved commerce adapters

DOCUMENTS
└── MarkItDown

CONTROL PLANE
├── Redis Streams
├── domain capability registry
├── priority queues
├── retries and dead-letter queue
├── request deduplication
└── resource governor

STORAGE
├── raw compressed responses
├── JSONL streaming output
├── Parquet compaction
├── MongoDB
├── Qdrant
└── Neo4j
```

**Removed from core:**

| Tool | Reason |
|------|--------|
| Firecrawl | Overlapping full-stack alternative |
| Crawlee | Use instead of Scrapy only in a Node.js repo |
| AutoScraper | Schema prototyping only |
| scrcpy | Separate mobile-research environment |

---

## 13. Verification Benchmark

Create a fixed **1,000-URL benchmark**:

| Category | Count |
|----------|-------|
| Static articles | 400 |
| Documentation pages | 200 |
| Forum/thread pages | 150 |
| YouTube videos | 100 |
| Document files | 50 |
| JavaScript applications | 50 |
| Intentionally difficult/unsupported | 50 |

**Record:**

- HTTP pages per second
- API records per second
- Browser pages per minute
- p50 / p95 fetch latency
- p50 / p95 extraction latency
- Browser-routing percentage
- Cache-hit percentage
- 429 and 503 rate
- Schema-validation rate
- Queue age
- RSS memory
- Swap growth
- Failed-job recovery

### Initial Design Goals

| Goal | Target |
|------|--------|
| Handled without a browser | ≥ 80–90% |
| Browser jobs blocking HTTP jobs | Never |
| Browser launched per individual URL | No |
| LLM call per individual page | No |
| Duplicate URLs fetched in one job | Zero |
| Memory usage | < 75–80% |
| Sustained swap growth | None |
| Interrupted crawl resumption | From queue |

---

## Summary

The resulting design is not a slow escalation chain. It is a **high-throughput acquisition fabric** where APIs, HTTP crawling, browser rendering, document parsing, and schema extraction all operate simultaneously.
