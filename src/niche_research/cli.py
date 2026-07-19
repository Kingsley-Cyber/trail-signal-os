from __future__ import annotations
import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from .csv_store import read_headers, read_rows, write_rows
from .dossier import render_dossier
from .paths import repo_root
from .query_builder import generate_queries
from .scoring import load_scoring_config, score_candidate
from .validator import validate_repository


def resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def cmd_validate(_args: argparse.Namespace) -> int:
    root = repo_root()
    result = validate_repository(root)
    print("VALIDATION", "PASS" if result.ok else "FAIL")
    for rel, count in result.stats.items():
        print(f"  {rel}: {count} rows")
    for warning in result.warnings:
        print(f"WARNING: {warning}")
    for error in result.errors:
        print(f"ERROR: {error}")
    return 0 if result.ok else 1


def cmd_score(args: argparse.Namespace) -> int:
    root = repo_root()
    input_path = resolve(root, args.input)
    output_path = resolve(root, args.output)
    rows = read_rows(input_path)
    config = load_scoring_config(root / "config/scoring_weights.json")
    scored = []
    for row in rows:
        merged = dict(row)
        merged.update(score_candidate(row, config))
        scored.append(merged)
    count = write_rows(output_path, scored)
    print(f"SCORE PASS: wrote {count} rows to {output_path}")
    return 0


def cmd_queries(args: argparse.Namespace) -> int:
    root = repo_root()
    seed = read_rows(root / "data/outdoor_activity_niche_seed.csv")
    selected = [row for row in seed if row["activity_id"] == args.activity_id]
    if not selected:
        print(f"ERROR: activity_id not found: {args.activity_id}", file=sys.stderr)
        return 2
    templates = read_rows(root / "data/search_query_templates.csv")
    queries = generate_queries(selected, templates, args.region, args.year)
    output_path = resolve(root, args.output)
    write_rows(output_path, queries)
    print(f"QUERY PASS: wrote {len(queries)} rows to {output_path}")
    return 0


def cmd_new_run(args: argparse.Namespace) -> int:
    root = repo_root()
    today = date.today().isoformat()
    safe_slug = "-".join(args.slug.lower().split())
    run_dir = root / "research_runs" / f"{today}_{safe_slug}"
    if run_dir.exists() and not args.force:
        print(f"ERROR: run already exists: {run_dir}", file=sys.stderr)
        return 2

    candidates = read_rows(root / "data/niche_candidates.csv")
    candidate = next((row for row in candidates if row["candidate_id"] == args.candidate_id), None)
    if not candidate:
        print(f"ERROR: candidate not found: {args.candidate_id}", file=sys.stderr)
        return 2

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "raw").mkdir(exist_ok=True)
    run_id = f"run-{today}-{safe_slug}"
    run = {
        "research_run_id": run_id,
        "candidate_id": args.candidate_id,
        "state": "researching",
        "created_at": today,
        "last_updated_at": today,
        "owner": args.owner,
        "research_questions": [
            "Is the friction repeated and material?",
            "What independent workarounds exist?",
            "Do current solutions leave a measurable gap?",
            "What evidence would falsify the thesis?",
        ],
        "required_gates": ["g1", "g2", "g3", "g4", "g5", "g6", "g7"],
        "blocked_assumptions": ["No fabricated metrics", "No demand conclusion from one source"],
        "inputs": ["data/niche_candidates.csv", "data/outdoor_activity_niche_seed.csv"],
        "outputs": ["evidence.csv", "queries.csv", "notes.md"],
    }
    (run_dir / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    shutil.copy(root / "templates/research_log.md", run_dir / "notes.md")
    write_rows(run_dir / "evidence.csv", [], read_headers(root / "data/research_evidence.csv"))

    selected = [
        row for row in read_rows(root / "data/outdoor_activity_niche_seed.csv")
        if row["activity_id"] == candidate["activity_id"]
    ]
    queries = generate_queries(
        selected,
        read_rows(root / "data/search_query_templates.csv"),
        args.region,
        args.year,
    )
    write_rows(run_dir / "queries.csv", queries)
    (run_dir / "README.md").write_text(
        f"# Research Run {run_id}\n\n"
        f"Candidate: {args.candidate_id} — {candidate['candidate_title']}\n\n"
        "Raw artifacts are immutable. Normalize findings into `evidence.csv`.\n",
        encoding="utf-8",
    )
    print(f"RUN PASS: created {run_dir}")
    return 0


def cmd_dossier(args: argparse.Namespace) -> int:
    root = repo_root()
    candidates = read_rows(root / "data/niche_candidates.csv")
    candidate = next((row for row in candidates if row["candidate_id"] == args.candidate_id), None)
    if not candidate:
        print(f"ERROR: candidate not found: {args.candidate_id}", file=sys.stderr)
        return 2
    config = load_scoring_config(root / "config/scoring_weights.json")
    score = score_candidate(candidate, config)
    output_path = resolve(root, args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_dossier(candidate, score, read_rows(root / "data/research_evidence.csv")),
        encoding="utf-8",
    )
    print(f"DOSSIER PASS: wrote {output_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="niche-research", description="TrailSignal OS CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("validate")
    command.set_defaults(func=cmd_validate)

    command = sub.add_parser("score")
    command.add_argument("--input", default="data/niche_candidates.csv")
    command.add_argument("--output", default="outputs/scored_niches.csv")
    command.set_defaults(func=cmd_score)

    command = sub.add_parser("queries")
    command.add_argument("--activity-id", required=True)
    command.add_argument("--output", default="outputs/queries.csv")
    command.add_argument("--region", default="United States")
    command.add_argument("--year", type=int, default=date.today().year)
    command.set_defaults(func=cmd_queries)

    command = sub.add_parser("new-run")
    command.add_argument("--slug", required=True)
    command.add_argument("--candidate-id", required=True)
    command.add_argument("--owner", default="unassigned")
    command.add_argument("--region", default="United States")
    command.add_argument("--year", type=int, default=date.today().year)
    command.add_argument("--force", action="store_true")
    command.set_defaults(func=cmd_new_run)

    command = sub.add_parser("dossier")
    command.add_argument("--candidate-id", required=True)
    command.add_argument("--output", default="outputs/dossier.md")
    command.set_defaults(func=cmd_dossier)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
