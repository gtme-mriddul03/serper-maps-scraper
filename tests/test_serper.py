from __future__ import annotations

import json
import unittest

from scraper.serper import INFERRED_PLACES_PAGE_SIZE, SerperClient


def build_place(cid: str, title: str) -> dict[str, object]:
    return {
        "cid": cid,
        "title": title,
        "address": f"{title} Address",
        "phoneNumber": "(555) 0100",
        "category": "Architect",
    }


class FakePaginatedClient(SerperClient):
    def __init__(self, pages: dict[int, list[dict[str, object]]]) -> None:
        super().__init__(api_key="test-key", base_url="https://google.serper.dev")
        self.pages = pages
        self.seen_payloads: list[dict[str, object]] = []

    def _post_json(self, url: str, payload: dict[str, object]) -> str:
        self.seen_payloads.append(dict(payload))
        page = int(payload.get("page", 1))
        return json.dumps({"places": self.pages.get(page, [])})


class SerperPaginationTests(unittest.TestCase):
    def test_places_continues_past_five_pages_when_results_continue(self) -> None:
        pages = {
            page: [build_place(f"{page}-{index}", f"Firm {page}-{index}") for index in range(INFERRED_PLACES_PAGE_SIZE)]
            for page in range(1, 7)
        }
        pages[7] = [build_place("7-0", "Firm 7-0")]
        client = FakePaginatedClient(pages)

        result = client.search_places(query="Architect Austin, TX")

        self.assertEqual(len(result.places), INFERRED_PLACES_PAGE_SIZE * 6 + 1)
        self.assertEqual(result.api_request_count, 7)
        self.assertEqual(result.estimated_credit_usage, 7)
        self.assertEqual(result.pagination_stop_reason, "short_page")
        self.assertEqual(client.seen_payloads[-1].get("page"), 7)

    def test_places_paginates_when_first_page_is_full(self) -> None:
        page_one = [build_place(str(index), f"Firm {index}") for index in range(INFERRED_PLACES_PAGE_SIZE)]
        page_two = [build_place("10", "Firm 10"), build_place("11", "Firm 11")]
        client = FakePaginatedClient({1: page_one, 2: page_two})

        result = client.search_places(query="Architect Austin, TX")

        self.assertEqual(len(result.places), INFERRED_PLACES_PAGE_SIZE + 2)
        self.assertEqual(result.api_request_count, 2)
        self.assertEqual(result.estimated_credit_usage, 2)
        self.assertEqual(result.pagination_stop_reason, "short_page")
        self.assertEqual(client.seen_payloads[0].get("page"), None)
        self.assertEqual(client.seen_payloads[1].get("page"), 2)

    def test_places_stops_on_repeated_page(self) -> None:
        page_one = [build_place(str(index), f"Firm {index}") for index in range(INFERRED_PLACES_PAGE_SIZE)]
        client = FakePaginatedClient({1: page_one, 2: page_one})

        result = client.search_places(query="Architect Boston, MA")

        self.assertEqual(len(result.places), INFERRED_PLACES_PAGE_SIZE)
        self.assertEqual(result.api_request_count, 2)
        self.assertEqual(result.estimated_credit_usage, 2)
        self.assertEqual(result.pagination_stop_reason, "repeated_page")

    def test_places_does_not_paginate_on_short_first_page(self) -> None:
        client = FakePaginatedClient({1: [build_place("1", "Solo Firm")]})

        result = client.search_places(query="Architect Dover, DE")

        self.assertEqual(len(result.places), 1)
        self.assertEqual(result.api_request_count, 1)
        self.assertEqual(result.estimated_credit_usage, 1)
        self.assertEqual(result.pagination_stop_reason, "short_page")
        self.assertEqual(len(client.seen_payloads), 1)

    def test_places_stops_when_query_budget_is_exhausted(self) -> None:
        page_one = [build_place(str(index), f"Firm {index}") for index in range(INFERRED_PLACES_PAGE_SIZE)]
        page_two = [build_place("10", "Firm 10")]
        client = FakePaginatedClient({1: page_one, 2: page_two})

        result = client.search_places(query="Architect Seattle, WA", max_page_requests=1)

        self.assertEqual(len(result.places), INFERRED_PLACES_PAGE_SIZE)
        self.assertEqual(result.api_request_count, 1)
        self.assertEqual(result.estimated_credit_usage, 1)
        self.assertEqual(result.pagination_stop_reason, "query_budget_exhausted")
        self.assertEqual(len(client.seen_payloads), 1)


if __name__ == "__main__":
    unittest.main()
