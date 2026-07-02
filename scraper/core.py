from __future__ import annotations

import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml

from .export import export_master_dataset
from .progress import ProgressLogger
from .serper import (
    NormalizedBusiness,
    SearchResult,
    SerperClient,
    SerperRequestError,
    normalize_place,
)
from .store import Store


CSV_COLUMN_NAME = "Locations"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    env_file: Path
    locations_csv: Path
    keywords_yaml: Path
    db_path: Path
    outputs_dir: Path
    serper_base_url: str
    serper_base_url_source: str
    serper_api_key: str | None
    serper_api_key_source: str


@dataclass(frozen=True)
class Keyword:
    id: str
    category: str
    query: str
    enabled: bool
    priority: int


@dataclass(frozen=True)
class LocationRecord:
    zip_code: str
    city: str
    state: str
    country: str


@dataclass(frozen=True)
class Seed:
    seed_type: str
    seed_value: str
    zip_code: str
    state: str
    city: str
    country: str


@dataclass(frozen=True)
class StateSeedPlan:
    zip_seeds: list[Seed]


@dataclass(frozen=True)
class ParsedSeedInput:
    keyword_hint: str | None
    location: LocationRecord
    raw_seed: str


@dataclass
class RequestExecution:
    performed_request: bool
    api_request_count: int
    estimated_credit_usage: int
    new_unique_businesses: int
    result_count: int
    distinct_business_ids: set[str]
    status: str
    seed_descriptor: str | None


@dataclass(frozen=True)
class IndexedExecution:
    index: int
    execution: RequestExecution


def default_app_config(project_root: Path | None = None) -> AppConfig:
    resolved_root = Path(
        project_root
        or os.environ.get("SCRAPER_PROJECT_ROOT")
        or Path(__file__).resolve().parent.parent
    ).resolve()
    env_file = resolved_root / ".env"
    file_env = load_dotenv_file(env_file)
    serper_base_url, serper_base_url_source = resolve_env_setting(
        "SERPER_BASE_URL",
        file_env,
        default="https://google.serper.dev",
    )
    serper_api_key, serper_api_key_source = resolve_env_setting("SERPER_API_KEY", file_env)
    return AppConfig(
        project_root=resolved_root,
        env_file=env_file,
        locations_csv=resolved_root / "us_input_locations_for_maps.csv",
        keywords_yaml=resolved_root / "config" / "keywords.yaml",
        db_path=resolved_root / "data" / "scrape.db",
        outputs_dir=resolved_root / "outputs",
        serper_base_url=serper_base_url or "https://google.serper.dev",
        serper_base_url_source=serper_base_url_source,
        serper_api_key=serper_api_key,
        serper_api_key_source=serper_api_key_source,
    )


def validate_config(app_config: AppConfig) -> dict[str, Any]:
    ensure_runtime_dirs(app_config)
    locations = load_locations(app_config.locations_csv)
    keywords = load_keywords(app_config.keywords_yaml)
    states = sorted({location.state for location in locations})
    enabled_keyword_ids = [keyword.id for keyword in keywords if keyword.enabled]
    return {
        "ok": True,
        "env_file": str(app_config.env_file),
        "env_file_present": app_config.env_file.exists(),
        "locations_csv": str(app_config.locations_csv),
        "location_count": len(locations),
        "state_count": len(states),
        "states": states,
        "keywords_yaml": str(app_config.keywords_yaml),
        "keyword_count": len(keywords),
        "enabled_keyword_count": len(enabled_keyword_ids),
        "enabled_keyword_ids": enabled_keyword_ids,
        "db_path": str(app_config.db_path),
        "outputs_dir": str(app_config.outputs_dir),
        "serper_base_url": app_config.serper_base_url,
        "serper_base_url_source": app_config.serper_base_url_source,
        "serper_api_key_present": bool(app_config.serper_api_key),
        "serper_api_key_source": app_config.serper_api_key_source,
    }






def run_seed(
    app_config: AppConfig,
    *,
    mode: str,
    keyword_id: str,
    seed: str,
    max_serper_queries: int | None = None,
    max_serper_queries_per_seed: int | None = None,
    workers: int = 1,
) -> dict[str, Any]:
    ensure_runtime_dirs(app_config)
    if mode != "raw_zip_max":
        raise ConfigError("Only raw_zip_max mode is supported.")
    parsed_seed = parse_manual_seed(seed)
    all_keywords = load_keywords(app_config.keywords_yaml)
    keyword = get_keyword_by_id(all_keywords, keyword_id)
    if not keyword.enabled:
        raise ConfigError(f"Keyword id {keyword_id} is disabled in config/keywords.yaml.")
    validate_legacy_seed_keyword_hint(keyword, parsed_seed)

    locations = load_locations(app_config.locations_csv)
    if max_serper_queries is not None and max_serper_queries <= 0:
        raise ConfigError("max_serper_queries must be a positive integer.")
    if max_serper_queries_per_seed is not None and max_serper_queries_per_seed <= 0:
        raise ConfigError("max_serper_queries_per_seed must be a positive integer.")
    if workers <= 0:
        raise ConfigError("workers must be a positive integer.")
    if max_serper_queries_per_seed is not None and mode != "raw_zip_max":
        raise ConfigError("max_serper_queries_per_seed is only supported for seed runs in raw_zip_max mode.")
    if workers > 1 and mode != "raw_zip_max":
        raise ConfigError("workers > 1 is only supported for seed runs in raw_zip_max mode.")
    if workers > 1 and max_serper_queries is not None:
        raise ConfigError("workers > 1 is not supported together with max_serper_queries.")

    if not seed_exists_in_locations(locations, parsed_seed.location):
        raise ConfigError(
            "Seed location was not found in us_input_locations_for_maps.csv: "
            f"{parsed_seed.location.zip_code}, {parsed_seed.location.city}, "
            f"{parsed_seed.location.state}, {parsed_seed.location.country}"
        )

    state_locations = [location for location in locations if location.state == parsed_seed.location.state]
    if not state_locations:
        raise ConfigError(
            f"Seed state {parsed_seed.location.state} was not found in {app_config.locations_csv.name}."
        )

    store = Store(app_config.db_path)
    run_id = generate_run_id(mode)
    store.start_run(
        run_id=run_id,
        mode=mode,
        run_kind="seed",
        targeted_states=[parsed_seed.location.state],
        targeted_keyword_ids=[keyword.id],
        command_payload={
            "command": "seed",
            "mode": mode,
            "keyword_id": keyword.id,
            "seed": parsed_seed.raw_seed,
            "seed_keyword_hint": parsed_seed.keyword_hint,
            "max_serper_queries": max_serper_queries,
            "max_serper_queries_per_seed": max_serper_queries_per_seed,
            "workers": workers,
        },
    )
    progress = ProgressLogger(app_config.outputs_dir / f"live_run_{run_id}.jsonl")
    progress.event(
        "run_started",
        run_id=run_id,
        command="seed",
        mode=mode,
        state_count=1,
        keyword_count=1,
        live_log_path=str(progress.path),
    )
    state_seed_count = len({(location.zip_code, location.city) for location in state_locations})
    max_possible_serper_queries_at_seed_cap = None
    if max_serper_queries_per_seed is not None:
        max_possible_serper_queries_at_seed_cap = state_seed_count * max_serper_queries_per_seed
    effective_max_serper_queries = max_serper_queries
    if max_possible_serper_queries_at_seed_cap is not None:
        effective_max_serper_queries = (
            min(effective_max_serper_queries, max_possible_serper_queries_at_seed_cap)
            if effective_max_serper_queries is not None
            else max_possible_serper_queries_at_seed_cap
        )
    payload: dict[str, Any] = {
        "command": "seed",
        "mode": mode,
        "run_id": run_id,
        "keyword_id": keyword.id,
        "keyword_query": keyword.query,
        "seed": parsed_seed.raw_seed,
        "seed_keyword_hint": parsed_seed.keyword_hint,
        "seed_location": {
            "zip_code": parsed_seed.location.zip_code,
            "city": parsed_seed.location.city,
            "state": parsed_seed.location.state,
            "country": parsed_seed.location.country,
        },
        "targeted_state": parsed_seed.location.state,
        "state_location_count": len(state_locations),
        "state_seed_count": state_seed_count,
        "requested_max_serper_queries": max_serper_queries,
        "requested_max_serper_queries_per_seed": max_serper_queries_per_seed,
        "max_possible_serper_queries_at_seed_cap": max_possible_serper_queries_at_seed_cap,
        "effective_max_serper_queries": effective_max_serper_queries,
        "workers_requested": workers,
    }

    try:
        client = SerperClient(
            api_key=require_api_key(app_config),
            base_url=app_config.serper_base_url,
        )
        result = _run_state_keyword_mode(
            store=store,
            client=client,
            locations=state_locations,
            keyword=keyword,
            state=parsed_seed.location.state,
            mode=mode,
            run_id=run_id,
            max_serper_queries=max_serper_queries,
            max_serper_queries_per_seed=max_serper_queries_per_seed,
            workers=workers,
            store_factory=lambda: Store(app_config.db_path),
            progress=progress,
        )
        payload.update(result)
    finally:
        request_log_rows = store.fetch_run_request_rows(run_id)
        payload["request_log_path"] = write_request_log(
            app_config.outputs_dir / f"request_log_{run_id}.csv",
            request_log_rows,
        )
        payload["live_log_path"] = str(progress.path)
        payload["summary_path"] = write_json_summary(
            app_config.outputs_dir / f"run_summary_{run_id}.json",
            payload,
        )
        progress.event(
            "run_finished",
            run_id=run_id,
            total_requests=payload.get("total_requests", 0),
            estimated_credits=payload.get("estimated_credits", 0),
            new_unique_businesses_added=payload.get("new_unique_businesses_added", 0),
            summary_path=payload["summary_path"],
        )
        progress.close()
        store.finish_run(run_id, payload)
        store.close()

    return payload


def run_export(app_config: AppConfig, *, chunk_size: int) -> dict[str, Any]:
    ensure_runtime_dirs(app_config)
    if chunk_size <= 0:
        raise ConfigError("Chunk size must be a positive integer.")
    store = Store(app_config.db_path)
    try:
        result = export_master_dataset(
            store=store,
            outputs_dir=app_config.outputs_dir,
            chunk_size=chunk_size,
        )
    finally:
        store.close()
    return result


def load_keywords(path: Path) -> list[Keyword]:
    if not path.exists():
        raise ConfigError(f"Keyword config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}

    raw_keywords = payload.get("keywords")
    if not isinstance(raw_keywords, list):
        raise ConfigError("config/keywords.yaml must contain a top-level 'keywords' list.")

    keywords: list[Keyword] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_keywords, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"Keyword entry #{index} must be a mapping.")
        keyword_id = _required_string(item, "id", context=f"keyword #{index}")
        if keyword_id in seen_ids:
            raise ConfigError(f"Duplicate keyword id found: {keyword_id}")
        seen_ids.add(keyword_id)
        category = _required_string(item, "category", context=f"keyword {keyword_id}")
        query = _required_string(item, "query", context=f"keyword {keyword_id}")
        enabled = item.get("enabled")
        priority = item.get("priority")
        if not isinstance(enabled, bool):
            raise ConfigError(f"Keyword {keyword_id} must define boolean 'enabled'.")
        if not isinstance(priority, int):
            raise ConfigError(f"Keyword {keyword_id} must define integer 'priority'.")
        keywords.append(
            Keyword(
                id=keyword_id,
                category=category,
                query=query,
                enabled=enabled,
                priority=priority,
            )
        )
    keywords.sort(key=lambda keyword: (keyword.priority, keyword.id))
    return keywords


def get_keyword_by_id(keywords: list[Keyword], keyword_id: str) -> Keyword:
    for keyword in keywords:
        if keyword.id == keyword_id:
            return keyword
    raise ConfigError(f"Keyword id {keyword_id} was not found in config/keywords.yaml.")


def load_locations(path: Path) -> list[LocationRecord]:
    if not path.exists():
        raise ConfigError(f"Locations CSV not found: {path}")

    locations: list[LocationRecord] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if CSV_COLUMN_NAME not in (reader.fieldnames or []):
            raise ConfigError(f"{path.name} must contain a '{CSV_COLUMN_NAME}' column.")
        for line_number, row in enumerate(reader, start=2):
            raw_value = (row.get(CSV_COLUMN_NAME) or "").strip()
            if not raw_value:
                continue
            parts = [part.strip() for part in raw_value.split(",")]
            if len(parts) != 4:
                raise ConfigError(
                    f"Malformed location at line {line_number}: expected 4 comma-separated values."
                )
            zip_code, city, state, country = parts
            if not all(parts):
                raise ConfigError(f"Malformed location at line {line_number}: blank fields are not allowed.")
            locations.append(
                LocationRecord(
                    zip_code=zip_code,
                    city=city,
                    state=state.upper(),
                    country=country.upper(),
                )
            )
    if not locations:
        raise ConfigError(f"No locations were parsed from {path.name}.")
    return locations


def build_state_seed_plan(locations: list[LocationRecord], state: str) -> StateSeedPlan:
    # One seed per unique (zip, city) location: a single ZIP can hold multiple
    # cities, and each is queried separately. seed_value carries "zip, city" so
    # fingerprints and provenance stay unique per location.
    locations_by_key: dict[tuple[str, str], LocationRecord] = {}
    for location in locations:
        if location.state != state:
            continue
        locations_by_key.setdefault((location.zip_code, location.city), location)

    zip_seeds = [
        Seed(
            seed_type="raw_zip",
            seed_value=f"{location.zip_code}, {location.city}",
            zip_code=location.zip_code,
            state=location.state,
            city=location.city,
            country=location.country,
        )
        for location in locations_by_key.values()
    ]
    zip_seeds.sort(key=lambda seed: (seed.zip_code, seed.city))

    return StateSeedPlan(zip_seeds=zip_seeds)


def parse_manual_seed(raw_seed: str) -> ParsedSeedInput:
    parts = [part.strip() for part in raw_seed.split(",")]
    if len(parts) == 4:
        keyword_hint = None
        location_parts = parts
    elif len(parts) == 5:
        keyword_hint = parts[0] or None
        location_parts = parts[1:]
    else:
        raise ConfigError(
            "Seed must be either 'zip, city, state, country' or "
            "'keyword, zip, city, state, country'."
        )

    if not all(location_parts):
        raise ConfigError("Seed contains blank values; expected non-empty zip, city, state, and country.")

    zip_code, city, state, country = location_parts
    return ParsedSeedInput(
        keyword_hint=keyword_hint,
        location=LocationRecord(
            zip_code=zip_code,
            city=city,
            state=state.upper(),
            country=country.upper(),
        ),
        raw_seed=raw_seed.strip(),
    )


def _legacy_keyword_aliases(value: str) -> set[str]:
    normalized = "".join(char for char in value.casefold() if char.isalnum())
    aliases = {normalized}
    if normalized.endswith("s") and normalized[:-1]:
        aliases.add(normalized[:-1])
    return aliases


def validate_legacy_seed_keyword_hint(keyword: Keyword, parsed_seed: ParsedSeedInput) -> None:
    if parsed_seed.keyword_hint is None:
        return

    expected_aliases = set().union(
        _legacy_keyword_aliases(keyword.id),
        _legacy_keyword_aliases(keyword.query),
        _legacy_keyword_aliases(keyword.category),
    )
    if _legacy_keyword_aliases(parsed_seed.keyword_hint).isdisjoint(expected_aliases):
        raise ConfigError(
            f"Legacy seed keyword prefix {parsed_seed.keyword_hint!r} "
            f"does not match selected keyword id {keyword.id!r} "
            f"or query {keyword.query!r}."
        )


def seed_exists_in_locations(locations: list[LocationRecord], target: LocationRecord) -> bool:
    return any(
        location.zip_code == target.zip_code
        and location.city.casefold() == target.city.casefold()
        and location.state == target.state
        and location.country == target.country
        for location in locations
    )


def load_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            raise ConfigError(f"Malformed .env line {line_number}: expected KEY=VALUE format.")
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            raise ConfigError(f"Malformed .env line {line_number}: missing key before '='.")
        parsed[key] = value.strip().strip("'").strip('"')
    return parsed


def resolve_env_setting(
    key: str,
    file_env: dict[str, str],
    *,
    default: str | None = None,
) -> tuple[str | None, str]:
    shell_value = os.environ.get(key)
    if shell_value is not None and shell_value.strip():
        return shell_value.strip(), "environment"

    file_value = file_env.get(key)
    if file_value is not None and file_value.strip():
        return file_value.strip(), ".env"

    if default is not None:
        return default, "default"
    return None, "none"


def ensure_runtime_dirs(app_config: AppConfig) -> None:
    app_config.db_path.parent.mkdir(parents=True, exist_ok=True)
    app_config.outputs_dir.mkdir(parents=True, exist_ok=True)
    app_config.keywords_yaml.parent.mkdir(parents=True, exist_ok=True)


def require_api_key(app_config: AppConfig) -> str:
    if not app_config.serper_api_key:
        raise ConfigError("SERPER_API_KEY is required for seed runs.")
    return app_config.serper_api_key


def generate_run_id(mode: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{mode}_{uuid4().hex[:8]}"


def write_request_log(path: Path, rows: list[dict[str, Any]]) -> str:
    fieldnames = [
        "run_id",
        "mode",
        "keyword_id",
        "state",
        "seed_type",
        "seed_value",
        "status",
        "latency_ms",
        "result_count",
        "new_unique_businesses",
        "api_request_count",
        "retry_count",
        "estimated_credit_usage",
        "pagination_stop_reason",
        "query_text",
        "http_status",
        "error_message",
        "fingerprint",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return str(path)


def write_json_summary(path: Path, payload: dict[str, Any]) -> str:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return str(path)




def _run_state_keyword_mode(
    *,
    store: Store,
    client: SerperClient,
    locations: list[LocationRecord],
    keyword: Keyword,
    state: str,
    mode: str,
    run_id: str,
    max_serper_queries: int | None = None,
    max_serper_queries_per_seed: int | None = None,
    workers: int = 1,
    store_factory: Callable[[], Store] | None = None,
    progress: ProgressLogger | None = None,
) -> dict[str, Any]:
    if mode != "raw_zip_max":
        raise ConfigError(f"Unsupported mode: {mode}")

    seed_plan = build_state_seed_plan(locations, state)
    run_seen_business_ids: set[str] = set()
    seeds_ran: list[str] = []
    total_requests = 0
    failed_requests = 0
    skipped_completed_requests = 0
    skipped_in_flight_requests = 0
    estimated_credits = 0
    total_results = 0
    new_unique_businesses_added = 0
    remaining_serper_queries = max_serper_queries

    if workers > 1:
        indexed_executions = _run_raw_zip_seeds_concurrently(
            store_factory=store_factory or (lambda: Store(store.db_path)),
            client=client,
            keyword=keyword,
            state=state,
            mode=mode,
            seeds=seed_plan.zip_seeds,
            run_id=run_id,
            workers=workers,
            max_serper_queries_per_seed=max_serper_queries_per_seed,
            progress=progress,
        )
        executions = [indexed.execution for indexed in indexed_executions]
    else:
        executions = []
        for seed in seed_plan.zip_seeds:
            if remaining_serper_queries is not None and remaining_serper_queries <= 0:
                break
            execution = _execute_seed_request(
                store=store,
                client=client,
                keyword=keyword,
                state=state,
                mode=mode,
                seed=seed,
                run_id=run_id,
                max_serper_queries=_limit_seed_query_budget(
                    remaining_serper_queries=remaining_serper_queries,
                    max_serper_queries_per_seed=max_serper_queries_per_seed,
                    seed=seed,
                ),
                progress=progress,
            )
            executions.append(execution)
            if execution.performed_request and remaining_serper_queries is not None:
                remaining_serper_queries -= execution.api_request_count

    for execution in executions:
        if execution.performed_request:
            total_requests += execution.api_request_count
            total_results += execution.result_count
            new_unique_businesses_added += execution.new_unique_businesses
            run_seen_business_ids.update(execution.distinct_business_ids)
            if execution.seed_descriptor is not None:
                seeds_ran.append(execution.seed_descriptor)
            estimated_credits += execution.estimated_credit_usage
            if execution.status != "success":
                failed_requests += 1
        elif execution.status == "in_progress":
            skipped_in_flight_requests += 1
        else:
            skipped_completed_requests += 1

    unique_businesses_found = len(run_seen_business_ids)
    return {
        "run_id": run_id,
        "mode": mode,
        "state": state,
        "keyword_id": keyword.id,
        "keyword_category": keyword.category,
        "total_requests": total_requests,
        "failed_requests": failed_requests,
        "skipped_completed_requests": skipped_completed_requests,
        "skipped_in_flight_requests": skipped_in_flight_requests,
        "estimated_credits": estimated_credits,
        "total_results": total_results,
        "unique_businesses_found": unique_businesses_found,
        "new_unique_businesses_added": new_unique_businesses_added,
        "duplicates_collapsed": max(total_results - unique_businesses_found, 0),
        "states_ran": [state] if seeds_ran else [],
        "keywords_ran": [keyword.id] if seeds_ran else [],
        "seeds_ran": seeds_ran,
        "serper_queries_remaining": remaining_serper_queries,
    }


def _run_raw_zip_seeds_concurrently(
    *,
    store_factory: Callable[[], Store],
    client: SerperClient,
    keyword: Keyword,
    state: str,
    mode: str,
    seeds: list[Seed],
    run_id: str,
    workers: int,
    max_serper_queries_per_seed: int | None,
    progress: ProgressLogger | None,
) -> list[IndexedExecution]:
    def run_seed_at_index(index: int, seed: Seed) -> IndexedExecution:
        worker_store = store_factory()
        try:
            execution = _execute_seed_request(
                store=worker_store,
                client=client,
                keyword=keyword,
                state=state,
                mode=mode,
                seed=seed,
                run_id=run_id,
                max_serper_queries=max_serper_queries_per_seed,
                progress=progress,
            )
            return IndexedExecution(index=index, execution=execution)
        finally:
            worker_store.close()

    indexed_results: list[IndexedExecution] = []
    max_workers = min(workers, max(len(seeds), 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(run_seed_at_index, index, seed)
            for index, seed in enumerate(seeds)
        ]
        for future in as_completed(futures):
            indexed_results.append(future.result())

    indexed_results.sort(key=lambda item: item.index)
    return indexed_results


def _limit_seed_query_budget(
    *,
    remaining_serper_queries: int | None,
    max_serper_queries_per_seed: int | None,
    seed: Seed,
) -> int | None:
    if seed.seed_type != "raw_zip" or max_serper_queries_per_seed is None:
        return remaining_serper_queries
    if remaining_serper_queries is None:
        return max_serper_queries_per_seed
    return min(remaining_serper_queries, max_serper_queries_per_seed)


def _execute_seed_request(
    *,
    store: Store,
    client: SerperClient,
    keyword: Keyword,
    state: str,
    mode: str,
    seed: Seed,
    run_id: str,
    max_serper_queries: int | None = None,
    progress: ProgressLogger | None = None,
) -> RequestExecution:
    fingerprint = build_request_fingerprint(
        mode=mode,
        keyword_id=keyword.id,
        state=state,
        seed_type=seed.seed_type,
        seed_value=seed.seed_value,
    )
    query_text = build_query_text(keyword.query, seed)
    seed_descriptor = f"{seed.seed_type}:{seed.seed_value}"
    claim_acquired, existing_request = store.claim_request(
        fingerprint=fingerprint,
        run_id=run_id,
        mode=mode,
        keyword_id=keyword.id,
        state=state,
        seed_type=seed.seed_type,
        seed_value=seed.seed_value,
        query_text=query_text,
    )
    if not claim_acquired:
        request_status = existing_request["status"] if existing_request is not None else "error"
        if existing_request and existing_request["status"] == "success":
            execution = RequestExecution(
                performed_request=False,
                api_request_count=0,
                estimated_credit_usage=0,
                new_unique_businesses=existing_request["new_unique_businesses"],
                result_count=existing_request["result_count"],
                distinct_business_ids=set(),
                status="success",
                seed_descriptor=None,
            )
            _emit_request_progress(
                progress,
                run_id=run_id,
                mode=mode,
                keyword=keyword,
                state=state,
                seed=seed,
                execution=execution,
            )
            return execution
        execution = RequestExecution(
            performed_request=False,
            api_request_count=0,
            estimated_credit_usage=0,
            new_unique_businesses=0,
            result_count=0,
            distinct_business_ids=set(),
            status=request_status,
            seed_descriptor=None,
        )
        _emit_request_progress(
            progress,
            run_id=run_id,
            mode=mode,
            keyword=keyword,
            state=state,
            seed=seed,
            execution=execution,
        )
        return execution

    result: SearchResult | None = None
    try:
        result = client.search_places(query=query_text, max_page_requests=max_serper_queries)
        raw_result_count = len(result.places)
        normalized_businesses = _normalize_businesses(result)
        distinct_ids = {business.stable_business_id for business in normalized_businesses}
        inserted_new_businesses = 0
        with store.transaction():
            for business in normalized_businesses:
                if store.upsert_business(
                    business=business,
                    run_id=run_id,
                    keyword_id=keyword.id,
                    category=keyword.category,
                    state=state,
                    seed_type=seed.seed_type,
                    seed_value=seed.seed_value,
                    request_fingerprint=fingerprint,
                ):
                    inserted_new_businesses += 1
            store.record_request(
                {
                    "fingerprint": fingerprint,
                    "run_id": run_id,
                    "mode": mode,
                    "keyword_id": keyword.id,
                    "state": state,
                    "seed_type": seed.seed_type,
                    "seed_value": seed.seed_value,
                    "query_text": query_text,
                    "status": "success",
                    "http_status": 200,
                    "latency_ms": result.latency_ms,
                    "result_count": raw_result_count,
                    "new_unique_businesses": inserted_new_businesses,
                    "api_request_count": result.api_request_count,
                    "retry_count": result.retry_count,
                    "estimated_credit_usage": result.estimated_credit_usage,
                    "pagination_stop_reason": result.pagination_stop_reason,
                    "error_message": None,
                }
            )
        execution = RequestExecution(
            performed_request=True,
            api_request_count=result.api_request_count,
            estimated_credit_usage=result.estimated_credit_usage,
            new_unique_businesses=inserted_new_businesses,
            result_count=raw_result_count,
            distinct_business_ids=distinct_ids,
            status="success",
            seed_descriptor=seed_descriptor,
        )
        _emit_request_progress(
            progress,
            run_id=run_id,
            mode=mode,
            keyword=keyword,
            state=state,
            seed=seed,
            execution=execution,
        )
        return execution
    except SerperRequestError as exc:
        with store.transaction():
            store.record_request(
                {
                    "fingerprint": fingerprint,
                    "run_id": run_id,
                    "mode": mode,
                    "keyword_id": keyword.id,
                    "state": state,
                    "seed_type": seed.seed_type,
                    "seed_value": seed.seed_value,
                    "query_text": query_text,
                    "status": "error",
                    "http_status": exc.status_code,
                    "latency_ms": exc.latency_ms,
                    "result_count": 0,
                    "new_unique_businesses": 0,
                    "api_request_count": exc.api_request_count,
                    "retry_count": exc.retry_count,
                    "estimated_credit_usage": exc.estimated_credit_usage,
                    "pagination_stop_reason": None,
                    "error_message": str(exc),
                }
            )
        execution = RequestExecution(
            performed_request=True,
            api_request_count=exc.api_request_count,
            estimated_credit_usage=exc.estimated_credit_usage,
            new_unique_businesses=0,
            result_count=0,
            distinct_business_ids=set(),
            status="error",
            seed_descriptor=seed_descriptor,
        )
        _emit_request_progress(
            progress,
            run_id=run_id,
            mode=mode,
            keyword=keyword,
            state=state,
            seed=seed,
            execution=execution,
            error_message=str(exc),
        )
        return execution
    except Exception as exc:
        api_request_count = result.api_request_count if result is not None else 0
        estimated_credit_usage = result.estimated_credit_usage if result is not None else 0
        latency_ms = result.latency_ms if result is not None else 0
        with store.transaction():
            store.record_request(
                {
                    "fingerprint": fingerprint,
                    "run_id": run_id,
                    "mode": mode,
                    "keyword_id": keyword.id,
                    "state": state,
                    "seed_type": seed.seed_type,
                    "seed_value": seed.seed_value,
                    "query_text": query_text,
                    "status": "error",
                    "http_status": None,
                    "latency_ms": latency_ms,
                    "result_count": 0,
                    "new_unique_businesses": 0,
                    "api_request_count": api_request_count,
                    "retry_count": 0,
                    "estimated_credit_usage": estimated_credit_usage,
                    "pagination_stop_reason": None,
                    "error_message": str(exc),
                }
        )
        raise


def _emit_request_progress(
    progress: ProgressLogger | None,
    *,
    run_id: str,
    mode: str,
    keyword: Keyword,
    state: str,
    seed: Seed,
    execution: RequestExecution,
    error_message: str | None = None,
) -> None:
    if progress is None:
        return
    progress.event(
        "request_done",
        run_id=run_id,
        mode=mode,
        keyword_id=keyword.id,
        keyword_query=keyword.query,
        state=state,
        seed_type=seed.seed_type,
        seed_value=seed.seed_value,
        status=execution.status,
        performed_request=execution.performed_request,
        api_request_count=execution.api_request_count,
        estimated_credit_usage=execution.estimated_credit_usage,
        result_count=execution.result_count,
        new_unique_businesses=execution.new_unique_businesses,
        error_message=error_message,
    )


def build_request_fingerprint(
    *,
    mode: str,
    keyword_id: str,
    state: str,
    seed_type: str,
    seed_value: str,
) -> str:
    raw_value = "|".join([mode, keyword_id, state, seed_type, seed_value])
    return sha1(raw_value.encode("utf-8")).hexdigest()


def build_query_text(keyword_query: str, seed: Seed) -> str:
    if seed.seed_type == "raw_zip":
        return f"{keyword_query}, {seed.zip_code}, {seed.city}, {seed.state}, {seed.country}"
    raise ConfigError(f"Unsupported seed type: {seed.seed_type}")


def _normalize_businesses(result: SearchResult) -> list[NormalizedBusiness]:
    normalized_businesses: list[NormalizedBusiness] = []
    seen_ids: set[str] = set()
    for place in result.places:
        if not isinstance(place, dict):
            continue
        normalized = normalize_place(place)
        if normalized is None:
            continue
        if normalized.stable_business_id in seen_ids:
            continue
        seen_ids.add(normalized.stable_business_id)
        normalized_businesses.append(normalized)
    return normalized_businesses


def _required_string(payload: dict[str, Any], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context} must define non-empty string '{key}'.")
    return value.strip()
