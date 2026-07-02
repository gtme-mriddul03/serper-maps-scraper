from __future__ import annotations

import csv
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scraper.core import (
    AppConfig,
    ConfigError,
    Keyword,
    LocationRecord,
    ParsedSeedInput,
    SearchResult,
    Seed,
    _execute_seed_request,
    _run_state_keyword_mode,
    build_request_fingerprint,
    build_state_seed_plan,
    default_app_config,
    generate_run_id,
    load_keywords,
    load_locations,
    parse_manual_seed,
    seed_exists_in_locations,
    validate_legacy_seed_keyword_hint,
)
from scraper.export import flatten_business_row
from scraper.serper import SerperClient
from scraper.store import Store


class CoreBehaviorTests(unittest.TestCase):
    def test_parse_manual_seed_accepts_legacy_keyword_prefix(self) -> None:
        parsed = parse_manual_seed("architects, 15018, Buena Vista, PA, US")
        self.assertIsInstance(parsed, ParsedSeedInput)
        self.assertEqual(parsed.keyword_hint, "architects")
        self.assertEqual(parsed.location.zip_code, "15018")
        self.assertEqual(parsed.location.city, "Buena Vista")
        self.assertEqual(parsed.location.state, "PA")
        self.assertEqual(parsed.location.country, "US")

    def test_validate_legacy_seed_keyword_hint_allows_missing_prefix(self) -> None:
        keyword = Keyword(id="architect", category="architect", query="Architect", enabled=True, priority=10)
        parsed_without_prefix = ParsedSeedInput(
            keyword_hint=None,
            location=LocationRecord(zip_code="15018", city="Buena Vista", state="PA", country="US"),
            raw_seed="15018, Buena Vista, PA, US",
        )
        parsed_with_matching_prefix = ParsedSeedInput(
            keyword_hint="architects",
            location=LocationRecord(zip_code="15018", city="Buena Vista", state="PA", country="US"),
            raw_seed="architects, 15018, Buena Vista, PA, US",
        )

        validate_legacy_seed_keyword_hint(keyword, parsed_without_prefix)
        validate_legacy_seed_keyword_hint(keyword, parsed_with_matching_prefix)

    def test_validate_legacy_seed_keyword_hint_rejects_mismatch(self) -> None:
        keyword = Keyword(
            id="civil_engineering",
            category="civil_engineering",
            query="Civil Engineering",
            enabled=True,
            priority=30,
        )
        parsed = ParsedSeedInput(
            keyword_hint="architects",
            location=LocationRecord(zip_code="15018", city="Buena Vista", state="PA", country="US"),
            raw_seed="architects, 15018, Buena Vista, PA, US",
        )

        with self.assertRaises(ConfigError):
            validate_legacy_seed_keyword_hint(keyword, parsed)

    def test_validate_legacy_seed_keyword_hint_allows_query_alias_when_id_differs(self) -> None:
        keyword = Keyword(
            id="architect_search",
            category="design_services",
            query="Architect",
            enabled=True,
            priority=20,
        )
        parsed = ParsedSeedInput(
            keyword_hint="architects",
            location=LocationRecord(zip_code="15018", city="Buena Vista", state="PA", country="US"),
            raw_seed="architects, 15018, Buena Vista, PA, US",
        )

        validate_legacy_seed_keyword_hint(keyword, parsed)

    def test_seed_exists_in_locations_matches_exact_row(self) -> None:
        locations = [
            LocationRecord(zip_code="15018", city="Buena Vista", state="PA", country="US"),
            LocationRecord(zip_code="10001", city="New York", state="NY", country="US"),
        ]
        self.assertTrue(
            seed_exists_in_locations(
                locations,
                LocationRecord(zip_code="15018", city="Buena Vista", state="PA", country="US"),
            )
        )
        self.assertFalse(
            seed_exists_in_locations(
                locations,
                LocationRecord(zip_code="15019", city="Buena Vista", state="PA", country="US"),
            )
        )

    def test_load_locations_preserves_zip_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "locations.csv"
            csv_path.write_text(
                "Locations\n"
                "\"01234, Albany, NY, US\"\n"
                "\"1503, Berlin, MA, US\"\n",
                encoding="utf-8",
            )
            rows = load_locations(csv_path)
            self.assertEqual(rows[0].zip_code, "01234")
            self.assertEqual(rows[1].zip_code, "1503")

    def test_build_state_seed_plan_orders_zip_seeds_deterministically(self) -> None:
        locations = [
            LocationRecord(zip_code="94103", city="San Francisco", state="CA", country="US"),
            LocationRecord(zip_code="94107", city="San Francisco", state="CA", country="US"),
            LocationRecord(zip_code="94016", city="Daly City", state="CA", country="US"),
            LocationRecord(zip_code="94102", city="San Francisco", state="CA", country="US"),
        ]
        plan = build_state_seed_plan(locations, "CA")
        self.assertEqual(
            [seed.seed_value for seed in plan.zip_seeds],
            ["94016, Daly City", "94102, San Francisco", "94103, San Francisco", "94107, San Francisco"],
        )
        self.assertEqual([seed.zip_code for seed in plan.zip_seeds], ["94016", "94102", "94103", "94107"])
        self.assertEqual([seed.city for seed in plan.zip_seeds], ["Daly City", "San Francisco", "San Francisco", "San Francisco"])

    def test_build_state_seed_plan_splits_same_zip_by_city_and_dedups(self) -> None:
        locations = [
            LocationRecord(zip_code="10001", city="New York", state="NY", country="US"),
            LocationRecord(zip_code="10001", city="Hoboken", state="NY", country="US"),
            LocationRecord(zip_code="10001", city="New York", state="NY", country="US"),  # exact dup
        ]
        plan = build_state_seed_plan(locations, "NY")
        # same ZIP, two cities -> two seeds; exact (zip, city) dup collapsed
        self.assertEqual(
            [seed.seed_value for seed in plan.zip_seeds],
            ["10001, Hoboken", "10001, New York"],
        )
        # distinct seed_value -> distinct fingerprint (the collision this change fixes)
        fingerprints = {
            build_request_fingerprint(
                mode="raw_zip_max", keyword_id="k", state="NY",
                seed_type=seed.seed_type, seed_value=seed.seed_value,
            )
            for seed in plan.zip_seeds
        }
        self.assertEqual(len(fingerprints), 2)

    def test_load_keywords_validates_and_sorts_by_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            yaml_path = Path(temp_dir) / "keywords.yaml"
            yaml_path.write_text(
                "keywords:\n"
                "  - id: second\n"
                "    category: plumbers\n"
                "    query: plumber\n"
                "    enabled: true\n"
                "    priority: 20\n"
                "  - id: first\n"
                "    category: dentists\n"
                "    query: dentist\n"
                "    enabled: false\n"
                "    priority: 10\n",
                encoding="utf-8",
            )
            keywords = load_keywords(yaml_path)
            self.assertEqual([keyword.id for keyword in keywords], ["first", "second"])

    def test_request_fingerprint_is_stable(self) -> None:
        left = build_request_fingerprint(
            mode="raw_zip_max",
            keyword_id="dentists",
            state="CA",
            seed_type="raw_zip",
            seed_value="San Francisco, CA",
        )
        right = build_request_fingerprint(
            mode="raw_zip_max",
            keyword_id="dentists",
            state="CA",
            seed_type="raw_zip",
            seed_value="San Francisco, CA",
        )
        self.assertEqual(left, right)

    def test_flatten_business_row_aggregates_provenance(self) -> None:
        business = {
            "stable_business_id": "cid:123",
            "cid": "123",
            "title": "Acme Dental",
            "address": "1 Main St",
            "phone": "555-0100",
            "website": "https://example.com",
            "source_category": "Dentist",
            "latitude": 1.0,
            "longitude": 2.0,
            "rating": 4.5,
            "rating_count": 100,
            "first_seed_type": "raw_zip",
            "first_seed_value": "Albany, NY",
            "first_state": "NY",
        }
        provenance = [
            {
                "stable_business_id": "cid:123",
                "run_id": "run-a",
                "keyword_id": "dentists",
                "category": "healthcare",
                "state": "NY",
                "seed_type": "raw_zip",
                "seed_value": "Albany, NY",
            },
            {
                "stable_business_id": "cid:123",
                "run_id": "run-b",
                "keyword_id": "orthodontists",
                "category": "healthcare",
                "state": "NY",
                "seed_type": "raw_zip",
                "seed_value": "12207",
            },
        ]
        row = flatten_business_row(business, provenance)
        self.assertEqual(row["matched_keywords"], "dentists|orthodontists")
        self.assertEqual(row["matched_categories"], "healthcare")
        self.assertEqual(row["seed_count"], 2)

    def test_run_state_keyword_mode_counts_paginated_api_requests(self) -> None:
        class FakePaginatedResultClient(SerperClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", base_url="https://google.serper.dev")

            def search_places(
                self,
                *,
                query: str,
                gl: str = "us",
                hl: str = "en",
                autocorrect: bool = True,
                max_page_requests: int | None = None,
            ) -> SearchResult:
                self.last_query = query
                api_request_count = 3 if max_page_requests is None else min(3, max_page_requests)
                pagination_stop_reason = (
                    "short_page" if api_request_count == 3 else "query_budget_exhausted"
                )
                return SearchResult(
                    places=[
                        {
                            "cid": "cid-1",
                            "title": "Firm One",
                            "address": "1 Main St",
                            "phoneNumber": "(555) 0100",
                            "category": "Architect",
                        }
                    ],
                    latency_ms=42,
                    retry_count=1,
                    api_request_count=api_request_count,
                    estimated_credit_usage=api_request_count,
                    pagination_stop_reason=pagination_stop_reason,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            store = Store(temp_path / "scrape.db")
            keyword = Keyword(
                id="architect",
                category="architect",
                query="Architect",
                enabled=True,
                priority=10,
            )
            locations = [
                LocationRecord(zip_code="73301", city="Austin", state="TX", country="US"),
            ]
            run_id = generate_run_id("raw_zip_max")
            store.start_run(
                run_id=run_id,
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )

            try:
                result = _run_state_keyword_mode(
                    store=store,
                    client=FakePaginatedResultClient(),
                    locations=locations,
                    keyword=keyword,
                    state="TX",
                    mode="raw_zip_max",
                    run_id=run_id,
                    max_serper_queries=2,
                )
                request_rows = store.fetch_run_request_rows(run_id)
            finally:
                store.close()

        self.assertEqual(result["total_requests"], 2)
        self.assertEqual(result["estimated_credits"], 2)
        self.assertEqual(result["total_results"], 1)
        self.assertEqual(result["unique_businesses_found"], 1)
        self.assertEqual(len(request_rows), 1)
        self.assertEqual(request_rows[0]["api_request_count"], 2)
        self.assertEqual(request_rows[0]["pagination_stop_reason"], "query_budget_exhausted")

    def test_run_state_keyword_mode_applies_per_seed_cap_with_global_budget(self) -> None:
        class FakePerZipBudgetClient(SerperClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", base_url="https://google.serper.dev")
                self.max_page_requests_seen: list[int | None] = []

            def search_places(
                self,
                *,
                query: str,
                gl: str = "us",
                hl: str = "en",
                autocorrect: bool = True,
                max_page_requests: int | None = None,
            ) -> SearchResult:
                self.max_page_requests_seen.append(max_page_requests)
                budget = max_page_requests or 1
                zip_code = query.split(",")[1].strip()
                return SearchResult(
                    places=[
                        {
                            "cid": f"cid-{zip_code}",
                            "title": f"Firm {zip_code}",
                            "address": f"{zip_code} Main St",
                            "phoneNumber": "(555) 0100",
                            "category": "Architect",
                        }
                    ],
                    latency_ms=10,
                    retry_count=0,
                    api_request_count=budget,
                    estimated_credit_usage=budget,
                    pagination_stop_reason="query_budget_exhausted",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            store = Store(temp_path / "scrape.db")
            keyword = Keyword(
                id="architect",
                category="architect",
                query="Architect",
                enabled=True,
                priority=10,
            )
            locations = [
                LocationRecord(zip_code="73301", city="Austin", state="TX", country="US"),
                LocationRecord(zip_code="73344", city="Austin", state="TX", country="US"),
            ]
            client = FakePerZipBudgetClient()
            run_id = generate_run_id("raw_zip_max")
            store.start_run(
                run_id=run_id,
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )

            try:
                result = _run_state_keyword_mode(
                    store=store,
                    client=client,
                    locations=locations,
                    keyword=keyword,
                    state="TX",
                    mode="raw_zip_max",
                    run_id=run_id,
                    max_serper_queries=5,
                    max_serper_queries_per_seed=3,
                )
            finally:
                store.close()

        self.assertEqual(client.max_page_requests_seen, [3, 2])
        self.assertEqual(result["total_requests"], 5)
        self.assertEqual(result["estimated_credits"], 5)
        self.assertEqual(result["serper_queries_remaining"], 0)
        self.assertEqual(result["seeds_ran"], ["raw_zip:73301, Austin", "raw_zip:73344, Austin"])
        self.assertEqual(result["unique_businesses_found"], 2)

    def test_run_state_keyword_mode_parallel_workers_share_duplicate_business_safely(self) -> None:
        class FakeConcurrentClient(SerperClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", base_url="https://google.serper.dev")
                self._lock = threading.Lock()
                self.active_calls = 0
                self.max_active_calls = 0

            def search_places(
                self,
                *,
                query: str,
                gl: str = "us",
                hl: str = "en",
                autocorrect: bool = True,
                max_page_requests: int | None = None,
            ) -> SearchResult:
                with self._lock:
                    self.active_calls += 1
                    self.max_active_calls = max(self.max_active_calls, self.active_calls)
                try:
                    time.sleep(0.02)
                    return SearchResult(
                        places=[
                            {
                                "cid": "shared-cid",
                                "title": "Shared Firm",
                                "address": "1 Main St",
                                "phoneNumber": "(555) 0100",
                                "category": "Architect",
                            }
                        ],
                        latency_ms=10,
                        retry_count=0,
                        api_request_count=1,
                        estimated_credit_usage=1,
                        pagination_stop_reason="short_page",
                    )
                finally:
                    with self._lock:
                        self.active_calls -= 1

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "scrape.db"
            store = Store(db_path)
            keyword = Keyword(
                id="architect",
                category="architect",
                query="Architect",
                enabled=True,
                priority=10,
            )
            locations = [
                LocationRecord(zip_code="73301", city="Austin", state="TX", country="US"),
                LocationRecord(zip_code="73344", city="Austin", state="TX", country="US"),
                LocationRecord(zip_code="75001", city="Addison", state="TX", country="US"),
                LocationRecord(zip_code="78701", city="Austin", state="TX", country="US"),
            ]
            client = FakeConcurrentClient()
            run_id = generate_run_id("raw_zip_max")
            store.start_run(
                run_id=run_id,
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )

            try:
                result = _run_state_keyword_mode(
                    store=store,
                    client=client,
                    locations=locations,
                    keyword=keyword,
                    state="TX",
                    mode="raw_zip_max",
                    run_id=run_id,
                    max_serper_queries_per_seed=1,
                    workers=4,
                    store_factory=lambda: Store(db_path),
                )
                request_rows = store.fetch_run_request_rows(run_id)
            finally:
                store.close()

        self.assertGreater(client.max_active_calls, 1)
        self.assertEqual(result["total_requests"], 4)
        self.assertEqual(result["total_results"], 4)
        self.assertEqual(result["unique_businesses_found"], 1)
        self.assertEqual(result["new_unique_businesses_added"], 1)
        self.assertEqual(result["duplicates_collapsed"], 3)
        self.assertEqual(
            result["seeds_ran"],
            ["raw_zip:73301, Austin", "raw_zip:73344, Austin", "raw_zip:75001, Addison", "raw_zip:78701, Austin"],
        )
        self.assertEqual(len(request_rows), 4)

    def test_execute_seed_request_skips_when_same_fingerprint_is_in_progress(self) -> None:
        class BlockingClient(SerperClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", base_url="https://google.serper.dev")
                self.started = threading.Event()
                self.release = threading.Event()
                self.calls = 0
                self._lock = threading.Lock()

            def search_places(
                self,
                *,
                query: str,
                gl: str = "us",
                hl: str = "en",
                autocorrect: bool = True,
                max_page_requests: int | None = None,
            ) -> SearchResult:
                with self._lock:
                    self.calls += 1
                self.started.set()
                self.release.wait(timeout=1)
                return SearchResult(
                    places=[
                        {
                            "cid": "cid-1",
                            "title": "Firm One",
                            "address": "1 Main St",
                            "phoneNumber": "(555) 0100",
                            "category": "Architect",
                        }
                    ],
                    latency_ms=10,
                    retry_count=0,
                    api_request_count=1,
                    estimated_credit_usage=1,
                    pagination_stop_reason="short_page",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "scrape.db"
            bootstrap = Store(db_path)
            keyword = Keyword(
                id="architect",
                category="architect",
                query="Architect",
                enabled=True,
                priority=10,
            )
            seed = Seed(seed_type="raw_zip", seed_value="73301, Austin", zip_code="73301", state="TX", city="Austin", country="US")
            bootstrap.start_run(
                run_id="run-a",
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )
            bootstrap.start_run(
                run_id="run-b",
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )
            bootstrap.close()

            store_b = Store(db_path)
            client = BlockingClient()
            first_result: dict[str, object] = {}
            thread_error: dict[str, Exception] = {}

            def _execute_seed_request_in_thread(
                thread_db_path: Path,
                thread_client: SerperClient,
                thread_keyword: Keyword,
                thread_seed: Seed,
            ):
                store_a = Store(thread_db_path)
                try:
                    return _execute_seed_request(
                        store=store_a,
                        client=thread_client,
                        keyword=thread_keyword,
                        state="TX",
                        mode="raw_zip_max",
                        seed=thread_seed,
                        run_id="run-a",
                    )
                finally:
                    store_a.close()

            try:
                def run_first_request() -> None:
                    try:
                        first_result["result"] = _execute_seed_request_in_thread(
                            db_path,
                            client,
                            keyword,
                            seed,
                        )
                    except Exception as exc:  # pragma: no cover - assertion path
                        thread_error["error"] = exc

                thread = threading.Thread(
                    target=run_first_request
                )
                thread.start()
                started = client.started.wait(timeout=5)
                if not started:
                    thread.join(timeout=0.1)
                    self.fail(
                        f"First request never reached search_places; thread_error={thread_error.get('error')!r}"
                    )

                second_result = _execute_seed_request(
                    store=store_b,
                    client=client,
                    keyword=keyword,
                    state="TX",
                    mode="raw_zip_max",
                    seed=seed,
                    run_id="run-b",
                )
                self.assertFalse(second_result.performed_request)
                self.assertEqual(second_result.status, "in_progress")
                self.assertEqual(client.calls, 1)

                client.release.set()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertNotIn("error", thread_error)
                self.assertIn("result", first_result)
                self.assertTrue(getattr(first_result["result"], "performed_request"))
            finally:
                store_b.close()

    def test_execute_seed_request_reclaims_stale_in_progress_claim(self) -> None:
        class CountingClient(SerperClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", base_url="https://google.serper.dev")
                self.calls = 0

            def search_places(
                self,
                *,
                query: str,
                gl: str = "us",
                hl: str = "en",
                autocorrect: bool = True,
                max_page_requests: int | None = None,
            ) -> SearchResult:
                self.calls += 1
                return SearchResult(
                    places=[
                        {
                            "cid": "cid-1",
                            "title": "Firm One",
                            "address": "1 Main St",
                            "phoneNumber": "(555) 0100",
                            "category": "Architect",
                        }
                    ],
                    latency_ms=10,
                    retry_count=0,
                    api_request_count=1,
                    estimated_credit_usage=1,
                    pagination_stop_reason="short_page",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "scrape.db"
            store = Store(db_path)
            keyword = Keyword(
                id="architect",
                category="architect",
                query="Architect",
                enabled=True,
                priority=10,
            )
            seed = Seed(seed_type="raw_zip", seed_value="73301, Austin", zip_code="73301", state="TX", city="Austin", country="US")
            fingerprint = build_request_fingerprint(
                mode="raw_zip_max",
                keyword_id=keyword.id,
                state="TX",
                seed_type=seed.seed_type,
                seed_value=seed.seed_value,
            )
            store.start_run(
                run_id="run-a",
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )
            store.start_run(
                run_id="run-b",
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )
            store.record_request(
                {
                    "fingerprint": fingerprint,
                    "run_id": "run-a",
                    "mode": "raw_zip_max",
                    "keyword_id": keyword.id,
                    "state": "TX",
                    "seed_type": seed.seed_type,
                    "seed_value": seed.seed_value,
                    "query_text": "Architect 73301",
                    "status": "in_progress",
                    "http_status": None,
                    "latency_ms": 0,
                    "result_count": 0,
                    "new_unique_businesses": 0,
                    "api_request_count": 0,
                    "retry_count": 0,
                    "estimated_credit_usage": 0,
                    "pagination_stop_reason": None,
                    "error_message": None,
                }
            )
            stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            store.connection.execute(
                "UPDATE requests SET created_at = ? WHERE fingerprint = ?",
                (stale_created_at, fingerprint),
            )
            store.connection.commit()

            try:
                client = CountingClient()
                result = _execute_seed_request(
                    store=store,
                    client=client,
                    keyword=keyword,
                    state="TX",
                    mode="raw_zip_max",
                    seed=seed,
                    run_id="run-b",
                )
                request_row = store.fetch_request(fingerprint)
            finally:
                store.close()

        self.assertTrue(result.performed_request)
        self.assertEqual(client.calls, 1)
        self.assertEqual(request_row["status"], "success")
        self.assertEqual(request_row["run_id"], "run-b")

    def test_execute_seed_request_preserves_fetch_accounting_on_post_fetch_failure(self) -> None:
        class FakeFetchedResultClient(SerperClient):
            def __init__(self) -> None:
                super().__init__(api_key="test-key", base_url="https://google.serper.dev")

            def search_places(
                self,
                *,
                query: str,
                gl: str = "us",
                hl: str = "en",
                autocorrect: bool = True,
                max_page_requests: int | None = None,
            ) -> SearchResult:
                return SearchResult(
                    places=[
                        {
                            "cid": "cid-1",
                            "title": "Firm One",
                            "address": "1 Main St",
                            "phoneNumber": "(555) 0100",
                            "category": "Architect",
                        }
                    ],
                    latency_ms=42,
                    retry_count=0,
                    api_request_count=2,
                    estimated_credit_usage=2,
                    pagination_stop_reason="short_page",
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "scrape.db"
            store = Store(db_path)
            keyword = Keyword(
                id="architect",
                category="architect",
                query="Architect",
                enabled=True,
                priority=10,
            )
            seed = Seed(seed_type="raw_zip", seed_value="73301, Austin", zip_code="73301", state="TX", city="Austin", country="US")
            fingerprint = build_request_fingerprint(
                mode="raw_zip_max",
                keyword_id=keyword.id,
                state="TX",
                seed_type=seed.seed_type,
                seed_value=seed.seed_value,
            )
            store.start_run(
                run_id="run-a",
                mode="raw_zip_max",
                run_kind="seed",
                targeted_states=["TX"],
                targeted_keyword_ids=[keyword.id],
                command_payload={"command": "seed"},
            )

            try:
                with patch("scraper.core._normalize_businesses", side_effect=RuntimeError("boom")):
                    with self.assertRaisesRegex(RuntimeError, "boom"):
                        _execute_seed_request(
                            store=store,
                            client=FakeFetchedResultClient(),
                            keyword=keyword,
                            state="TX",
                            mode="raw_zip_max",
                            seed=seed,
                            run_id="run-a",
                        )
                request_row = store.fetch_request(fingerprint)
            finally:
                store.close()

        self.assertEqual(request_row["status"], "error")
        self.assertEqual(request_row["api_request_count"], 2)
        self.assertEqual(request_row["estimated_credit_usage"], 2)
        self.assertEqual(request_row["latency_ms"], 42)



if __name__ == "__main__":
    unittest.main()
