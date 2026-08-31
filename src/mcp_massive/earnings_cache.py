"""Shared daily-refreshed cache of AlphaVantage's whole-market EARNINGS_CALENDAR.

AlphaVantage's free tier caps at 25 requests/day fleet-wide — shared across every
agent that might want an earnings date. A single no-symbol EARNINGS_CALENDAR call
already returns the whole market in one request (confirmed 2026-08-31: 1,589
tickers for horizon=3month), so there is no reason for each caller to spend their
own quota re-fetching it. A daily scheduled job (not this module — see the
`polygon`/`massive` seat's ATC schedule) is the only thing that calls AlphaVantage;
this module just reads what that job wrote.

The writer runs OUTSIDE the container (an agent session calling the AlphaVantage
MCP tool, then writing this file to the NAS-shared repo path) — this module is
read-only by design, matching the flatfile cache's separation between what
populates a cache and what serves it.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_cache_path() -> Path:
    cache_dir = Path(os.environ.get("EARNINGS_CALENDAR_CACHE_DIR", "/app/.cache/alphavantage"))
    return cache_dir / "earnings_calendar.json"


def read_cache() -> Dict[str, Any]:
    """Read the cached whole-market earnings calendar.

    Returns a dict with `fetched_at`, `horizon`, `row_count`, `age_hours`, `stale`,
    and `rows`. `stale` is true past 36h since the daily job refreshes once a day
    and a missed run shouldn't be silently treated as current.
    """
    path = get_cache_path()
    if not path.exists():
        return {
            "error": f"No cache at {path} — the daily refresh job hasn't populated it yet.",
        }

    with path.open() as f:
        data = json.load(f)

    fetched_at = datetime.fromisoformat(data["fetched_at"])
    age_hours = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 3600.0

    return {
        "fetched_at": data["fetched_at"],
        "horizon": data.get("horizon"),
        "row_count": len(data.get("rows", [])),
        "age_hours": round(age_hours, 1),
        "stale": age_hours > 36,
        "rows": data.get("rows", []),
    }


def get_earnings_calendar(ticker: Optional[str] = None) -> Dict[str, Any]:
    """Read the cached calendar, optionally filtered to one ticker."""
    result = read_cache()
    if "error" in result:
        return result

    if ticker:
        ticker_upper = ticker.upper()
        result = dict(result)
        result["rows"] = [r for r in result["rows"] if r.get("symbol", "").upper() == ticker_upper]
        result["row_count"] = len(result["rows"])

    return result


def write_cache(rows: List[Dict[str, Any]], horizon: str) -> Path:
    """Write the cache file. Called by the daily refresh job, not by the MCP server."""
    path = get_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "horizon": horizon,
        "rows": rows,
    }
    path.write_text(json.dumps(payload))
    return path
