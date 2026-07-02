from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from scraper.core import ConfigError, default_app_config, require_api_key


SERPER_STATS_URL = "https://api.serper.dev/stats/dashboard"
REQUEST_TIMEOUT_SECONDS = 30


def fetch_serper_credits(*, api_key: str) -> dict[str, Any]:
    req = request.Request(
        SERPER_STATS_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "X-API-KEY": api_key,
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ConfigError(f"Serper credits request failed with HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise ConfigError(f"Serper credits request failed: {exc.reason}") from exc

    if not isinstance(payload, dict):
        raise ConfigError("Serper credits response was not a JSON object.")
    return payload


def run_credits() -> dict[str, Any]:
    app_config = default_app_config()
    payload = fetch_serper_credits(api_key=require_api_key(app_config))
    return {
        "creditBalance": payload.get("creditBalance"),
        "endpoint": SERPER_STATS_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "serper_api_key_source": app_config.serper_api_key_source,
        "usageLastMonth": payload.get("usageLastMonth"),
        "usageToday": payload.get("usageToday"),
    }


def main() -> int:
    try:
        payload = run_credits()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
