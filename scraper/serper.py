from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from hashlib import sha1
from typing import Any
from urllib import error, request


DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES = 2
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# Inferred from Serper's public Places example, which returns 10 businesses.
INFERRED_PLACES_PAGE_SIZE = 10


class SerperRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_count: int = 0,
        latency_ms: int = 0,
        api_request_count: int = 0,
        estimated_credit_usage: int = 0,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_count = retry_count
        self.latency_ms = latency_ms
        self.api_request_count = api_request_count
        self.estimated_credit_usage = estimated_credit_usage


@dataclass(frozen=True)
class SearchResult:
    places: list[dict[str, Any]]
    latency_ms: int
    retry_count: int
    api_request_count: int
    estimated_credit_usage: int
    pagination_stop_reason: str


@dataclass(frozen=True)
class NormalizedBusiness:
    stable_business_id: str
    cid: str | None
    title: str
    address: str
    phone: str | None
    website: str | None
    source_category: str | None
    latitude: float | None
    longitude: float | None
    rating: float | None
    rating_count: int | None
    raw_payload: str


class SerperClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("SERPER_API_KEY is required for seed runs.")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def search_places(
        self,
        *,
        query: str,
        gl: str = "us",
        hl: str = "en",
        autocorrect: bool = True,
        max_page_requests: int | None = None,
    ) -> SearchResult:
        payload = {
            "q": query,
            "gl": gl,
            "hl": hl,
            "autocorrect": autocorrect,
        }
        url = f"{self.base_url}/places"
        total_retry_count = 0
        started_at = time.perf_counter()
        aggregated_places: list[dict[str, Any]] = []
        aggregated_business_ids: set[str] = set()
        seen_page_signatures: set[str] = set()
        successful_page_requests = 0
        pagination_stop_reason = "query_budget_exhausted"

        if max_page_requests is not None and max_page_requests <= 0:
            return SearchResult(
                places=[],
                latency_ms=_elapsed_ms(started_at),
                retry_count=0,
                api_request_count=0,
                estimated_credit_usage=0,
                pagination_stop_reason="query_budget_exhausted",
            )

        page = 1
        while True:
            if max_page_requests is not None and successful_page_requests >= max_page_requests:
                pagination_stop_reason = "query_budget_exhausted"
                break

            page_payload = dict(payload)
            if page > 1:
                page_payload["page"] = page

            places, page_retry_count = self._fetch_places_page(
                url=url,
                payload=page_payload,
                started_at=started_at,
                successful_page_requests=successful_page_requests,
            )
            total_retry_count += page_retry_count
            successful_page_requests += 1

            if not places:
                pagination_stop_reason = "empty_page"
                break

            page_signature = _page_signature(places)
            if page_signature in seen_page_signatures:
                pagination_stop_reason = "repeated_page"
                break

            page_business_ids = _page_business_ids(places)
            page_new_business_ids = page_business_ids - aggregated_business_ids
            if page > 1 and not page_new_business_ids:
                pagination_stop_reason = "no_new_businesses"
                break

            seen_page_signatures.add(page_signature)
            aggregated_places.extend(places)
            aggregated_business_ids.update(page_business_ids)

            if len(places) < INFERRED_PLACES_PAGE_SIZE:
                pagination_stop_reason = "short_page"
                break

            page += 1

        return SearchResult(
            places=aggregated_places,
            latency_ms=_elapsed_ms(started_at),
            retry_count=total_retry_count,
            api_request_count=successful_page_requests,
            estimated_credit_usage=successful_page_requests,
            pagination_stop_reason=pagination_stop_reason,
        )

    def _fetch_places_page(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        started_at: float,
        successful_page_requests: int,
    ) -> tuple[list[dict[str, Any]], int]:
        page_retry_count = 0
        for attempt in range(MAX_RETRIES + 1):
            try:
                raw_response = self._post_json(url, payload)
                response_payload = json.loads(raw_response)
                places = response_payload.get("places", [])
                if not isinstance(places, list):
                    raise SerperRequestError(
                        "Serper response did not include a places list.",
                        retry_count=page_retry_count,
                        latency_ms=_elapsed_ms(started_at),
                        api_request_count=successful_page_requests + 1,
                        estimated_credit_usage=successful_page_requests,
                    )
                return places, page_retry_count
            except SerperRequestError as exc:
                should_retry = exc.status_code in RETRYABLE_HTTP_STATUSES or exc.status_code is None
                if attempt >= MAX_RETRIES or not should_retry:
                    exc.retry_count = page_retry_count
                    exc.latency_ms = _elapsed_ms(started_at)
                    exc.api_request_count = successful_page_requests + 1
                    exc.estimated_credit_usage = successful_page_requests
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
                page_retry_count += 1

        raise AssertionError("Unreachable page retry loop termination.")

    def _post_json(self, url: str, payload: dict[str, Any]) -> str:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-KEY": self.api_key,
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                return body
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = _extract_error_message(body) or f"Serper request failed with HTTP {exc.code}."
            raise SerperRequestError(message, status_code=exc.code) from exc
        except error.URLError as exc:
            raise SerperRequestError(f"Serper request failed: {exc.reason}") from exc


def normalize_place(place: dict[str, Any]) -> NormalizedBusiness | None:
    title = _clean_text(place.get("title"))
    address = _clean_text(place.get("address"))
    cid = _clean_text(place.get("cid"))
    phone = _clean_text(place.get("phoneNumber") or place.get("phone"))
    website = _clean_text(place.get("website") or place.get("link"))
    source_category = _clean_text(
        place.get("category")
        or place.get("type")
        or (place.get("types") or [None])[0]
    )
    latitude = _coerce_float(place.get("latitude"))
    longitude = _coerce_float(place.get("longitude"))
    rating = _coerce_float(place.get("rating"))
    rating_count = _coerce_int(place.get("ratingCount"))

    if not title and not cid:
        return None
    if not address and not cid:
        return None

    stable_business_id = _stable_business_id(
        cid=cid,
        title=title,
        address=address,
        phone=phone,
    )
    raw_payload = json.dumps(place, sort_keys=True, ensure_ascii=True)

    return NormalizedBusiness(
        stable_business_id=stable_business_id,
        cid=cid,
        title=title or "(untitled business)",
        address=address or "(unknown address)",
        phone=phone,
        website=website,
        source_category=source_category,
        latitude=latitude,
        longitude=longitude,
        rating=rating,
        rating_count=rating_count,
        raw_payload=raw_payload,
    )


def _stable_business_id(
    *,
    cid: str | None,
    title: str | None,
    address: str | None,
    phone: str | None,
) -> str:
    if cid:
        return f"cid:{cid}"
    normalized_parts = [
        _normalize_identity_text(title),
        _normalize_identity_text(address),
        _normalize_phone(phone),
    ]
    digest = sha1("|".join(normalized_parts).encode("utf-8")).hexdigest()
    return f"fallback:{digest}"


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_identity_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip().lower())


def _normalize_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D+", "", value)
    return digits or value.strip().lower()


def _coerce_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_error_message(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def _page_business_ids(places: list[dict[str, Any]]) -> set[str]:
    business_ids: set[str] = set()
    for place in places:
        if not isinstance(place, dict):
            continue
        normalized = normalize_place(place)
        if normalized is None:
            continue
        business_ids.add(normalized.stable_business_id)
    return business_ids


def _page_signature(places: list[dict[str, Any]]) -> str:
    payload = json.dumps(places, sort_keys=True, ensure_ascii=True)
    return sha1(payload.encode("utf-8")).hexdigest()


def _elapsed_ms(started_at: float) -> int:
    return int(round((time.perf_counter() - started_at) * 1000))
