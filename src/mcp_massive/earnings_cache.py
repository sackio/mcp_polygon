"""Shared daily-refreshed cache of AlphaVantage's whole-market EARNINGS_CALENDAR.

AlphaVantage's free tier caps at 25 requests/day fleet-wide — shared across every
agent that might want an earnings date. A single no-symbol EARNINGS_CALENDAR call
already returns the whole market in one request (confirmed 2026-08-31: 1,589
tickers for horizon=3month), so there is no reason for each caller to spend their
own quota re-fetching it. A daily scheduled job (not this module — see the
`polygon`/`massive` seat's ATC schedule) is the only thing that calls AlphaVantage;
this module just reads what that job wrote.

The writer runs OUTSIDE the container (an agent session calling the AlphaVantage
MCP tool, then writing these files to the NAS-shared repo path) — this module is
read-only by design, matching the flatfile cache's separation between what
populates a cache and what serves it.

⛔ POINT-IN-TIME NOTE (added 2026-08-31, same day as the first version — this
was NOT point-in-time safe on day one, and a vbt/lookahead-detection question
is why it got fixed the same day): `latest.json` is overwritten daily and only
ever answers "what does the calendar say right now" — never usable for
backtesting, since a forward calendar's own values change as reality gets
closer (a `reportDate`/`estimate` guessed today can differ from what the same
row said a week ago, and this module cannot tell you what it said a week ago
from `latest.json` alone). `snapshots/YYYY-MM-DD.json` fixes this GOING FORWARD
ONLY — each day's pull is preserved permanently, never overwritten, so from
2026-08-31 onward a caller can ask "what did the calendar say as of date X" and
get the honest answer. There is no way to recover snapshots from before this
was added; anyone needing pre-2026-08-31 point-in-time earnings-calendar state
does not have it from this cache, full stop — don't reconstruct it from
`latest.json` history.
"""

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_cache_dir() -> Path:
    cache_dir = Path(os.environ.get("EARNINGS_CALENDAR_CACHE_DIR", "/app/.cache/alphavantage"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_latest_path() -> Path:
    return get_cache_dir() / "earnings_calendar.json"


def get_snapshot_dir() -> Path:
    snap_dir = get_cache_dir() / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    return snap_dir


def _load(path: Path) -> Dict[str, Any]:
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


def _resolve_snapshot(as_of: str) -> Optional[Path]:
    """Find the snapshot dated on or most recently before `as_of` (YYYY-MM-DD)."""
    target = date.fromisoformat(as_of)
    candidates = []
    for p in get_snapshot_dir().glob("*.json"):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if d <= target:
            candidates.append((d, p))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def get_earnings_calendar(ticker: Optional[str] = None, as_of: Optional[str] = None) -> Dict[str, Any]:
    """Read the cached calendar, optionally filtered to one ticker and/or pinned
    to a historical snapshot date. `as_of` resolves to the nearest snapshot on or
    before that date, from snapshots/ (present from 2026-08-31 onward only) — the
    response's `snapshot_date` says exactly which one was used, or an error if
    none exist that early."""
    if as_of:
        snap_path = _resolve_snapshot(as_of)
        if snap_path is None:
            return {
                "error": (
                    f"No snapshot on or before {as_of} — snapshots only exist from "
                    "2026-08-31 onward (when point-in-time capture was added). "
                    "Earlier dates have no recoverable earnings-calendar state."
                )
            }
        result = _load(snap_path)
        result["snapshot_date"] = snap_path.stem
        result["stale"] = False  # historical reads are never "stale", they're pinned on purpose
    else:
        path = get_latest_path()
        if not path.exists():
            return {"error": f"No cache at {path} — the daily refresh job hasn't populated it yet."}
        result = _load(path)
        result["snapshot_date"] = None

    if ticker:
        ticker_upper = ticker.upper()
        result = dict(result)
        result["rows"] = [r for r in result["rows"] if r.get("symbol", "").upper() == ticker_upper]
        result["row_count"] = len(result["rows"])

    return result


def write_cache(rows: List[Dict[str, Any]], horizon: str) -> Dict[str, Path]:
    """Write both the overwritten `latest.json` and a permanent dated snapshot.
    Called by the daily refresh job, not by the MCP server."""
    now = datetime.now(timezone.utc)
    payload = {
        "fetched_at": now.isoformat(),
        "horizon": horizon,
        "rows": rows,
    }
    body = json.dumps(payload)

    latest_path = get_latest_path()
    latest_path.write_text(body)

    snapshot_path = get_snapshot_dir() / f"{now.date().isoformat()}.json"
    snapshot_path.write_text(body)

    return {"latest": latest_path, "snapshot": snapshot_path}
