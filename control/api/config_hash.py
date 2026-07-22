"""Deterministic config_hash over config/* (control_plane_v3 §config versioning)."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPO_ROOT / "config"


def hash_config_files(config_root: Path | None = None) -> str:
    """Return sha256 digest of all files under config/, sorted by relative path."""
    root = config_root or CONFIG_ROOT
    if not root.is_dir():
        raise FileNotFoundError(f"config directory not found: {root}")

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(root).as_posix()
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


@lru_cache(maxsize=1)
def current_config_hash() -> str:
    """Cached config hash for the lifetime of the process."""
    return hash_config_files()
