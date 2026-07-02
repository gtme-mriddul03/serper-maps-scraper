from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .serper import NormalizedBusiness


SQLITE_BUSY_TIMEOUT_MS = 30_000
REQUEST_CLAIM_STALE_AFTER = timedelta(minutes=15)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_claim_is_stale(created_at: Any) -> bool:
    if not isinstance(created_at, str) or not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created >= REQUEST_CLAIM_STALE_AFTER


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.connection = sqlite3.connect(
            db_path,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._initialize_schema()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _initialize_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                run_kind TEXT NOT NULL,
                targeted_states_json TEXT NOT NULL,
                targeted_keyword_ids_json TEXT NOT NULL,
                command_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                summary_json TEXT
            );

            CREATE TABLE IF NOT EXISTS requests (
                fingerprint TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                keyword_id TEXT NOT NULL,
                state TEXT NOT NULL,
                seed_type TEXT NOT NULL,
                seed_value TEXT NOT NULL,
                query_text TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                latency_ms INTEGER NOT NULL,
                result_count INTEGER NOT NULL,
                new_unique_businesses INTEGER NOT NULL,
                api_request_count INTEGER NOT NULL DEFAULT 1,
                retry_count INTEGER NOT NULL,
                estimated_credit_usage INTEGER NOT NULL,
                pagination_stop_reason TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_requests_run_id ON requests(run_id);
            CREATE INDEX IF NOT EXISTS idx_requests_mode_state_keyword
                ON requests(mode, state, keyword_id);

            CREATE TABLE IF NOT EXISTS businesses (
                stable_business_id TEXT PRIMARY KEY,
                cid TEXT,
                title TEXT NOT NULL,
                address TEXT NOT NULL,
                phone TEXT,
                website TEXT,
                source_category TEXT,
                latitude REAL,
                longitude REAL,
                rating REAL,
                rating_count INTEGER,
                first_seed_type TEXT NOT NULL,
                first_seed_value TEXT NOT NULL,
                first_state TEXT NOT NULL,
                first_keyword_id TEXT NOT NULL,
                first_run_id TEXT NOT NULL,
                raw_payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_businesses_cid ON businesses(cid);

            CREATE TABLE IF NOT EXISTS business_provenance (
                stable_business_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                keyword_id TEXT NOT NULL,
                category TEXT NOT NULL,
                state TEXT NOT NULL,
                seed_type TEXT NOT NULL,
                seed_value TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (
                    stable_business_id,
                    run_id,
                    keyword_id,
                    state,
                    seed_type,
                    seed_value
                ),
                FOREIGN KEY (stable_business_id) REFERENCES businesses(stable_business_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_business_provenance_business
                ON business_provenance(stable_business_id);
            """
        )
        self._ensure_requests_columns()
        self.connection.commit()

    def _ensure_requests_columns(self) -> None:
        existing_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(requests)").fetchall()
        }
        if "api_request_count" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE requests ADD COLUMN api_request_count INTEGER NOT NULL DEFAULT 1"
            )
        if "pagination_stop_reason" not in existing_columns:
            self.connection.execute(
                "ALTER TABLE requests ADD COLUMN pagination_stop_reason TEXT"
            )

    def start_run(
        self,
        *,
        run_id: str,
        mode: str,
        run_kind: str,
        targeted_states: list[str],
        targeted_keyword_ids: list[str],
        command_payload: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO runs (
                run_id,
                mode,
                run_kind,
                targeted_states_json,
                targeted_keyword_ids_json,
                command_json,
                started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                mode,
                run_kind,
                json.dumps(targeted_states),
                json.dumps(targeted_keyword_ids),
                json.dumps(command_payload, sort_keys=True),
                _utc_now(),
            ),
        )
        self.connection.commit()

    def finish_run(self, run_id: str, summary_payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            UPDATE runs
            SET completed_at = ?, summary_json = ?
            WHERE run_id = ?
            """,
            (_utc_now(), json.dumps(summary_payload, sort_keys=True), run_id),
        )
        self.connection.commit()

    def fetch_request(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM requests WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_request(
        self,
        *,
        fingerprint: str,
        run_id: str,
        mode: str,
        keyword_id: str,
        state: str,
        seed_type: str,
        seed_value: str,
        query_text: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        inserted = self.connection.execute(
            """
            INSERT OR IGNORE INTO requests (
                fingerprint,
                run_id,
                mode,
                keyword_id,
                state,
                seed_type,
                seed_value,
                query_text,
                status,
                http_status,
                latency_ms,
                result_count,
                new_unique_businesses,
                api_request_count,
                retry_count,
                estimated_credit_usage,
                pagination_stop_reason,
                error_message,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fingerprint,
                run_id,
                mode,
                keyword_id,
                state,
                seed_type,
                seed_value,
                query_text,
                "in_progress",
                None,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                _utc_now(),
            ),
        ).rowcount == 1
        if inserted:
            self.connection.commit()
            return True, None

        existing = self.fetch_request(fingerprint)
        if existing is None:
            raise RuntimeError("Request claim conflicted but no existing request row could be loaded.")
        should_reclaim = existing["status"] == "error" or (
            existing["status"] == "in_progress"
            and _request_claim_is_stale(existing.get("created_at"))
        )
        if should_reclaim:
            reclaimed = self.connection.execute(
                """
                UPDATE requests
                SET
                    run_id = ?,
                    mode = ?,
                    keyword_id = ?,
                    state = ?,
                    seed_type = ?,
                    seed_value = ?,
                    query_text = ?,
                    status = ?,
                    http_status = NULL,
                    latency_ms = 0,
                    result_count = 0,
                    new_unique_businesses = 0,
                    api_request_count = 0,
                    retry_count = 0,
                    estimated_credit_usage = 0,
                    pagination_stop_reason = NULL,
                    error_message = NULL,
                    created_at = ?
                WHERE fingerprint = ? AND (
                    status = 'error' OR (status = 'in_progress' AND created_at = ?)
                )
                """,
                (
                    run_id,
                    mode,
                    keyword_id,
                    state,
                    seed_type,
                    seed_value,
                    query_text,
                    "in_progress",
                    _utc_now(),
                    fingerprint,
                    existing["created_at"],
                ),
            ).rowcount == 1
            if reclaimed:
                self.connection.commit()
                return True, None
            existing = self.fetch_request(fingerprint)
            if existing is None:
                raise RuntimeError("Request reclaim succeeded unexpectedly without an existing request row.")

        self.connection.commit()
        return False, existing

    def record_request(self, payload: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO requests (
                fingerprint,
                run_id,
                mode,
                keyword_id,
                state,
                seed_type,
                seed_value,
                query_text,
                status,
                http_status,
                latency_ms,
                result_count,
                new_unique_businesses,
                api_request_count,
                retry_count,
                estimated_credit_usage,
                pagination_stop_reason,
                error_message,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                run_id = excluded.run_id,
                status = excluded.status,
                http_status = excluded.http_status,
                latency_ms = excluded.latency_ms,
                result_count = excluded.result_count,
                new_unique_businesses = excluded.new_unique_businesses,
                api_request_count = excluded.api_request_count,
                retry_count = excluded.retry_count,
                estimated_credit_usage = excluded.estimated_credit_usage,
                pagination_stop_reason = excluded.pagination_stop_reason,
                error_message = excluded.error_message,
                created_at = excluded.created_at
            """,
            (
                payload["fingerprint"],
                payload["run_id"],
                payload["mode"],
                payload["keyword_id"],
                payload["state"],
                payload["seed_type"],
                payload["seed_value"],
                payload["query_text"],
                payload["status"],
                payload.get("http_status"),
                payload["latency_ms"],
                payload["result_count"],
                payload["new_unique_businesses"],
                payload["api_request_count"],
                payload["retry_count"],
                payload["estimated_credit_usage"],
                payload.get("pagination_stop_reason"),
                payload.get("error_message"),
                _utc_now(),
            ),
        )

    def upsert_business(
        self,
        *,
        business: NormalizedBusiness,
        run_id: str,
        keyword_id: str,
        category: str,
        state: str,
        seed_type: str,
        seed_value: str,
        request_fingerprint: str,
    ) -> bool:
        now = _utc_now()
        insert_cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO businesses (
                stable_business_id,
                cid,
                title,
                address,
                phone,
                website,
                source_category,
                latitude,
                longitude,
                rating,
                rating_count,
                first_seed_type,
                first_seed_value,
                first_state,
                first_keyword_id,
                first_run_id,
                raw_payload,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business.stable_business_id,
                business.cid,
                business.title,
                business.address,
                business.phone,
                business.website,
                business.source_category,
                business.latitude,
                business.longitude,
                business.rating,
                business.rating_count,
                seed_type,
                seed_value,
                state,
                keyword_id,
                run_id,
                business.raw_payload,
                now,
                now,
            ),
        )
        inserted_new = insert_cursor.rowcount == 1

        if not inserted_new:
            existing = self.connection.execute(
                "SELECT * FROM businesses WHERE stable_business_id = ?",
                (business.stable_business_id,),
            ).fetchone()
            if existing is None:
                raise RuntimeError(
                    "Business upsert could not reload an existing row after INSERT OR IGNORE."
                )
            merged = {
                "cid": existing["cid"] or business.cid,
                "title": existing["title"] or business.title,
                "address": existing["address"] or business.address,
                "phone": existing["phone"] or business.phone,
                "website": existing["website"] or business.website,
                "source_category": existing["source_category"] or business.source_category,
                "latitude": existing["latitude"] if existing["latitude"] is not None else business.latitude,
                "longitude": existing["longitude"] if existing["longitude"] is not None else business.longitude,
                "rating": existing["rating"] if existing["rating"] is not None else business.rating,
                "rating_count": existing["rating_count"]
                if existing["rating_count"] is not None
                else business.rating_count,
                "raw_payload": existing["raw_payload"] or business.raw_payload,
            }
            self.connection.execute(
                """
                UPDATE businesses
                SET
                    cid = ?,
                    title = ?,
                    address = ?,
                    phone = ?,
                    website = ?,
                    source_category = ?,
                    latitude = ?,
                    longitude = ?,
                    rating = ?,
                    rating_count = ?,
                    raw_payload = ?,
                    updated_at = ?
                WHERE stable_business_id = ?
                """,
                (
                    merged["cid"],
                    merged["title"],
                    merged["address"],
                    merged["phone"],
                    merged["website"],
                    merged["source_category"],
                    merged["latitude"],
                    merged["longitude"],
                    merged["rating"],
                    merged["rating_count"],
                    merged["raw_payload"],
                    now,
                    business.stable_business_id,
                ),
            )
            inserted_new = False

        self.connection.execute(
            """
            INSERT OR IGNORE INTO business_provenance (
                stable_business_id,
                run_id,
                keyword_id,
                category,
                state,
                seed_type,
                seed_value,
                request_fingerprint,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business.stable_business_id,
                run_id,
                keyword_id,
                category,
                state,
                seed_type,
                seed_value,
                request_fingerprint,
                now,
            ),
        )
        return inserted_new

    def fetch_run_request_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM requests
            WHERE run_id = ?
            ORDER BY created_at, state, keyword_id, seed_type, seed_value
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def fetch_all_businesses(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM businesses
            ORDER BY stable_business_id, first_state, title COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def fetch_all_business_provenance(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT *
            FROM business_provenance
            ORDER BY stable_business_id, state, keyword_id, seed_type, seed_value, run_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
