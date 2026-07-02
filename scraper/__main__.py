from __future__ import annotations

import argparse
import json
import sys

from .core import (
    ConfigError,
    default_app_config,
    run_export,
    run_seed,
    validate_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal Serper Places scraper CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("validate-config", help="Validate keywords, CSV parsing, and runtime paths.")

    seed_parser = subparsers.add_parser(
        "seed",
        help="Run one state from a legacy seed string using the matching CSV state rows.",
    )
    seed_parser.add_argument(
        "--mode",
        required=True,
        choices=["raw_zip_max"],
        help="Collection mode to run for the seed-derived state.",
    )
    seed_parser.add_argument("--keyword-id", required=True, help="Keyword id from config/keywords.yaml.")
    seed_parser.add_argument(
        "--seed",
        required=True,
        help="Legacy seed string. Accepts either 'zip, city, state, country' or "
        "'keyword, zip, city, state, country'.",
    )
    seed_parser.add_argument(
        "--max-serper-queries",
        type=int,
        help="Optional hard cap on underlying Serper page requests for the run.",
    )
    seed_parser.add_argument(
        "--max-serper-queries-per-seed",
        type=int,
        help="Optional hard cap on underlying Serper page requests for each raw_zip seed.",
    )
    seed_parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of internal workers for raw_zip_max seed runs.",
    )

    export_parser = subparsers.add_parser("export", help="Export the deduped master dataset to CSV.")
    export_parser.add_argument(
        "--chunk-size",
        type=int,
        default=50000,
        help="Maximum number of rows per part file.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        app_config = default_app_config()
        if args.command == "validate-config":
            payload = validate_config(app_config)
        elif args.command == "seed":
            payload = run_seed(
                app_config,
                mode=args.mode,
                keyword_id=args.keyword_id,
                seed=args.seed,
                max_serper_queries=args.max_serper_queries,
                max_serper_queries_per_seed=args.max_serper_queries_per_seed,
                workers=args.workers,
            )
        elif args.command == "export":
            payload = run_export(app_config, chunk_size=args.chunk_size)
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
