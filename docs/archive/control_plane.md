> **ARCHIVED — superseded by `docs/build/control_plane_v3_24gb.md`.** Reference only; not governing.

# Control Plane for Mass-Research Scraper

> **Status:** Design specification
> **Scope:** Job-control plane for the parallel acquisition architecture
> **Invariant:** PostgreSQL says what should exist. Redis says what should run now. Workers perform the work. The reconciler repairs any disagreement.

---

## TL;DR

The control plane does not scrape websites. It decides what work exists, which worker receives it, how much can run concurrently, when a task should retry, whether a domain requires HTTP/API/browser access, whether a worker died, whether the job has gathered enough evidence, and how to resume after Redis, Docker, or a worker restart.

**Foundation:**

| Component | Role |
|-----------|------|
| PostgreSQL | Durable source of truth |
| Redis Streams | Fast work delivery |
| Redis | Rate limits, short-lived locks, counters |
| MinIO / filesystem | Raw HTML, JSON, documents, screenshots |
| FastAPI | Control API |
| OpenTelemetry | Logs, metrics, distributed traces |

Redis Streams consumer groups provide at-least-once delivery, acknowledgements, pending-message tracking, and reassignment of abandoned messages. PostgreSQL remains authoritative because duplicate delivery is still possible and Redis should not be the only record of job state.

---

## 1. Overall Architecture

```
                         RESEARCH AGENT
                               │
                         POST /research-jobs
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                        CONTROL PLANE                            │
│                                                                 │
│  ┌──────────┐   ┌──────────┐   ┌────────────┐   ┌───────────┐ │
│  │ Job API  │──▶│ Planner  │──▶│ Scheduler  │──▶│ Dispatcher│ │
│  └──────────┘   └──────────┘   └────────────┘   └─────┬─────┘ │
│                                                       │       │
│  ┌───────────────┐ ┌────────────┐ ┌────────────────┐  │       │
│  │ Domain Router │ │Rate Limiter│ │Resource Governor│ │       │
│  └───────────────┘ └────────────┘ └────────────────┘  │       │
│                                                       ▼       │
│                                         Redis Streams          │
│                                                                 │
│  PostgreSQL: jobs, tasks, attempts, leases, domain profiles,   │
│              budgets, artifacts, events and dead letters       │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                          DATA PLANE                             │
│                                                                 │
│ Search workers       Platform API workers      HTTP workers    │
│ SearXNG              Reddit/YouTube/etc.       Scrapy/curl_cffi│
│                                                                 │
│ Browser workers      Extraction workers        Document workers│
│ Crawl4AI/Playwright  Selectolax/Trafilatura    MarkItDown      │
│                                                                 │
│ Index workers        Synthesis workers                          │
│ Mongo/Qdrant/Neo4j   Research answer compiler                  │
└─────────────────────────────────────────────────────────────────┘
```

The two planes are deliberately separate. A browser crash should not crash the scheduler. A failed Neo4j write should not cause the URL to be downloaded again.

---

## 2. Core Services

### control-api

Responsibilities:

- Create research jobs
- Validate budgets and policies
- Pause, resume, or cancel jobs
- Return progress
- Inspect failed tasks
- Requeue dead-letter tasks
- Modify domain profiles

**Endpoints:**

```
POST   /v1/research-jobs
GET    /v1/research-jobs/{job_id}
POST   /v1/research-jobs/{job_id}/pause
POST   /v1/research-jobs/{job_id}/resume
POST   /v1/research-jobs/{job_id}/cancel

GET    /v1/research-jobs/{job_id}/tasks
GET    /v1/research-jobs/{job_id}/artifacts

GET    /v1/workers
GET    /v1/queues
GET    /v1/domains/{domain}

GET    /v1/dead-letters
POST   /v1/dead-letters/{task_id}/requeue
```

### planner

Converts a research request into a bounded plan:

```json
{
  "objective": "Find current consumer complaints about portable ice makers",
  "queries": [
    "portable ice maker complaints Reddit",
    "portable ice maker common failures",
    "site:youtube.com portable ice maker review",
    "portable ice maker leaking reviews"
  ],
  "platforms": ["web", "reddit", "youtube"],
  "max_queries": 30,
  "max_urls": 5000,
  "max_urls_per_domain": 500,
  "max_browser_pages": 100,
  "max_download_bytes": 10737418240,
  "deadline_seconds": 1800,
  "minimum_unique_sources": 50
}
```

The planner proposes work. The control plane enforces the limits.

### scheduler

The scheduler:

- Finds tasks whose dependencies are satisfied
- Finds retries whose `retry_at` has arrived
- Checks job and domain budgets
- Applies priority and fairness
- Moves tasks into `READY`
- Creates outbox events

It does not directly call workers.

### outbox-publisher

Reads unpublished events from PostgreSQL and sends them to Redis Streams.

**Transactional outbox pattern:**

```
Database transaction:
1. Insert task
2. Update job counters
3. Insert outbox event
4. Commit

Separate publisher:
5. Read outbox event
6. XADD to Redis Stream
7. Mark outbox event published
```

Multiple publishers can claim different outbox rows with `FOR UPDATE SKIP LOCKED`. Redis publishing should be batched using pipelines.

### reconciler

Repairs inconsistencies every 15–30 seconds locally:

- PostgreSQL task is `READY`, but no Redis message exists
- Redis has a duplicate message for a completed task
- Worker lease expired
- Job counters differ from actual task states
- Artifact exists, but its task was never marked successful
- Job was cancelled while messages remained queued

---

## 3. Queue Topology

Do not use one giant queue.

```
cp:search:high
cp:search:normal
cp:search:bulk

cp:platform:high
cp:platform:normal
cp:platform:bulk

cp:http:high
cp:http:normal
cp:http:bulk

cp:browser:high
cp:browser:normal
cp:browser:repair

cp:extract:high
cp:extract:normal
cp:extract:bulk

cp:document:normal
cp:index:normal
cp:synthesis:normal
```

**Worker consumer groups:**

```
search-workers
platform-workers
http-workers
browser-workers
extract-workers
document-workers
index-workers
synthesis-workers
```

**Weighted polling:**

| Priority | Batches |
|----------|---------|
| High | 8 |
| Normal | 4 |
| Bulk | 1 |
| Repair | only when spare capacity exists |

This prevents a 100,000-page background crawl from delaying an interactive 100-page research request.

### Stream Message

Do not store HTML or large JSON inside Redis.

```json
{
  "task_id": "tsk_01J...",
  "job_id": "job_01J...",
  "lane": "http",
  "priority": 2,
  "attempt": 1,
  "idempotency_key": "sha256:...",
  "payload_ref": "postgres://tasks/tsk_01J...",
  "traceparent": "00-...",
  "created_at": "2026-07-21T10:00:00Z"
}
```

Redis carries references. PostgreSQL and object storage carry durable data.

---

## 4. State Machines

### Research Job

```
CREATED
   ↓
PLANNING
   ↓
DISCOVERING
   ↓
ACQUIRING
   ↓
EXTRACTING
   ↓
INDEXING
   ↓
SYNTHESIZING
   ↓
COMPLETED
```

**Terminal / control states:**

- `PAUSED`
- `CANCEL_REQUESTED`
- `CANCELLED`
- `FAILED`
- `COMPLETED_WITH_GAPS` — result produced, but some sources were inaccessible, policy-blocked, or permanently failed

### Individual Task

```
PENDING
   ↓
READY
   ↓
LEASED
   ↓
RUNNING
   ├──▶ SUCCEEDED
   ├──▶ RETRY_WAIT ──▶ READY
   ├──▶ BLOCKED
   ├──▶ CANCELLED
   └──▶ DEAD
```

Never represent failure with a generic `failed=true`. Preserve the failure class.

---

## 5. PostgreSQL Data Model

| Table | Purpose |
|-------|---------|
| `research_jobs` | Top-level request, status, deadline, budgets |
| `job_stage_counters` | Counts by stage and task state |
| `tasks` | Every unit of work |
| `task_dependencies` | Parent/child and prerequisite relationships |
| `task_attempts` | Every execution attempt and error |
| `outbox_events` | Events waiting to be published to Redis |
| `crawl_targets` | Canonical URLs and routing decisions |
| `domain_profiles` | Learned capability and policy information |
| `domain_limits` | Rate and concurrency settings |
| `workers` | Registered workers and health status |
| `artifacts` | Raw and normalized output references |
| `dead_letters` | Exhausted or manually reviewable tasks |
| `audit_events` | Important state transitions and operator actions |

### Critical `tasks` Indexes

```sql
CREATE INDEX idx_tasks_scheduler
ON tasks (state, not_before, priority, created_at);

CREATE INDEX idx_tasks_retry
ON tasks (retry_at)
WHERE state = 'RETRY_WAIT';

CREATE INDEX idx_tasks_lease_expiry
ON tasks (lease_expires_at)
WHERE state IN ('LEASED', 'RUNNING');

CREATE UNIQUE INDEX idx_tasks_idempotency
ON tasks (idempotency_key);

CREATE INDEX idx_tasks_job_state
ON tasks (job_id, state);
```

---

## 6. Leases and Fencing Tokens

A Redis message being delivered does not prove the worker still owns the task. Workers must acquire a PostgreSQL lease.

```sql
UPDATE tasks
SET
    state = 'LEASED',
    lease_owner = :worker_id,
    lease_generation = lease_generation + 1,
    lease_expires_at = NOW() + :lease_duration,
    last_heartbeat_at = NOW()
WHERE id = :task_id
  AND (
      state = 'READY'
      OR (
          state IN ('LEASED', 'RUNNING')
          AND lease_expires_at < NOW()
      )
  )
RETURNING lease_generation;
```

The returned `lease_generation` is a fencing token. Every result update must include it:

```sql
UPDATE tasks
SET
    state = 'SUCCEEDED',
    completed_at = NOW(),
    result_artifact_id = :artifact_id
WHERE id = :task_id
  AND lease_owner = :worker_id
  AND lease_generation = :lease_generation;
```

**Failure scenario prevented:**

1. Worker A stalls
2. Its lease expires
3. Worker B obtains generation 7
4. Worker A resumes with generation 6
5. Worker A's result is rejected — fencing token stale

### Suggested Leases

| Worker type | Lease | Heartbeat |
|-------------|-------|-----------|
| Search/API | 60s | 20s |
| HTTP | 60s | 20s |
| Browser | 240s | 60s |
| Document | 300s | 60s |
| Extraction | 120s | 30s |
| Indexing | 180s | 45s |

---

## 7. Message Acknowledgement Order

The worker must use this order:

1. Receive Redis message
2. Acquire PostgreSQL lease
3. Perform work
4. Persist artifact
5. Commit PostgreSQL success transaction
6. `XACK` Redis message

**Never acknowledge the message before the database commit.**

If the worker crashes after step 5 but before step 6, Redis redelivers the message. The new worker sees that the task is already `SUCCEEDED` and simply acknowledges the duplicate.

This is at-least-once processing with idempotent effects — not theoretical exactly-once delivery.

Redis tracks delivered-but-unacknowledged messages in its Pending Entries List. `XAUTOCLAIM` can move messages that have remained idle to another consumer, allowing the reaper to recover work from a dead worker.

---

## 8. Domain Routing and Capability Profiles

Every registered domain gets a profile:

```json
{
  "domain": "example.com",
  "preferred_route": "http",
  "fallback_routes": ["browser"],
  "requires_javascript": false,
  "supports_structured_data": true,
  "parser_schema": "article_v3",
  "robots_status": "allowed",
  "authentication_scope": null,

  "requests_per_second": 2.0,
  "burst": 4,
  "max_in_flight": 3,

  "http_success_rate": 0.98,
  "browser_success_rate": 0.94,
  "average_latency_ms": 410,

  "circuit_state": "closed",
  "cooldown_until": null,
  "last_verified_at": "2026-07-21T10:00:00Z"
}
```

### Unknown Domain Flow

Only the first few URLs require profiling:

```
Unknown domain
   │
   ├── HTTP sample
   └── Browser sample when JS indicators exist
          │
          ▼
Create domain profile
          │
          ▼
Route all remaining URLs directly
```

Do not race HTTP and browser on all URLs. Race one or two representative pages and cache the result.

### Route Outcomes

| Condition | Lane |
|-----------|------|
| HTTP content complete | HTTP lane |
| HTML shell / empty content | Browser lane |
| Embedded JSON endpoint found | API / HTTP JSON lane |
| Known platform | Platform connector |
| Authentication needed | Authorized session queue |
| Policy denied | `BLOCKED` |

---

## 9. Rate Limiting and Domain Fairness

Use one token bucket per `eTLD+1 + authentication_scope + connector`:

```
reddit.com:public:reddit_api
youtube.googleapis.com:key_1:youtube_api
example.com:anonymous:http
```

Use Redis Lua so token checking and token consumption happen atomically.

The dispatcher obtains a rate token **before** publishing work to a worker. This prevents HTTP workers from sitting idle while waiting for one overloaded domain.

Also enforce `max_in_flight`:

```
Domain limit: 3
URLs queued: 10,000

Only 3 tasks enter the HTTP stream.
The other 9,997 remain READY in PostgreSQL.
```

### Response Handling

| Status | Action |
|--------|--------|
| 429 | Domain cooldown; respect `Retry-After` |
| 503 | Retry and respect `Retry-After` |
| Timeout / connection | Bounded retry |
| 401 | Credentials required or blocked |
| 403 | Blocked or manual policy review |
| 404 / 410 | Permanent failure |
| Empty static HTML | Browser escalation |
| Invalid schema | Extraction-repair queue |

A `403` must **not** automatically cause the control plane to launch progressively stealthier tools.

---

## 10. Retry Design

### Retryable Failures

```
NETWORK_TIMEOUT
DNS_TEMPORARY_FAILURE
CONNECTION_RESET
HTTP_408
HTTP_429
HTTP_500
HTTP_502
HTTP_503
HTTP_504
BROWSER_CRASH
TEMPORARY_STORAGE_FAILURE
DATABASE_CONNECTION_FAILURE
```

### Non-Retryable Failures

```
HTTP_404
HTTP_410
ROBOTS_DISALLOWED
POLICY_DISALLOWED
AUTHORIZATION_REQUIRED
UNSUPPORTED_CONTENT
INVALID_JOB_INPUT
MAX_RESPONSE_SIZE_EXCEEDED
```

### Retry Schedule

```python
delay = min(
    max_delay,
    base_delay * (2 ** (attempt_number - 1))
)

delay = delay * random.uniform(0.75, 1.25)
```

| Lane | Schedule |
|------|----------|
| HTTP/API | 2s, 4s, 8s, 16s, 32s |
| Browser | 5s, 15s, 45s |
| Database/storage | 1s, 2s, 5s, 10s, 30s |
| 429 | `Retry-After` or domain cooldown policy |

**Retry procedure:**

1. Set task to `RETRY_WAIT`
2. Save `retry_at`
3. Commit
4. Acknowledge the old Redis message
5. Scheduler republishes when `retry_at` arrives

Do not leave intentional retries pending inside Redis.

---

## 11. Circuit Breakers

Track failures per `domain + route`.

**Open circuit when:**

- 10 consecutive transient failures, **or**
- More than 50% of the last 20 requests fail

**Open duration:**

- 5 minutes initially
- Increase to 15, then 30 minutes on repeated failures

**Half-open:**

- Allow one probe

Maintain separate circuits:

```
example.com:http
example.com:browser
example.com:authenticated
```

A static HTTP route can be unhealthy while the browser route remains valid.

---

## 12. Resource Governor

The control plane should control admission, not merely react after the Mac starts swapping.

### Resource Slots

```yaml
resource_slots:
  http:
    maximum: 64
    memory_weight_mb: 15

  parser:
    maximum: 4
    memory_weight_mb: 300

  browser:
    maximum: 2
    memory_weight_mb: 1800

  browser_agent:
    maximum: 1
    memory_weight_mb: 2500

  document:
    maximum: 1
    memory_weight_mb: 1200

  local_llm:
    maximum: 1
    memory_weight_mb: 6000
```

The values are planning estimates. Runtime measurements override them.

### Pressure Levels

| Level | Condition | Action |
|-------|-----------|--------|
| GREEN | Memory < 65%, swap stable | Permit gradual concurrency increases |
| YELLOW | Memory 65–75% | Hold current concurrency |
| ORANGE | Memory 75–82% or swap increasing | Stop admitting browser jobs, reduce parser concurrency, continue lightweight HTTP work |
| RED | Memory > 82%, disk nearly full, repeated OOM | Pause new work, cancel lowest-priority browser session, restart unhealthy worker, preserve queued tasks |

### Docker Memory Limits

```yaml
services:
  control-api:
    mem_limit: 768m
    mem_reservation: 256m

  scheduler:
    mem_limit: 512m
    mem_reservation: 128m

  http-worker:
    mem_limit: 2g
    mem_reservation: 512m

  extraction-worker:
    mem_limit: 2g
    mem_reservation: 512m

  browser-worker:
    mem_limit: 6g
    mem_reservation: 3g

  document-worker:
    mem_limit: 2g
    mem_reservation: 512m
```

---

## 13. Research Budgets and Stop Conditions

Every job needs bounded traversal.

```json
{
  "max_queries": 50,
  "max_discovered_urls": 10000,
  "max_fetched_urls": 5000,
  "max_urls_per_domain": 1000,
  "max_total_bytes": 21474836480,
  "max_browser_pages": 200,
  "max_browser_minutes": 30,
  "max_task_attempts": 5,
  "deadline_seconds": 3600,
  "minimum_source_domains": 10,
  "minimum_evidence_records": 100
}
```

The control plane checks the budget before creating or dispatching each child task.

### Stop Conditions

The job stops when any of these is true:

- Hard deadline reached
- URL budget exhausted
- Byte budget exhausted
- Browser budget exhausted
- Required source coverage achieved
- Evidence novelty falls below threshold
- User cancels the job

**Novelty rule:**

Stop expanding a query branch when the last 100 documents produce:

- Less than 5% new entities
- Less than 5% new claims
- Less than 10% new source domains

This prevents the agent from endlessly crawling near-duplicate pages.

---

## 14. Streaming Execution Without Stage Barriers

Do not wait for all searches to finish before fetching.

```
Search result page 1
   ↓ immediately
URL classification
   ↓ immediately
Fetch
   ↓ immediately
Extraction
   ↓ immediately
Indexing
```

Meanwhile, search pages 2–20 continue.

### Example Live Counters

```json
{
  "search": { "planned": 30, "running": 8, "succeeded": 12 },
  "fetch":  { "planned": 1400, "running": 64, "succeeded": 804 },
  "extract": { "planned": 804, "running": 4, "succeeded": 690 },
  "index":  { "planned": 690, "running": 2, "succeeded": 625 }
}
```

Synthesis can begin after an evidence quorum:

- At least 100 validated records
- At least 10 source domains
- No critical source class missing

The job can later update the synthesis as more evidence arrives.

---

## 15. Idempotency and Deduplication

Use four levels.

### Request Fingerprint

```python
SHA256(
    normalized_url
    + HTTP method
    + relevant query parameters
    + request body hash
    + authentication scope
)
```

### Task Idempotency

```python
SHA256(
    task_type
    + request_fingerprint
    + extractor_version
    + schema_version
)
```

### Content Deduplication

```python
SHA256(normalized_response_bytes)
```

### Extraction Deduplication

```python
SHA256(canonical_normalized_json)
```

Enforce the task key with a PostgreSQL unique constraint. A duplicate Redis message then becomes harmless.

---

## 16. Raw Artifact Handling

Do not place raw content in PostgreSQL or Redis.

```
artifacts/
├── raw/
│   └── sha256/ab/cd/<hash>.html.zst
├── json/
│   └── sha256/...
├── markdown/
│   └── sha256/...
├── documents/
│   └── sha256/...
├── screenshots/
│   └── sha256/...
└── extraction/
    └── sha256/...
```

### Artifacts Table

```json
{
  "artifact_id": "art_01J...",
  "content_hash": "sha256:...",
  "storage_uri": "s3://research/raw/...",
  "media_type": "text/html",
  "compressed_bytes": 18204,
  "uncompressed_bytes": 94772,
  "created_by_task": "tsk_01J...",
  "extractor_version": "crawl4ai-...",
  "schema_version": "article-v3"
}
```

A worker first writes to a temporary path, calculates the hash, then atomically promotes the file to its final path.

---

## 17. Worker Registration and Health

Each worker registers:

```json
{
  "worker_id": "http-macstudio-01",
  "worker_type": "http",
  "hostname": "mac-studio",
  "version": "git:8b73c0a",
  "capacity": 32,
  "active_tasks": 19,
  "memory_bytes": 713031680,
  "last_heartbeat_at": "2026-07-21T10:00:05Z",
  "status": "healthy"
}
```

**Statuses:**

```
STARTING
HEALTHY
DRAINING
PAUSED_RESOURCE_PRESSURE
UNHEALTHY
OFFLINE
```

### Deployment Draining

1. Mark worker `DRAINING`
2. Stop accepting new tasks
3. Complete or release current leases
4. Exit
5. Start new worker version

---

## 18. Cancellation

Cancellation must be cooperative and durable.

```
User requests cancellation
        ↓
Job → CANCEL_REQUESTED
        ↓
Scheduler stops dispatching new tasks
        ↓
Workers check cancellation:
  - before network request
  - during pagination
  - before expensive browser action
  - before indexing
        ↓
Active tasks → CANCELLED
        ↓
Job → CANCELLED
```

Do not delete queued work. Preserve it for audit and possible restart.

---

## 19. Observability

Every task carries:

```
trace_id
job_id
task_id
parent_task_id
attempt_id
worker_id
domain
route
extractor
```

### Required Metrics

**Control plane:**

```
control_jobs_active
control_tasks_ready
control_tasks_running
control_tasks_retry_wait
control_tasks_dead
```

**Queue:**

```
queue_oldest_message_seconds
queue_pending_messages
queue_delivery_attempts
```

**Fetch:**

```
fetch_requests_total
fetch_success_ratio
fetch_bytes_total
fetch_latency_seconds
```

**Domain:**

```
domain_429_total
domain_403_total
domain_circuit_open
```

**Browser:**

```
browser_sessions_active
browser_crashes_total
browser_memory_bytes
```

**Extraction:**

```
extraction_validation_ratio
extraction_empty_total
content_duplicate_ratio
```

**Worker:**

```
worker_heartbeat_age_seconds
worker_lease_expired_total
```

**Host:**

```
host_memory_percent
host_swap_bytes
raw_storage_free_bytes
```

### Alerts

| Condition | Threshold |
|-----------|-----------|
| Oldest READY task | > 60 seconds |
| Pending Redis message | > lease duration |
| Worker heartbeat absent | > 2 heartbeat intervals |
| 429 ratio for a domain | > 10% |
| Browser crash rate | > 5% |
| Validation success | < 90% |
| Memory | > 82% |
| Disk free | < 15% |
| Dead-letter count | increasing |
| No completed task while queue nonempty | 60 seconds |

---

## 20. Repository Layout

```
control_plane/
├── api/
│   ├── app.py
│   ├── routes_jobs.py
│   ├── routes_workers.py
│   ├── routes_domains.py
│   └── routes_dead_letters.py
│
├── models/
│   ├── job.py
│   ├── task.py
│   ├── attempt.py
│   ├── artifact.py
│   ├── domain.py
│   └── worker.py
│
├── db/
│   ├── session.py
│   ├── migrations/
│   ├── repositories/
│   └── transactions.py
│
├── planner/
│   ├── research_plan.py
│   ├── budget.py
│   ├── query_expansion.py
│   └── stop_conditions.py
│
├── scheduler/
│   ├── scheduler.py
│   ├── dependency_resolver.py
│   ├── priority.py
│   ├── fairness.py
│   └── admission.py
│
├── dispatcher/
│   ├── outbox_publisher.py
│   ├── streams.py
│   ├── batching.py
│   └── queue_topology.py
│
├── leases/
│   ├── acquire.py
│   ├── heartbeat.py
│   ├── fencing.py
│   └── reaper.py
│
├── retries/
│   ├── classifier.py
│   ├── backoff.py
│   ├── circuit_breaker.py
│   └── dead_letter.py
│
├── routing/
│   ├── domain_profiles.py
│   ├── capability_classifier.py
│   ├── route_selector.py
│   └── escalation_policy.py
│
├── limits/
│   ├── token_bucket.lua
│   ├── rate_limiter.py
│   ├── concurrency.py
│   └── job_budgets.py
│
├── resources/
│   ├── governor.py
│   ├── host_metrics.py
│   ├── admission_tokens.py
│   └── pressure_policy.py
│
├── reconciliation/
│   ├── task_reconciler.py
│   ├── counter_reconciler.py
│   ├── stream_reconciler.py
│   └── artifact_reconciler.py
│
├── observability/
│   ├── logging.py
│   ├── metrics.py
│   ├── tracing.py
│   └── alerts/
│
├── policies/
│   ├── domain_access.py
│   ├── robots.py
│   ├── authentication.py
│   └── content_safety.py
│
├── schemas/
│   ├── events.py
│   ├── commands.py
│   └── results.py
│
└── tests/
    ├── fault_injection/
    ├── integration/
    ├── load/
    └── contracts/
```

---

## 21. Recommended Local Deployment

For 32 GB M1 Max:

| Component | Memory |
|-----------|--------|
| PostgreSQL | 1–2 GB |
| Redis control-plane data | 512 MB – 1 GB |
| Control API | 512 MB |
| Scheduler/outbox/reaper | 512 MB total |
| HTTP workers | 1–2 GB |
| Extraction workers | 1–2 GB |
| Browser worker | 4–6 GB |
| Document worker | 1–2 GB |
| OpenTelemetry collector | 256–512 MB |

MongoDB, Qdrant, and Neo4j remain part of the data plane — not the job-control database.

**Initial worker settings:**

```
search_workers: 8
platform_api_concurrency: 16
http_concurrency: 64
per_domain_default: 2
parser_processes: 4
browser_pages: 2
browser_agents: 1
document_workers: 1
index_workers: 2
```

---

## 22. Fault-Injection Verification

A control plane is not complete until these tests pass.

### Kill an HTTP Worker

```
docker kill research-http-worker-1
```

**Expected:**

- Message becomes pending
- Lease expires
- Reaper reclaims task
- Another worker completes it
- Only one artifact is committed

### Restart Redis

```
docker restart redis
```

**Expected:**

- PostgreSQL still contains all `READY`/`RUNNING` tasks
- Reconciler republishes missing messages
- No completed URL is downloaded twice permanently

### Publish a Duplicate Message

**Expected:**

- Second worker cannot create a second task result
- Unique idempotency constraint holds
- Duplicate message is acknowledged

### Force a 429 Response

**Expected:**

- Domain cooldown is created
- No new tasks for that domain are dispatched
- Other domains continue at full speed
- Task resumes after `Retry-After`/cooldown

### Kill a Browser During Execution

**Expected:**

- Browser task lease expires
- Task retries with a fresh browser context
- Global HTTP queue remains unaffected

### Force Memory Pressure

```
docker stats
```

**Expected:**

- New browser admission stops
- HTTP workers continue
- No new local LLM task starts
- Concurrency returns gradually after pressure clears

### Cancel a Job

**Expected:**

- No new tasks dispatched
- Active workers observe cancellation
- Artifacts already collected remain available
- Job reaches `CANCELLED`

---

## 23. Temporal Decision

Do not start by placing every URL inside Temporal.

The PostgreSQL + Redis design is better suited to very large numbers of small URL tasks. Temporal can later orchestrate the top-level research workflow when jobs need to remain active for days, pause for human input, or survive complex multi-service failures.

**Later split:**

| Temporal workflow | Redis Streams |
|-------------------|---------------|
| Research job lifecycle | Individual queries |
| Human approvals | Individual URLs |
| Long-running monitoring | Individual extraction tasks |
| Final synthesis | Individual indexing operations |

---

## Final Selected Control Plane

```
FastAPI
+ PostgreSQL
+ Redis Streams
+ Redis token buckets
+ Transactional outbox
+ Leases with fencing tokens
+ Domain capability registry
+ Weighted priority queues
+ Resource governor
+ Retry classifier
+ Circuit breakers
+ Dead-letter queue
+ Reconciliation workers
+ OpenTelemetry
+ Content-addressed artifact storage
```

**The most important invariant:**

> PostgreSQL says what should exist.
> Redis says what should run now.
> Workers perform the work.
> The reconciler repairs any disagreement.
