"""Load and validate the offline fixture corpus (N3 fixtures-load verifier)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_ROOT = REPO_ROOT / "fixtures"
SCHEMAS_DIR = REPO_ROOT / "schemas"

CAMPING_FIXTURE_DIR = FIXTURES_ROOT / "niches" / "camping-fixture"
CAMPING_EXPECTED_SCORE = 0.72
CAMPING_EXPECTED_CONFIDENCE = 0.65

REQUIRED_PAGE_SOURCES = (
    "article.html",
    "forum_thread.html",
    "marketplace_listing.html",
    "review_page.html",
    "youtube_meta.json",
    "youtube_transcript.vtt",
)

REQUIRED_PAGE_GOLDENS = (
    "article.page.v1.json",
    "forum_thread.page.v1.json",
    "marketplace_listing.page.v1.json",
    "review_page.page.v1.json",
    "youtube.page.v1.json",
)

CASSETTE_KINDS = ("enrich", "classify", "explain")
CASSETTE_REQUIRED_KEYS = ("cassette_kind", "input_hash", "request", "response")


class FixtureLoadError(Exception):
    """Raised when the fixture corpus fails structural or schema validation."""


@dataclass(frozen=True)
class FixtureCorpus:
    pages_dir: Path
    page_goldens: dict[str, dict[str, Any]]
    search_responses: dict[str, dict[str, Any]]
    cassettes: dict[str, list[dict[str, Any]]]
    camping_signals: dict[str, Any]
    camping_expected: dict[str, Any]


def _load_schema(name: str) -> dict[str, Any]:
    path = SCHEMAS_DIR / name
    if not path.is_file():
        raise FixtureLoadError(f"missing schema {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FixtureLoadError(f"missing fixture file {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FixtureLoadError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FixtureLoadError(f"expected JSON object in {path}")
    return payload


def _validate_with_schema(
    instance: dict[str, Any],
    schema_name: str,
    *,
    label: str,
) -> None:
    validator = Draft202012Validator(_load_schema(schema_name))
    try:
        validator.validate(instance)
    except jsonschema.ValidationError as exc:
        raise FixtureLoadError(f"{label} failed {schema_name}: {exc.message}") from exc


def _assert_non_empty_text(path: Path, *, min_chars: int = 80) -> None:
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < min_chars:
        raise FixtureLoadError(f"{path} must contain real offline content (≥{min_chars} chars)")


def _validate_cassette(payload: dict[str, Any], path: Path) -> None:
    missing = [key for key in CASSETTE_REQUIRED_KEYS if key not in payload]
    if missing:
        raise FixtureLoadError(f"{path} missing cassette keys: {', '.join(missing)}")
    kind = payload["cassette_kind"]
    if kind not in CASSETTE_KINDS:
        raise FixtureLoadError(f"{path} has unknown cassette_kind {kind!r}")
    if path.parent.name != kind:
        raise FixtureLoadError(
            f"{path} cassette_kind {kind!r} does not match directory {path.parent.name!r}"
        )
    input_hash = payload["input_hash"]
    if not isinstance(input_hash, str) or not input_hash.startswith("sha256:"):
        raise FixtureLoadError(f"{path} input_hash must start with sha256:")
    response = payload["response"]
    if not isinstance(response, dict) or not response:
        raise FixtureLoadError(f"{path} response must be a non-empty object")


def _validate_camping_fixture(signals: dict[str, Any], expected: dict[str, Any]) -> None:
    signal_items = signals.get("signals")
    if not isinstance(signal_items, list) or not signal_items:
        raise FixtureLoadError("camping-fixture/signals.json must contain a non-empty signals array")
    for index, signal in enumerate(signal_items):
        _validate_with_schema(
            signal,
            "signal.v1.schema.json",
            label=f"camping-fixture signal[{index}]",
        )

    _validate_with_schema(
        expected,
        "opportunity.v1.schema.json",
        label="camping-fixture expected_opportunity.json",
    )
    if expected.get("score") != CAMPING_EXPECTED_SCORE:
        raise FixtureLoadError(
            "expected_opportunity.json score must be "
            f"{CAMPING_EXPECTED_SCORE} (doc 08 §12), got {expected.get('score')!r}"
        )
    if expected.get("confidence") != CAMPING_EXPECTED_CONFIDENCE:
        raise FixtureLoadError(
            "expected_opportunity.json confidence must be "
            f"{CAMPING_EXPECTED_CONFIDENCE} (doc 08 §12), got {expected.get('confidence')!r}"
        )


def validate_fixture_corpus(corpus: FixtureCorpus) -> None:
    """Run all structural and schema checks on a loaded corpus."""
    pages_dir = corpus.pages_dir
    for name in REQUIRED_PAGE_SOURCES:
        path = pages_dir / name
        if not path.is_file():
            raise FixtureLoadError(f"missing page fixture {path}")
        if name.endswith(".html"):
            _assert_non_empty_text(path, min_chars=120)
        elif name.endswith(".json"):
            _load_json(path)
        elif name.endswith(".vtt"):
            _assert_non_empty_text(path, min_chars=40)

    for golden_name in REQUIRED_PAGE_GOLDENS:
        golden = corpus.page_goldens.get(golden_name)
        if golden is None:
            raise FixtureLoadError(f"missing page golden {golden_name}")
        _validate_with_schema(
            golden,
            "page.v1.schema.json",
            label=f"pages/golden/{golden_name}",
        )

    if not corpus.search_responses:
        raise FixtureLoadError("fixtures/search must contain at least one searxng_*.json file")
    for name, payload in corpus.search_responses.items():
        if "query" not in payload or "results" not in payload:
            raise FixtureLoadError(f"search/{name} must include query and results")
        results = payload["results"]
        if not isinstance(results, list) or not results:
            raise FixtureLoadError(f"search/{name} must include a non-empty results array")

    for kind in CASSETTE_KINDS:
        entries = corpus.cassettes.get(kind, [])
        if not entries:
            raise FixtureLoadError(f"fixtures/cassettes/{kind} must contain at least one cassette")
        for entry in entries:
            path = Path(entry["_path"])
            _validate_cassette(entry, path)
            parsed = entry["response"].get("parsed")
            if kind == "enrich" and isinstance(parsed, dict):
                _validate_with_schema(
                    parsed,
                    "evidence.v1.schema.json",
                    label=str(path),
                )

    _validate_camping_fixture(corpus.camping_signals, corpus.camping_expected)


def load_fixtures(root: Path | None = None) -> FixtureCorpus:
    """Load the fixture corpus from disk."""
    fixtures_root = (root or FIXTURES_ROOT).resolve()
    pages_dir = fixtures_root / "pages"
    goldens_dir = pages_dir / "golden"

    page_goldens: dict[str, dict[str, Any]] = {}
    for golden_name in REQUIRED_PAGE_GOLDENS:
        page_goldens[golden_name] = _load_json(goldens_dir / golden_name)

    search_dir = fixtures_root / "search"
    search_responses: dict[str, dict[str, Any]] = {}
    if search_dir.is_dir():
        for path in sorted(search_dir.glob("searxng_*.json")):
            search_responses[path.name] = _load_json(path)

    cassettes: dict[str, list[dict[str, Any]]] = {kind: [] for kind in CASSETTE_KINDS}
    cassettes_root = fixtures_root / "cassettes"
    for kind in CASSETTE_KINDS:
        kind_dir = cassettes_root / kind
        if not kind_dir.is_dir():
            continue
        for path in sorted(kind_dir.glob("*.json")):
            payload = _load_json(path)
            payload["_path"] = str(path)
            cassettes[kind].append(payload)

    camping_signals = _load_json(CAMPING_FIXTURE_DIR / "signals.json")
    camping_expected = _load_json(CAMPING_FIXTURE_DIR / "expected_opportunity.json")

    corpus = FixtureCorpus(
        pages_dir=pages_dir,
        page_goldens=page_goldens,
        search_responses=search_responses,
        cassettes=cassettes,
        camping_signals=camping_signals,
        camping_expected=camping_expected,
    )
    validate_fixture_corpus(corpus)
    return corpus


def main() -> None:
    corpus = load_fixtures()
    print(
        f"Loaded fixtures: {len(corpus.page_goldens)} page goldens, "
        f"{len(corpus.search_responses)} search responses, "
        f"{sum(len(v) for v in corpus.cassettes.values())} cassettes, "
        f"camping-fixture score={corpus.camping_expected['score']}"
    )


if __name__ == "__main__":
    main()
