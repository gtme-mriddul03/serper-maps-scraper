from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scraper import __main__ as cli


class FakeSearchResult:
    def __init__(
        self,
        places,
        latency_ms=10,
        retry_count=0,
        api_request_count=1,
        estimated_credit_usage=1,
        pagination_stop_reason="short_page",
    ):
        self.places = places
        self.latency_ms = latency_ms
        self.retry_count = retry_count
        self.api_request_count = api_request_count
        self.estimated_credit_usage = estimated_credit_usage
        self.pagination_stop_reason = pagination_stop_reason


class CliFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        (self.project_root / "config").mkdir(parents=True)
        (self.project_root / "data").mkdir(parents=True)
        (self.project_root / "outputs").mkdir(parents=True)
        (self.project_root / "config" / "keywords.yaml").write_text(
            "keywords:\n"
            "  - id: dentists\n"
            "    category: healthcare\n"
            "    query: dentist\n"
            "    enabled: true\n"
            "    priority: 10\n",
            encoding="utf-8",
        )
        (self.project_root / "us_input_locations_for_maps.csv").write_text(
            "Locations\n"
            "\"10001, New York, NY, US\"\n"
            "\"10002, New York, NY, US\"\n"
            "\"12207, Albany, NY, US\"\n",
            encoding="utf-8",
        )
        self.env_patch = patch.dict(
            os.environ,
            {
                "SCRAPER_PROJECT_ROOT": str(self.project_root),
                "SERPER_API_KEY": "test-api-key",
            },
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def test_validate_config_cli(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = cli.main(["validate-config"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["state_count"], 1)
        self.assertEqual(payload["enabled_keyword_ids"], ["dentists"])
        self.assertEqual(payload["serper_api_key_source"], "environment")

    def test_validate_config_reads_dotenv_when_shell_key_is_missing(self) -> None:
        (self.project_root / ".env").write_text(
            "SERPER_API_KEY=dotenv-test-key\n"
            "SERPER_BASE_URL=https://dotenv.example.test\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"SCRAPER_PROJECT_ROOT": str(self.project_root)},
            clear=True,
        ):
            with redirect_stdout(stdout):
                exit_code = cli.main(["validate-config"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["env_file_present"])
        self.assertTrue(payload["serper_api_key_present"])
        self.assertEqual(payload["serper_api_key_source"], ".env")
        self.assertEqual(payload["serper_base_url"], "https://dotenv.example.test")
        self.assertEqual(payload["serper_base_url_source"], ".env")




    def test_seed_cli_uses_legacy_seed_to_select_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "config").mkdir(parents=True)
            (project_root / "data").mkdir(parents=True)
            (project_root / "outputs").mkdir(parents=True)
            (project_root / "config" / "keywords.yaml").write_text(
                "keywords:\n"
                "  - id: architect\n"
                "    category: architect\n"
                "    query: Architect\n"
                "    enabled: true\n"
                "    priority: 10\n",
                encoding="utf-8",
            )
            (project_root / "us_input_locations_for_maps.csv").write_text(
                "Locations\n"
                "\"15018, Buena Vista, PA, US\"\n"
                "\"15019, Buena Vista, PA, US\"\n"
                "\"10001, New York, NY, US\"\n",
                encoding="utf-8",
            )

            env_patch = patch.dict(
                os.environ,
                {
                    "SCRAPER_PROJECT_ROOT": str(project_root),
                    "SERPER_API_KEY": "test-api-key",
                },
                clear=False,
            )
            env_patch.start()
            try:
                seen_queries: list[str] = []

                def fake_search_places(
                    self,
                    *,
                    query,
                    gl="us",
                    hl="en",
                    autocorrect=True,
                    max_page_requests=None,
                ):
                    seen_queries.append(query)
                    return FakeSearchResult([])

                with patch("scraper.core.SerperClient.search_places", new=fake_search_places):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        exit_code = cli.main(
                            [
                                "seed",
                                "--mode",
                                "raw_zip_max",
                                "--keyword-id",
                                "architect",
                                "--seed",
                                "architects, 15018, Buena Vista, PA, US",
                            ]
                        )
                    self.assertEqual(exit_code, 0)
                    payload = json.loads(stdout.getvalue())
                    self.assertEqual(payload["targeted_state"], "PA")
                    self.assertEqual(payload["state_seed_count"], 2)
                    self.assertEqual(payload["seed_keyword_hint"], "architects")
                    self.assertEqual(
                        seen_queries,
                        [
                            "Architect, 15018, Buena Vista, PA, US",
                            "Architect, 15019, Buena Vista, PA, US",
                        ],
                    )
            finally:
                env_patch.stop()

    def test_seed_cli_applies_per_seed_cap_for_legacy_seed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "config").mkdir(parents=True)
            (project_root / "data").mkdir(parents=True)
            (project_root / "outputs").mkdir(parents=True)
            (project_root / "config" / "keywords.yaml").write_text(
                "keywords:\n"
                "  - id: architect\n"
                "    category: architect\n"
                "    query: Architect\n"
                "    enabled: true\n"
                "    priority: 10\n",
                encoding="utf-8",
            )
            (project_root / "us_input_locations_for_maps.csv").write_text(
                "Locations\n"
                "\"15018, Buena Vista, PA, US\"\n"
                "\"15019, Buena Vista, PA, US\"\n",
                encoding="utf-8",
            )

            env_patch = patch.dict(
                os.environ,
                {
                    "SCRAPER_PROJECT_ROOT": str(project_root),
                    "SERPER_API_KEY": "test-api-key",
                },
                clear=False,
            )
            env_patch.start()
            try:
                max_page_requests_seen: list[int | None] = []

                def fake_search_places(
                    self,
                    *,
                    query,
                    gl="us",
                    hl="en",
                    autocorrect=True,
                    max_page_requests=None,
                ):
                    max_page_requests_seen.append(max_page_requests)
                    budget = max_page_requests or 1
                    return FakeSearchResult(
                        [],
                        api_request_count=budget,
                        estimated_credit_usage=budget,
                        pagination_stop_reason="query_budget_exhausted",
                    )

                with patch("scraper.core.SerperClient.search_places", new=fake_search_places):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        exit_code = cli.main(
                            [
                                "seed",
                                "--mode",
                                "raw_zip_max",
                                "--keyword-id",
                                "architect",
                                "--seed",
                                "architects, 15018, Buena Vista, PA, US",
                                "--max-serper-queries-per-seed",
                                "3",
                            ]
                        )
                    self.assertEqual(exit_code, 0)
                    payload = json.loads(stdout.getvalue())
                    self.assertEqual(max_page_requests_seen, [3, 3])
                    self.assertEqual(payload["requested_max_serper_queries_per_seed"], 3)
                    self.assertEqual(payload["max_possible_serper_queries_at_seed_cap"], 6)
                    self.assertEqual(payload["effective_max_serper_queries"], 6)
                    self.assertEqual(payload["total_requests"], 6)
            finally:
                env_patch.stop()

    def test_seed_cli_runs_threaded_raw_zip_max_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "config").mkdir(parents=True)
            (project_root / "data").mkdir(parents=True)
            (project_root / "outputs").mkdir(parents=True)
            (project_root / "config" / "keywords.yaml").write_text(
                "keywords:\n"
                "  - id: architect\n"
                "    category: architect\n"
                "    query: Architect\n"
                "    enabled: true\n"
                "    priority: 10\n",
                encoding="utf-8",
            )
            (project_root / "us_input_locations_for_maps.csv").write_text(
                "Locations\n"
                "\"15018, Buena Vista, PA, US\"\n"
                "\"15019, Buena Vista, PA, US\"\n"
                "\"15020, Buena Vista, PA, US\"\n"
                "\"15021, Buena Vista, PA, US\"\n",
                encoding="utf-8",
            )

            env_patch = patch.dict(
                os.environ,
                {
                    "SCRAPER_PROJECT_ROOT": str(project_root),
                    "SERPER_API_KEY": "test-api-key",
                },
                clear=False,
            )
            env_patch.start()
            try:
                lock = threading.Lock()
                active_calls = 0
                max_active_calls = 0

                def fake_search_places(
                    self,
                    *,
                    query,
                    gl="us",
                    hl="en",
                    autocorrect=True,
                    max_page_requests=None,
                ):
                    nonlocal active_calls, max_active_calls
                    with lock:
                        active_calls += 1
                        max_active_calls = max(max_active_calls, active_calls)
                    try:
                        time.sleep(0.02)
                        return FakeSearchResult(
                            [
                                {
                                    "cid": "shared-cid",
                                    "title": "Shared Firm",
                                    "address": "1 Main St",
                                    "phoneNumber": "(555) 0100",
                                    "category": "Architect",
                                }
                            ],
                            api_request_count=1,
                            estimated_credit_usage=1,
                            pagination_stop_reason="short_page",
                        )
                    finally:
                        with lock:
                            active_calls -= 1

                with patch("scraper.core.SerperClient.search_places", new=fake_search_places):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        exit_code = cli.main(
                            [
                                "seed",
                                "--mode",
                                "raw_zip_max",
                                "--keyword-id",
                                "architect",
                                "--seed",
                                "architects, 15018, Buena Vista, PA, US",
                                "--max-serper-queries-per-seed",
                                "1",
                                "--workers",
                                "4",
                            ]
                        )
                    self.assertEqual(exit_code, 0)
                    payload = json.loads(stdout.getvalue())
                    self.assertEqual(payload["workers_requested"], 4)
                    self.assertEqual(payload["total_requests"], 4)
                    self.assertEqual(payload["unique_businesses_found"], 1)
                    self.assertEqual(payload["new_unique_businesses_added"], 1)
                    self.assertGreater(max_active_calls, 1)
                    with Path(payload["request_log_path"]).open(newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                    self.assertEqual(len(rows), 4)
            finally:
                env_patch.stop()

    def test_seed_cli_rejects_mismatched_legacy_keyword_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "config").mkdir(parents=True)
            (project_root / "data").mkdir(parents=True)
            (project_root / "outputs").mkdir(parents=True)
            (project_root / "config" / "keywords.yaml").write_text(
                "keywords:\n"
                "  - id: dentists\n"
                "    category: healthcare\n"
                "    query: Dentist\n"
                "    enabled: true\n"
                "    priority: 10\n",
                encoding="utf-8",
            )
            (project_root / "us_input_locations_for_maps.csv").write_text(
                "Locations\n"
                "\"10001, New York, NY, US\"\n",
                encoding="utf-8",
            )

            env_patch = patch.dict(
                os.environ,
                {
                    "SCRAPER_PROJECT_ROOT": str(project_root),
                    "SERPER_API_KEY": "test-api-key",
                },
                clear=False,
            )
            env_patch.start()
            try:
                stderr = io.StringIO()
                with patch("scraper.core.SerperClient.search_places") as mock_search_places:
                    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                        exit_code = cli.main(
                            [
                                "seed",
                                "--mode",
                                "raw_zip_max",
                                "--keyword-id",
                                "dentists",
                                "--seed",
                                "architects, 10001, New York, NY, US",
                            ]
                        )

                self.assertEqual(exit_code, 1)
                self.assertIn(
                    "Legacy seed keyword prefix 'architects' does not match selected keyword id 'dentists' "
                    "or query 'Dentist'.",
                    stderr.getvalue(),
                )
                mock_search_places.assert_not_called()
            finally:
                env_patch.stop()



    def test_seed_cli_rejects_workers_with_global_query_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            (project_root / "config").mkdir(parents=True)
            (project_root / "data").mkdir(parents=True)
            (project_root / "outputs").mkdir(parents=True)
            (project_root / "config" / "keywords.yaml").write_text(
                "keywords:\n"
                "  - id: architect\n"
                "    category: architect\n"
                "    query: Architect\n"
                "    enabled: true\n"
                "    priority: 10\n",
                encoding="utf-8",
            )
            (project_root / "us_input_locations_for_maps.csv").write_text(
                "Locations\n"
                "\"15018, Buena Vista, PA, US\"\n"
                "\"15019, Buena Vista, PA, US\"\n",
                encoding="utf-8",
            )

            env_patch = patch.dict(
                os.environ,
                {
                    "SCRAPER_PROJECT_ROOT": str(project_root),
                    "SERPER_API_KEY": "test-api-key",
                },
                clear=False,
            )
            env_patch.start()
            try:
                stderr = io.StringIO()
                with patch("scraper.core.SerperClient.search_places") as mock_search_places:
                    with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                        exit_code = cli.main(
                            [
                                "seed",
                                "--mode",
                                "raw_zip_max",
                                "--keyword-id",
                                "architect",
                                "--seed",
                                "architects, 15018, Buena Vista, PA, US",
                                "--workers",
                                "4",
                                "--max-serper-queries",
                                "10",
                            ]
                        )

                self.assertEqual(exit_code, 1)
                self.assertIn(
                    "workers > 1 is not supported together with max_serper_queries.",
                    stderr.getvalue(),
                )
                mock_search_places.assert_not_called()
            finally:
                env_patch.stop()


if __name__ == "__main__":
    unittest.main()
