# Environment Profile — Build & Runtime Host

> **Tier-2 build spec.** Authoritative for the physical environment: hardware, memory budgets,
> ports, service reuse, and coexistence policy. Where `control_plane_v3_24gb.md` §1 assumed a
> dedicated 24GB / OrbStack host, **this file wins**. Verified against the live machine 2026-07-21.

## 1. Hardware (verified)

- Mac Studio, Apple M1 Max, 10 cores (8P/2E), **32 GB** unified RAM, macOS 26.4
- Disk: ~109 GB free at build start (Docker images already consume ~38 GB — prune dead containers before Gate 1)
- **Docker Desktop** (not OrbStack — functional equivalent), VM allocated **25.4 GiB**
- Local LLM: **Ollama, native** (keep ≤ 8 GB resident; 7–8B Q4 class for `enrich.*` roles)

## 2. Shared-host reality — Polymath v33 coexists

The Docker VM is shared with the always-on Polymath stack (~19.5 GiB actual use: Qdrant 5.2,
Neo4j 4.5, 2× ingest workers 6.4, Mongo 2.2, backend/LiteLLM/SearXNG/Redis ~1.4).
**Free VM headroom: ~6 GiB.**

Policy:

- Trail-signal steady-state budget inside the VM: **≤ 5 GiB** (Postgres ~1.5, Redis ~0.3, control API ~0.5, http/extract workers ~2, headroom ~0.7).
- Before any mass-scrape run (Gate 1+): **pause the Polymath ingest workers** (`docker pause polymath_v33-ingest-worker-1 polymath_v33-ingest-worker-2`) → +6.4 GiB. Operator approval required; `docker unpause` after the run.
- Memory-governor watermarks (v3 §2) calibrate against the **shared 25.4 GiB VM**, not a container-local view: GREEN < 70%, ORANGE 70–85%, RED > 85% VM-wide.

## 3. Ports — Polymath owns the defaults; do not collide

Taken by Polymath: `3000, 4000, 6333, 6379, 7474, 7687, 8000, 8080, 8765, 27017`.

| Trail-signal service | Port |
|---|---|
| Postgres | **5433** |
| Redis | **6380** |
| Control API | **8100** |
| MCP server | **8766** |

Service reuse:

- **SearXNG: reuse the running Polymath instance at `:8080`.** Never launch a second one.
- LiteLLM `:4000` is available as a gateway target; Ollama native `:11434` is the primary local role backend.
- Qdrant/Neo4j: reuse Polymath instances with a `ts_` collection/database prefix if those phases arrive; if isolation problems appear, record an ADR and phase-gate a dedicated instance.

## 4. Toolchain notes

- `python` is **not** on PATH — use `python3` (Makefile uses `python3`).
- `niche-research` console script is not installed; use `make validate` / `make test`, or `pip install -e .` in a venv to get the CLI.
- Validation baseline as of this profile: `make validate` exit 0, zero errors.

## 5. Secrets

- `.env` at repo root (gitignored). Never committed, never echoed into logs, commits, or docs.
- `DEEPSEEK_API_KEY` — graphify **semantic** extraction backend (`--backend deepseek`), used for docs/papers passes only.
- Code-graph passes need **no API key** (AST extraction).
- `GEMINI_API_KEY` — optional alternative graphify backend.

## 6. Graphify — mandatory anti-slop gate during build

- Once at build start: `/graphify .` → `graphify-out/` (gitignored).
- After **every** node: `graphify --update` + integration queries (run by the slop-auditor subagent, `.cursor/agents/slop-auditor.md`) — confirm the new module's edges match the node's `depends_on`, no orphans, no unexpected coupling.
- A graphify REJECT is treated as a failed check: fix the code, never the check.
