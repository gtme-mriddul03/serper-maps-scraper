from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from .store import Store


EXPORT_FIELDNAMES = [
    "stable_business_id",
    "cid",
    "title",
    "address",
    "phone",
    "website",
    "source_category",
    "latitude",
    "longitude",
    "rating",
    "rating_count",
    "matched_keywords",
    "matched_categories",
    "matched_states",
    "run_ids",
    "first_seed",
    "first_seed_type",
    "first_seed_value",
    "first_state",
    "seed_count",
]


def export_master_dataset(
    *,
    store: Store,
    outputs_dir: Path,
    chunk_size: int,
) -> dict[str, Any]:
    outputs_dir.mkdir(parents=True, exist_ok=True)
    businesses = store.fetch_all_businesses()
    provenance_rows = store.fetch_all_business_provenance()

    provenance_by_business: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in provenance_rows:
        provenance_by_business[row["stable_business_id"]].append(row)

    export_rows = [
        flatten_business_row(business, provenance_by_business[business["stable_business_id"]])
        for business in businesses
    ]
    export_rows.sort(
        key=lambda row: (
            row["stable_business_id"],
            row["matched_states"],
            row["title"].casefold(),
        )
    )

    master_path = outputs_dir / "master.csv"
    _write_csv(master_path, export_rows)

    part_paths: list[Path] = []
    if export_rows:
        for index, chunk in enumerate(_chunk_rows(export_rows, chunk_size), start=1):
            part_path = outputs_dir / f"master.part_{index:04d}.csv"
            _write_csv(part_path, chunk)
            part_paths.append(part_path)

    return {
        "master_csv": str(master_path),
        "chunk_csvs": [str(path) for path in part_paths],
        "row_count": len(export_rows),
        "chunk_count": len(part_paths),
        "chunk_size": chunk_size,
    }


def flatten_business_row(
    business: dict[str, Any],
    provenance_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    matched_keywords = sorted({row["keyword_id"] for row in provenance_rows})
    matched_categories = sorted({row["category"] for row in provenance_rows})
    matched_states = sorted({row["state"] for row in provenance_rows})
    run_ids = sorted({row["run_id"] for row in provenance_rows})
    seed_keys = {
        (row["seed_type"], row["seed_value"], row["state"])
        for row in provenance_rows
    }

    return {
        "stable_business_id": business["stable_business_id"],
        "cid": business["cid"] or "",
        "title": business["title"],
        "address": business["address"],
        "phone": business["phone"] or "",
        "website": business["website"] or "",
        "source_category": business["source_category"] or "",
        "latitude": "" if business["latitude"] is None else business["latitude"],
        "longitude": "" if business["longitude"] is None else business["longitude"],
        "rating": "" if business["rating"] is None else business["rating"],
        "rating_count": "" if business["rating_count"] is None else business["rating_count"],
        "matched_keywords": "|".join(matched_keywords),
        "matched_categories": "|".join(matched_categories),
        "matched_states": "|".join(matched_states),
        "run_ids": "|".join(run_ids),
        "first_seed": f"{business['first_seed_type']}:{business['first_seed_value']}",
        "first_seed_type": business["first_seed_type"],
        "first_seed_value": business["first_seed_value"],
        "first_state": business["first_state"],
        "seed_count": len(seed_keys),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _chunk_rows(rows: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + chunk_size] for index in range(0, len(rows), chunk_size)]
