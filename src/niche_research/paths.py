from __future__ import annotations
from pathlib import Path


def repo_root(start: Path | None = None) -> Path:
    """Find the repository root by locating pyproject.toml and AGENTS.md."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "AGENTS.md").exists():
            return candidate
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "pyproject.toml").exists():
        return candidate
    raise FileNotFoundError("Repository root not found. Run inside TrailSignal OS.")
