from __future__ import annotations
from datetime import date
from typing import Iterable, Mapping


def generate_queries(
    activity_rows: Iterable[Mapping[str, str]],
    templates: Iterable[Mapping[str, str]],
    region: str = "United States",
    current_year: int | None = None,
) -> list[dict[str, str]]:
    rows = list(activity_rows)
    if not rows:
        raise ValueError("No activity rows supplied")
    current_year = current_year or date.today().year
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        values = {
            "activity": row.get("activity", ""),
            "task": row.get("task", ""),
            "friction_family": row.get("friction_family", ""),
            "product_territory": row.get("product_territory", ""),
            "region": region,
            "current_year": current_year,
        }
        for template in templates:
            try:
                query = template["template"].format(**values).strip()
            except KeyError as exc:
                raise ValueError(f"Unknown query placeholder {exc} in {template.get('template_id')}") from exc
            normalized = " ".join(query.split()).lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            output.append({
                "query_id": f"query-{len(output)+1:04d}",
                "activity_id": row.get("activity_id", ""),
                "seed_id": row.get("seed_id", ""),
                "evidence_goal": template.get("evidence_goal", ""),
                "template_id": template.get("template_id", ""),
                "query": query,
                "region": region,
                "generated_at": date.today().isoformat(),
                "status": "unsearched",
            })
    return output
