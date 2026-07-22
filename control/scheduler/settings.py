"""Load scheduler config from config/limits.yaml, phases.yaml, queues.yaml."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LIMITS_PATH = REPO_ROOT / "config" / "limits.yaml"
PHASES_PATH = REPO_ROOT / "config" / "phases.yaml"
QUEUES_PATH = REPO_ROOT / "config" / "queues.yaml"

FETCH_LANES = frozenset({"search", "http", "browser", "media"})
DOWNSTREAM_BACKLOG_LANES = {
    "extract": "extract_backlog",
    "enrich": "enrich_backlog",
    "index": "index_backlog",
}


@lru_cache(maxsize=1)
def load_limits_config() -> dict:
    with LIMITS_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@lru_cache(maxsize=1)
def load_phases_config() -> dict:
    with PHASES_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@lru_cache(maxsize=1)
def load_queues_config() -> dict:
    with QUEUES_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def poll_batch_weight(priority: int) -> int:
    config = load_queues_config()
    weights = config.get("priorities", {}).get("poll_batch_weights", {})
    tier = _priority_to_tier(priority)
    return int(weights.get(tier, weights.get("normal", 4)))


def _priority_to_tier(priority: int) -> str:
    if priority <= 1:
        return "high"
    if priority == 2:
        return "normal"
    return "bulk"
