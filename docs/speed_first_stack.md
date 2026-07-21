# Speed-First Open-Source Scraping Stack

> **Status:** Design specification (kept)
> **Authority:** Not Tier-2. On conflict, `docs/build/control_plane_v3_24gb.md`, `docs/build/control_plane_v4_signal_engine.md`, and `docs/build/06_source_degradation.md` win. This file retains unique local/open-source stack and perf detail only.
> **Scope:** Lightweight, parallel scraping optimized for local, open-source execution
> **Principle:** Specialized extractors pull structured comments directly. Scrapy handles mass webpages. Persistent sessions handle TikTok. Browsers stay out of the main path.

---

## TL;DR

Strip away hosted services. Do not use Crawl4AI, Firecrawl, Playwright, or an LLM as the default scraper. Use lightweight platform-specific extractors and direct HTTP workers first.

---

## Final Stack

```
CONTROL
├── PostgreSQL
├── Redis Streams
└── FastAPI

DISCOVERY
└── SearXNG

MASS HTTP
├── Scrapy
├── curl_cffi
└── Selectolax

PLATFORM EXTRACTORS
├── YouTube
│   ├── yt-comment-dl
│   └── yt-dlp
├── TikTok
│   ├── PyTok
│   └── TikTokApi
└── Amazon
    └── Custom Scrapy spiders

DIFFICULT PAGES
├── Scrapling
└── Playwright

LLM PREPARATION
├── Trafilatura
├── Crawl4AI for selected pages only
├── MarkItDown for downloaded files
└── JSONL → Parquet

STORAGE
├── Raw compressed files
├── PostgreSQL job state
├── MongoDB documents
├── Qdrant embeddings
└── Neo4j relationships
```

---

## 1. General Internet

```
SearXNG → Scrapy → curl_cffi → Selectolax
```

Scrapy manages thousands of URLs asynchronously. curl_cffi provides a fast HTTP client capable of matching modern browser-style HTTP and TLS behavior without launching Chromium. Selectolax is a compiled HTML parser designed for fast CSS-selector extraction.

### Workflow

```
Search queries
    ↓
Thousands of URLs discovered
    ↓
64–100 URLs downloaded concurrently
    ↓
Selectolax extracts title, body, links, metadata
    ↓
Useful pages retained
    ↓
Only useful pages receive deeper Markdown processing
```

**Critical:** Crawl4AI should not process every discovered page. It uses Playwright by default, which is substantially heavier than direct HTTP extraction.

### Ranking Funnel

```
10,000 pages downloaded
        ↓
2,000 pages contain useful content
        ↓
500 highest-value pages converted to clean Markdown
```

That change alone makes the system dramatically faster.

---

## 2. YouTube Comments

**Tools:** `yt-comment-dl` + `yt-dlp`

`yt-comment-dl` is a maintained open-source comment downloader that retrieves YouTube comments without requiring an official developer key. It produces line-delimited JSON, which is convenient for streaming large comment sets.

`yt-dlp` retrieves comments, metadata, subtitles, and transcripts, but its current comment extraction accumulates comments before returning final JSON. That makes a dedicated streaming downloader preferable for very large comment sections.

### Concurrency Pattern

```
YouTube worker 1  → Video 1 comments
YouTube worker 2  → Video 2 comments
YouTube worker 3  → Video 3 comments
...
YouTube worker 12 → Video 12 comments
```

**Do not** use Playwright to scroll YouTube comments. The specialized downloader retrieves the underlying comment data directly.

### Recommended Settings

```yaml
youtube:
  concurrent_videos: 8
  pages_per_video: 1
  output: jsonl
```

Increase `concurrent_videos` to 12–16 after measuring memory and request failures.

---

## 3. TikTok Comments

**Tools:** `PyTok` (primary) + `TikTokApi` (alternate) + Playwright (session establishment only)

`PyTok` is an open-source TikTok scraper built around persistent browser profiles. It supports logged-in scraping and maintains reusable sessions rather than starting a fresh browser for every video.

`TikTokApi` provides asynchronous session management and comment extraction, with Playwright used to establish the sessions.

### Fast Design

```
Start 2–4 persistent TikTok sessions
                ↓
Keep browsers and cookies warm
                ↓
Assign many video IDs across sessions
                ↓
Retrieve paginated comments
                ↓
Stream records into JSONL
```

**Do not** launch a new Chromium process for every TikTok video.

### Recommended Pool

```yaml
tiktok:
  persistent_sessions: 3
  videos_per_session: 1
  browser_restart_after_videos: 100
  output_batch_size: 500
```

TikTok is usually the heaviest lane because sessions require browser-backed initialization, but persistent sessions prevent most browser-startup overhead.

---

## 4. Amazon Products and Reviews

**Tools:** `Scrapy` + `curl_cffi` + cached Amazon selectors + Playwright (session recovery only)

There are open-source Scrapy projects containing separate spiders for Amazon search pages, product pages, and review pages. Use these as starting templates, then maintain versioned selectors.

### Design

```
Keyword search pages
        ↓
Extract ASINs and product URLs
        ↓
Product pages scraped concurrently
        ↓
Review pages paginated concurrently across products
        ↓
Reviews written directly to JSONL
```

**Do not** process one product fully before starting the next.

### Parallel Pagination

```
Product A review page 1
Product B review page 1
Product C review page 1
Product D review page 1
        ↓
All run simultaneously
```

### Recommended Settings

```yaml
amazon:
  concurrent_products: 16
  concurrent_requests_per_product: 1
  global_concurrency: 24
  selector_schema: amazon_us_v1
  output_batch_size: 1000
```

Use separate selector profiles for:

- `amazon.com`
- `amazon.de`
- `amazon.co.uk`
- `amazon.ca`

They should not share one assumed page structure.

---

## 5. What Should NOT Be in the Fast Path

### Remove Browser Use from Normal Collection

Browser Use is useful for an agent exploring an unknown interface, but it is too expensive for mass comment collection — each action requires browser state plus LLM reasoning.

**Use it only for:**

- Discovering how an unfamiliar site works
- Finding hidden filters
- Generating a reusable scraper
- Repairing a broken extraction path

```
Agent explores once
        ↓
Agent generates selector or endpoint schema
        ↓
Normal workers reuse it thousands of times
```

### Do Not Use Firecrawl as the Core

Firecrawl overlaps with the scheduler, browser service, extraction service, and Redis infrastructure. The custom repo is faster and lighter when it uses direct Scrapy and platform-specific workers.

### Do Not Run Crawl4AI on Every Comment

Comments are already structured records. Turning every comment into Markdown wastes CPU.

**Use Crawl4AI for:**

- Articles
- Blog posts
- Complex landing pages
- Documentation
- Selected product pages
- Pages requiring cleaned Markdown

**Do not use it for:**

- Individual Reddit comments
- Individual YouTube comments
- Individual TikTok comments
- Individual Amazon reviews

---

## 6. Everything Runs Simultaneously

A single research task might launch:

```
16 query-discovery workers
64 general HTTP requests
8 YouTube video collectors
3 TikTok persistent sessions
16 Amazon product workers
4 HTML extraction processes
2 Playwright fallback pages
2 database batch writers
```

### Example Timeline

**Research request:** "Find customer complaints about portable ice makers"

```
00:00
├── Launch 30 web queries
├── Discover YouTube videos
├── Discover TikTok videos
├── Discover Amazon products
└── Discover forums and blogs

00:05
├── Scrapy downloads normal webpages
├── YouTube comment workers start
├── TikTok session workers start
├── Amazon product workers start
└── Extractors process completed downloads

00:20
├── Thousands of comments already stored
├── Duplicate products and comments removed
├── Useful webpages ranked
└── Top webpages sent to Crawl4AI

00:30+
├── Evidence clustering begins
├── Complaint categories appear
├── RAG indexing begins
└── Collection continues independently
```

There is no global step saying "wait until TikTok finishes before starting YouTube." Every lane operates independently.

---

## 7. Realistic Performance Targets

Initial benchmark targets, not guaranteed rates.

| Lane | Initial Target |
|------|----------------|
| General static web | 300–1,500 pages/minute |
| YouTube comments | 2,000–20,000 comments/minute |
| Amazon product/review pages | 100–500 pages/minute |
| TikTok comments | 500–5,000 comments/minute |
| Browser-rendered pages | 10–40 pages/minute |
| HTML parsing | Thousands of pages/minute |

TikTok and Amazon fluctuate more than generic websites. The important design feature is that their fluctuations do not slow the other lanes.

---

## 8. The Most Important Performance Rules

### 8.1 Never Launch One Browser per URL

```
One Chromium process
├── Context 1
├── Context 2
├── Context 3
└── Context 4
```

Playwright supports programmatic Chromium automation and reusable browser contexts.

### 8.2 Never Call an LLM per Comment

**Wrong:**

```
50,000 comments
× 50,000 LLM requests
```

**Correct:**

```
50,000 comments
        ↓
Code-based cleanup
        ↓
Language and duplicate filtering
        ↓
Batch into groups of 200–1,000
        ↓
Local classification
        ↓
Main LLM receives aggregated evidence
```

### 8.3 Never Write One Database Record at a Time

**Wrong:**

```
Comment downloaded → MongoDB insert → wait → next comment
```

**Correct:**

```
Collect 500–5,000 records → one bulk database write
```

### 8.4 Write Raw Data Before Expensive Processing

```
Download
   ↓
Immediately append to JSONL
   ↓
Acknowledge collection task
   ↓
Parse and enrich asynchronously
```

If an extraction worker crashes, the raw data is already saved.

### 8.5 Use Parquet for Large Completed Batches

```
Live collection      → JSONL
Completed batch      → Parquet
RAG documents        → MongoDB
Embeddings           → Qdrant
Relationships        → Neo4j
```

**Do not** use Neo4j as the first destination for millions of raw comments.

---

## 9. M1 Max Configuration

For 32 GB machine while databases remain active:

```yaml
workers:
  discovery: 12
  http_global_concurrency: 64
  youtube_videos: 8
  tiktok_sessions: 3
  amazon_requests: 16
  html_parser_processes: 4
  browser_pages: 2
  document_workers: 1
  database_writers: 2

batching:
  jsonl_flush_records: 100
  database_bulk_records: 1000
  parquet_compaction_records: 50000

memory:
  stop_new_browser_work_percent: 75
  reduce_http_concurrency_percent: 82
```

### Expected RAM Budget

| Component | Memory |
|-----------|--------|
| Scrapy / curl_cffi workers | 1–2 GB |
| Parsing workers | 1–2 GB |
| YouTube collectors | under 1 GB normally |
| TikTok sessions | 3–6 GB |
| Two browser fallbacks | 2–4 GB |
| Queues and writers | 1–2 GB |
| **Likely scraper usage** | **8–14 GB** |

---

## 10. Selected Stack

```
SearXNG
Scrapy
curl_cffi
Selectolax
Trafilatura

yt-comment-dl
yt-dlp

PyTok
TikTokApi

Custom Amazon Scrapy spiders

Scrapling
Playwright

Crawl4AI only after page ranking
MarkItDown only for documents

Redis Streams
PostgreSQL
JSONL
Polars/PyArrow Parquet
MongoDB
Qdrant
Neo4j
```

This is the speed-first version: specialized extractors pull structured comments directly, Scrapy handles mass webpages, persistent sessions handle TikTok, and browsers are kept out of the main path.
