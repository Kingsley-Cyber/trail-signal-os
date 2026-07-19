from __future__ import annotations
import csv
from pathlib import Path
from typing import Iterable, Mapping


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_headers(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return next(csv.reader(handle))


def write_rows(path: Path, rows: Iterable[Mapping[str, object]], fieldnames: list[str] | None = None) -> int:
    materialized = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        if not materialized:
            raise ValueError("fieldnames are required when writing zero rows")
        fieldnames = list(materialized[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(materialized)
    return len(materialized)


def append_row(path: Path, row: Mapping[str, object]) -> None:
    if not path.exists():
        write_rows(path, [row])
        return
    fieldnames = read_headers(path)
    unknown = set(row) - set(fieldnames)
    if unknown:
        raise ValueError(f"Unknown columns for {path}: {sorted(unknown)}")
    with path.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fieldnames).writerow(row)
