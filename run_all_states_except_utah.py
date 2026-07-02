#!/usr/bin/env python3
"""Run a Serper scrape for ONE keyword across all states except Utah.

The scraper runs one `seed` command per state (each seed covers every ZIP in
that state). This picks the first ZIP row per state from the location CSV and
drives one `seed` invocation per state, skipping UT.

Usage:
    python3 run_all_states_except_utah.py <keyword_id> <max_per_seed> [--dry-run]

Example:
    python3 run_all_states_except_utah.py architect 1 --dry-run   # print only
    python3 run_all_states_except_utah.py architect 30            # full paid run
"""
import csv
import subprocess
import sys

EXCLUDE = {"UT"}
CSV_PATH = "us_input_locations_for_maps.csv"

# keyword_id -> query text (from config/keywords.yaml)
QUERIES = {
    "design_build_firms": "Design Build Firms",
    "architect": "Architect",
    "civil_engineering": "Civil Engineering",
    "structural_engineering": "Structural Engineering",
    "urban_planners": "Urban Planners",
}


def first_seed_per_state():
    """Return {state: (zip, city, state, country)} using first row seen per state."""
    seeds = {}
    with open(CSV_PATH, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            zip_code, city, state, country = [p.strip() for p in row["Locations"].split(",")]
            if state in EXCLUDE or state in seeds:
                continue
            seeds[state] = (zip_code, city, state, country)
    return seeds


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    keyword_id = sys.argv[1]
    max_per_seed = sys.argv[2]
    dry_run = "--dry-run" in sys.argv[3:]

    if keyword_id not in QUERIES:
        sys.exit(f"unknown keyword_id {keyword_id!r}; known: {list(QUERIES)}")
    query = QUERIES[keyword_id]

    seeds = first_seed_per_state()
    print(f"# {len(seeds)} states (excluding {sorted(EXCLUDE)}), keyword={keyword_id}, max_per_seed={max_per_seed}")
    for state in sorted(seeds):
        zip_code, city, st, country = seeds[state]
        seed = f"{query}, {zip_code}, {city}, {st}, {country}"
        cmd = [
            "python3", "-m", "scraper", "seed",
            "--mode", "raw_zip_max",
            "--keyword-id", keyword_id,
            "--seed", seed,
            "--max-serper-queries-per-seed", str(max_per_seed),
            "--workers", "25",
        ]
        if dry_run:
            print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        else:
            print(f"\n=== {state} ===")
            subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
