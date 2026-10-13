"""Command-line entry point.

    python -m sourcing_agent --brief briefs/raw/tungsten-automation.md
    python -m sourcing_agent --brief briefs/raw/tungsten-automation.md --stop-after brief_parsing
    python -m sourcing_agent --spec results/tungsten_automation/brief_spec.json --stop-after query_generation
    python -m sourcing_agent --queries results/tungsten_automation/queries.json
    python -m sourcing_agent --search-results results/tungsten_automation/search_results.json
    python -m sourcing_agent --organisations results/tungsten_automation/organisations.json
    python -m sourcing_agent --candidates results/tungsten_automation/candidates.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from . import pipeline
from .config import Settings
from .reporting.console import (
    print_candidate_summary,
    print_final_summary,
    print_query_summary,
    print_resolution_summary,
    print_run_summary,
    print_search_summary,
    print_spec_summary,
)
from .reporting.progress import progress


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Polish briefs contain non-ASCII text; Windows consoles default to a legacy codepage.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _configure_logging(verbose=args.verbose, quiet=args.quiet)

    source: Path = (args.brief or args.spec or args.queries or args.search_results or args.organisations
                    or args.candidates)
    if not source.is_file():
        print(f"error: file not found: {source}", file=sys.stderr)
        return 2
    if not args.brief and args.run_date:
        print("error: --run-date only applies with --brief (a saved spec keeps its own run_date)",
              file=sys.stderr)
        return 2

    if args.output_dir:
        output_dir = args.output_dir
    elif not args.brief:
        output_dir = source.parent
    else:
        output_dir = Path("results") / pipeline.brief_id_from_path(args.brief)

    try:
        settings = Settings.from_env()
        result = pipeline.run(
            output_dir,
            settings=settings,
            brief_path=args.brief,
            spec_path=args.spec,
            queries_path=args.queries,
            search_path=args.search_results,
            organisations_path=args.organisations,
            candidates_path=args.candidates,
            run_date=args.run_date,
            stop_after=args.stop_after,
        )
    except Exception as exc:  # surface a readable message; details are in run_metadata.json
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"run metadata: {output_dir / pipeline.RUN_METADATA_FILE}", file=sys.stderr)
        return 1

    if args.brief:
        print_spec_summary(result.spec)
    else:
        print(f"\nUsing saved {source}  [{result.spec.brief_id}]  run_date={result.spec.run_date}")
    if result.queries and not args.queries:
        print_query_summary(result.queries)
    if result.search and not args.search_results:
        print_search_summary(result.search)
    if result.candidates and not args.candidates:
        print_candidate_summary(result.candidates)
    if result.resolved:
        print_resolution_summary(result.resolved)
    if result.final:
        print_final_summary(result.final, result.output_dir)
    print_run_summary(result.metadata, result.output_dir)
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sourcing-agent",
        description="Run the sourcing pipeline on a project brief.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--brief", type=Path, help="Path to the Markdown brief (parses it first).")
    src.add_argument(
        "--spec", type=Path,
        help="Path to a saved brief_spec.json (skips brief parsing, e.g. after manual review).",
    )
    src.add_argument(
        "--queries", type=Path,
        help="Path to a saved queries.json (runs only the search; brief_spec.json must be in the same folder).",
    )
    src.add_argument(
        "--search-results", type=Path,
        help="Path to a saved search_results.json (runs only candidate extraction; brief_spec.json alongside).",
    )
    src.add_argument(
        "--organisations", type=Path,
        help="Path to a (reviewed) organisations.json: runs only the people search at those organisations "
             "(brief_spec.json and candidates.json alongside).",
    )
    src.add_argument(
        "--candidates", type=Path,
        help="Path to a saved candidates.json: runs profile lookup (people named on pages not yet "
             "looked up), entity resolution, verification and ranking (brief_spec.json alongside).",
    )
    p.add_argument(
        "--output-dir", type=Path, default=None,
        help="Where to write artifacts (default: results/<brief_id>, or the input file's folder).",
    )
    p.add_argument(
        "--run-date", type=date.fromisoformat, default=None,
        help="Reference date for employment windows, YYYY-MM-DD (default: today). Only with --brief.",
    )
    p.add_argument(
        "--stop-after", choices=pipeline.STAGES, default=pipeline.STAGES[-1],
        help="Last stage to run (default: %(default)s).",
    )
    noise = p.add_mutually_exclusive_group()
    noise.add_argument("-v", "--verbose", action="store_true",
                       help="Also log every module's details (each LLM call, search query, file written).")
    noise.add_argument("-q", "--quiet", action="store_true",
                       help="No progress lines while running; only warnings and the final summary.")
    return p.parse_args(argv)


def _configure_logging(*, verbose: bool, quiet: bool) -> None:
    """Progress lines (stage, key numbers, cost) to stderr by default; module details with -v."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
    progress.handlers.clear()  # main() may run more than once in a process (tests)
    progress.addHandler(handler)
    progress.setLevel(logging.WARNING if quiet else logging.INFO)
    progress.propagate = False


if __name__ == "__main__":
    raise SystemExit(main())
