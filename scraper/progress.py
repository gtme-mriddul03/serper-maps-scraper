from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


class ProgressLogger:
    def __init__(self, path: Path, *, stream: TextIO | None = None) -> None:
        self.path = path
        self.stream = stream if stream is not None else sys.stderr
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        with self._lock:
            self._handle.close()

    def event(self, event: str, **payload: Any) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        with self._lock:
            self._handle.write(json.dumps(row, sort_keys=True) + "\n")
            self._handle.flush()
            print(self._format(row), file=self.stream, flush=True)

    def _format(self, row: dict[str, Any]) -> str:
        event = row["event"]
        run_id = row.get("run_id", "")
        prefix = f"[{run_id}] " if run_id else ""
        if event == "run_started":
            return (
                f"{prefix}started {row.get('command')} mode={row.get('mode')} "
                f"states={row.get('state_count')} keywords={row.get('keyword_count')}"
            )
        if event == "request_done":
            return (
                f"{prefix}{row.get('status')} {row.get('state')} {row.get('keyword_id')} "
                f"{row.get('seed_type')}={row.get('seed_value')} "
                f"queries={row.get('api_request_count')} credits={row.get('estimated_credit_usage')} "
                f"results={row.get('result_count')} new={row.get('new_unique_businesses')}"
            )
        if event == "run_finished":
            return (
                f"{prefix}finished requests={row.get('total_requests')} "
                f"credits={row.get('estimated_credits')} new={row.get('new_unique_businesses_added')} "
                f"summary={row.get('summary_path')}"
            )
        return f"{prefix}{event}"
