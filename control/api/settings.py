"""Control API host, port, and bearer token settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONTROL_API_PORT = 8100
DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True)
class ControlApiSettings:
    host: str
    port: int
    bearer_token: str

    @property
    def bind_addr(self) -> str:
        return f"{self.host}:{self.port}"


def load_control_api_settings() -> ControlApiSettings:
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

    token = os.environ.get("CONTROL_API_TOKEN")
    if not token:
        raise RuntimeError(
            "CONTROL_API_TOKEN is required (set in .env or environment before starting control API)"
        )

    return ControlApiSettings(
        host=os.environ.get("CONTROL_API_HOST", DEFAULT_HOST),
        port=int(os.environ.get("CONTROL_API_PORT", str(CONTROL_API_PORT))),
        bearer_token=token,
    )
