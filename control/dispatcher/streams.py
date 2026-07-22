"""Resolve Redis stream names from lane and priority using config/queues.yaml."""

from __future__ import annotations

from functools import lru_cache

from control.dispatcher.settings import load_queues_config

_PRIORITY_TO_TIER = {
    0: "high",
    1: "high",
    2: "normal",
    3: "bulk",
}


@lru_cache(maxsize=1)
def _streams_by_lane() -> dict[str, list[str]]:
    config = load_queues_config()
    streams = config.get("streams", {})
    return {lane: spec["tiers"] for lane, spec in streams.items()}


def priority_to_tier(lane: str, priority: int) -> str:
    tiers = _streams_by_lane().get(lane)
    if not tiers:
        raise ValueError(f"unknown lane for stream resolution: {lane}")
    if len(tiers) == 1:
        return tiers[0]
    tier = _PRIORITY_TO_TIER.get(priority, "normal")
    if tier == "bulk" and "bulk" not in tiers and "repair" in tiers:
        tier = "repair"
    if tier not in tiers:
        tier = "normal" if "normal" in tiers else tiers[0]
    return tier


def resolve_stream_name(lane: str, priority: int) -> str:
    config = load_queues_config()
    streams = config.get("streams", {})
    if lane not in streams:
        raise ValueError(f"unknown lane for stream resolution: {lane}")
    prefix = streams[lane]["name_prefix"]
    tier = priority_to_tier(lane, priority)
    return f"{prefix}:{tier}"
