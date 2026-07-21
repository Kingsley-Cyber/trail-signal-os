"""N0 infra — docker-compose and config structure tests (Gate 0 offline checks)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
CONFIG_DIR = REPO_ROOT / "config"


def _top_level_yaml_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if line.startswith(" ") or line.startswith("\t"):
            continue
        match = re.match(r"^([A-Za-z0-9_]+):", line)
        if match:
            keys.add(match.group(1))
    return keys


def _compose_service_names(compose_text: str) -> set[str]:
    names: set[str] = set()
    in_services = False
    for line in compose_text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^[a-zA-Z]", line) and not line.startswith(" "):
            break
        match = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", line)
        if in_services and match:
            names.add(match.group(1))
    return names


def _service_blocks(compose_text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    in_services = False
    current: str | None = None
    lines: list[str] = []

    for line in compose_text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^[a-zA-Z]", line) and not line.startswith(" "):
            break
        match = re.match(r"^  ([a-zA-Z0-9_-]+):\s*$", line)
        if in_services and match:
            if current is not None:
                blocks[current] = "\n".join(lines)
            current = match.group(1)
            lines = []
            continue
        if in_services and current is not None:
            lines.append(line)

    if current is not None:
        blocks[current] = "\n".join(lines)
    return blocks


class ComposeStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose_text = COMPOSE_PATH.read_text(encoding="utf-8")

    def test_compose_declares_postgres_port_5433(self) -> None:
        self.assertRegex(
            self.compose_text,
            r"\$\{POSTGRES_PORT:-5433\}:5432|5433:5432",
        )

    def test_compose_declares_redis_port_6380(self) -> None:
        self.assertRegex(
            self.compose_text,
            r"\$\{REDIS_PORT:-6380\}:6379|6380:6379",
        )

    def test_compose_does_not_define_searxng_service(self) -> None:
        services = _compose_service_names(self.compose_text)
        self.assertNotIn("searxng", services)
        self.assertNotRegex(
            self.compose_text,
            r"(?m)^  searxng:\s*$",
            msg="searxng service block must not exist; reuse Polymath :8080",
        )

    def test_compose_does_not_define_control_service(self) -> None:
        services = _compose_service_names(self.compose_text)
        self.assertNotIn("control", services)
        self.assertEqual(services, {"postgres", "redis"})

    def test_postgres_password_has_no_committed_default(self) -> None:
        self.assertNotRegex(
            self.compose_text,
            r"POSTGRES_PASSWORD:\s*\$\{POSTGRES_PASSWORD:-",
            msg="POSTGRES_PASSWORD must not have a committed default",
        )
        self.assertIn("POSTGRES_PASSWORD:?", self.compose_text.replace(" ", ""))

    def test_every_service_has_healthcheck(self) -> None:
        blocks = _service_blocks(self.compose_text)
        self.assertGreaterEqual(len(blocks), 1)
        for service, body in blocks.items():
            self.assertRegex(
                body,
                r"(?m)^    healthcheck:",
                msg=f"service {service!r} missing healthcheck block",
            )


class ConfigYamlTests(unittest.TestCase):
    def test_limits_yaml_parses_and_has_required_keys(self) -> None:
        path = CONFIG_DIR / "limits.yaml"
        keys = _top_level_yaml_keys(path)
        self.assertIn("version", keys)
        self.assertIn("token_buckets", keys)
        self.assertIn("max_in_flight", keys)
        self.assertIn("default_budgets", keys)

    def test_queues_yaml_parses_and_has_required_keys(self) -> None:
        path = CONFIG_DIR / "queues.yaml"
        keys = _top_level_yaml_keys(path)
        self.assertIn("version", keys)
        self.assertIn("streams", keys)
        self.assertIn("priorities", keys)
        self.assertIn("redis", keys)

    def test_phases_yaml_parses_and_has_required_keys(self) -> None:
        path = CONFIG_DIR / "phases.yaml"
        keys = _top_level_yaml_keys(path)
        self.assertIn("version", keys)
        self.assertIn("profiles", keys)
        self.assertIn("watermarks", keys)
        self.assertIn("memory_governor", keys)
        for phase in ("ACQUIRE", "ENRICH", "INDEX"):
            self.assertIn(phase, path.read_text(encoding="utf-8"))


class IntegrationCheckInfra(unittest.TestCase):
    """Offline structural integration check for gate-verifier (no Docker required)."""

    def test_compose_uses_env_port_defaults(self) -> None:
        text = COMPOSE_PATH.read_text(encoding="utf-8")
        self.assertIn("${POSTGRES_PORT:-5433}", text)
        self.assertIn("${REDIS_PORT:-6380}", text)

    def test_makefile_bootstrap_sources_env_file(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(". ./.env", makefile)
        self.assertNotRegex(
            makefile,
            r'@test -n "\$\$POSTGRES_PASSWORD"',
            msg="bootstrap must not shell-guard POSTGRES_PASSWORD before .env is loaded",
        )

    def test_all_n0_config_files_present(self) -> None:
        for name in ("limits.yaml", "queues.yaml", "phases.yaml"):
            path = CONFIG_DIR / name
            self.assertTrue(path.is_file(), f"missing {path}")
            self.assertGreater(path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
