from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = {".md", ".csv", ".json", ".jsonl"}
EXCLUDE_PARTS = {".git", ".venv", "__pycache__", "outputs", "raw"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    records = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in INCLUDE:
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_PARTS for part in rel.parts):
            continue
        records.append({
            "artifact_id": "artifact-" + hashlib.sha1(str(rel).encode()).hexdigest()[:12],
            "path": str(rel).replace("\\", "/"),
            "artifact_type": path.suffix.lstrip("."),
            "title": path.stem.replace("_", " ").replace("-", " ").title(),
            "domain": "outdoor_niche_research",
            "status": "active",
            "version": "1.0",
            "checksum_sha256": sha256(path),
        })
    manifest = ROOT / "manifests/rag_manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    with (ROOT / "manifests/rag_manifest.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with (ROOT / "manifests/checksums.sha256").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(f"{record['checksum_sha256']}  {record['path']}\n")
    print(f"MANIFEST PASS: {len(records)} artifacts")


if __name__ == "__main__":
    main()
