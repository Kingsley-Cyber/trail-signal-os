"""Redis connection settings from config/queues.yaml and environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
QUEUES_PATH = REPO_ROOT / "config" / "queues.yaml"


@dataclass(frozen=True)
class RedisSettings:
    host: str
    port: int
    decode_responses: bool = True


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_queues_config() -> dict:
    with QUEUES_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_redis_settings() -> RedisSettings:
    _load_dotenv()
    queues = load_queues_config()
    redis_cfg = queues.get("redis", {})
    host_env = redis_cfg.get("host_env", "REDIS_HOST")
    port_env = redis_cfg.get("port_env", "REDIS_PORT")
    return RedisSettings(
        host=os.environ.get(host_env, redis_cfg.get("default_host", "127.0.0.1")),
        port=int(os.environ.get(port_env, str(redis_cfg.get("default_port", 6380)))),
    )


def connect_redis(settings: RedisSettings | None = None):
    import redis

    cfg = settings or load_redis_settings()
    return redis.Redis(
        host=cfg.host,
        port=cfg.port,
        decode_responses=cfg.decode_responses,
    )
