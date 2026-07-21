# ADR-001: No framework fork

**Status:** Accepted  
**Date:** 2026-07-21  
**Refs:** `docs/build/repo_layout.md`, `docs/build/07_agent_graph_engineering.md` §5, `docs/AGENT_BUILD_CONTRACT.md` §7

## Decision

Do not fork or adopt an in-process agent framework (LangGraph, AutoGen runtime, etc.) as the durable research execution engine.

## Context

The control plane already provides durable checkpoints, crash recovery, leases, at-least-once delivery with idempotency, multi-hour runs, and mass fan-out via Postgres + Redis. An in-process `StateGraph` would create a second source of execution truth.

## Consequences

- Lift-and-rebind only the thin harness pieces needed (`harness/`, selected prompts), surgically untangled — not a framework fork.
- Study external graph libraries for API shape; do not adopt them as the durable runtime.
- LangGraph (or similar) is acceptable only for optional interactive operator sessions with no durability needs — never for durable research runs.
- Graph-as-data lives in Postgres (`workflow_*` tables); Mermaid is rendered from that source of truth.
