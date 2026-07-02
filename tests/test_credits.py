from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import serper_credits


class FakeHttpResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class SerperCreditsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_main_reads_dotenv_and_prints_credit_balance(self) -> None:
        (self.project_root / ".env").write_text("SERPER_API_KEY=dotenv-test-key\n", encoding="utf-8")
        seen_requests = []

        def fake_urlopen(req, timeout):
            seen_requests.append((req, timeout))
            return FakeHttpResponse(
                {
                    "usageToday": 12,
                    "usageLastMonth": 345,
                    "creditBalance": 678,
                }
            )

        stdout = io.StringIO()
        with patch.dict(
            os.environ,
            {"SCRAPER_PROJECT_ROOT": str(self.project_root)},
            clear=True,
        ):
            with patch("serper_credits.request.urlopen", new=fake_urlopen):
                with redirect_stdout(stdout):
                    exit_code = serper_credits.main()

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["creditBalance"], 678)
        self.assertEqual(payload["serper_api_key_source"], ".env")
        self.assertEqual(payload["endpoint"], serper_credits.SERPER_STATS_URL)
        self.assertEqual(seen_requests[0][1], serper_credits.REQUEST_TIMEOUT_SECONDS)
        request_headers = dict(seen_requests[0][0].header_items())
        self.assertEqual(request_headers["X-api-key"], "dotenv-test-key")

    def test_main_requires_api_key(self) -> None:
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {"SCRAPER_PROJECT_ROOT": str(self.project_root)},
            clear=True,
        ):
            with redirect_stderr(stderr):
                exit_code = serper_credits.main()

        self.assertEqual(exit_code, 1)
        self.assertIn("SERPER_API_KEY is required", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
