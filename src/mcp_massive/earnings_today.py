"""Publishes today's + tomorrow's earnings schedule to a shared file for dashboards.

Output (NAS path from the host: /mnt/nas/data/code/forks/mcp_polygon/.cache/earnings_today.json,
inside the container /app/.cache/earnings_today.json). Rewritten atomically every REFRESH seconds
(5 min on weekdays 05:00-21:00 ET, 30 min otherwise).

Sources: Massive's Benzinga earnings (time of day, estimates, and actuals once reported) is
primary; the daily Alpha Vantage calendar cache (.cache/alphavantage/earnings_calendar.json,
refreshed daily by the massive seat) fills reporters Benzinga lacks (session only, no clock time).
Session is derived from the Benzinga time ET: <09:30 bmo, >=16:00 amc, between = intraday.
"""
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("mcp_massive.earnings_today")
ET = ZoneInfo("America/New_York")
OUT = os.environ.get("EARNINGS_TODAY_PATH", "/app/.cache/earnings_today.json")
AV = "/app/.cache/alphavantage/earnings_calendar.json"


def _key() -> str:
    return os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")


def _session(t: str) -> str:
    if not t:
        return "unknown"
    hm = t[:5]
    return "bmo" if hm < "09:30" else "amc" if hm >= "16:00" else "intraday"


async def _build() -> Dict[str, Any]:
    now = datetime.now(ET)
    d0, d1 = now.date().isoformat(), (now.date() + timedelta(days=1)).isoformat()
    async with httpx.AsyncClient(timeout=60.0, headers={"Authorization": f"Bearer {_key()}"}) as c:
        r = await c.get(
            "https://api.massive.com/benzinga/v1/earnings",
            params={"date.gte": d0, "date.lte": d1, "limit": 50000, "sort": "date.asc,time.asc"},
        )
        r.raise_for_status()
        bz = r.json().get("results", [])
    rows: Dict[str, Dict[str, Any]] = {}
    for x in bz:
        k = f"{x['ticker']}|{x['date']}"
        rows[k] = {
            "ticker": x["ticker"], "company": x.get("company_name"), "date": x["date"],
            "time_et": x.get("time"), "session": _session(x.get("time") or ""),
            "fiscal_period": x.get("fiscal_period"), "fiscal_year": x.get("fiscal_year"),
            "date_status": x.get("date_status"), "estimated_eps": x.get("estimated_eps"),
            "estimated_revenue": x.get("estimated_revenue"), "previous_eps": x.get("previous_eps"),
            "actual_eps": x.get("actual_eps"), "actual_revenue": x.get("actual_revenue"),
            "last_updated": x.get("last_updated"), "source": "benzinga",
        }
    try:
        av = json.load(open(AV))
        for x in av.get("rows", []):
            if x.get("reportDate") in (d0, d1):
                k = f"{x['symbol']}|{x['reportDate']}"
                if k not in rows:
                    sess = {"pre-market": "bmo", "post-market": "amc"}.get(x.get("timeOfTheDay") or "", "unknown")
                    rows[k] = {
                        "ticker": x["symbol"], "company": x.get("name"), "date": x["reportDate"],
                        "time_et": None, "session": sess, "fiscal_period": None, "fiscal_year": None,
                        "date_status": None, "estimated_eps": float(x["estimate"]) if x.get("estimate") else None,
                        "estimated_revenue": None, "previous_eps": None, "actual_eps": None,
                        "actual_revenue": None, "last_updated": None, "source": "alphavantage",
                    }
    except Exception as e:  # cache may be absent: Benzinga alone is still a valid schedule
        logger.warning("earnings_today: alphavantage cache unreadable: %s", e)
    out: List[dict] = sorted(rows.values(), key=lambda v: (v["date"], v["time_et"] or "99", v["ticker"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dates": [d0, d1],
        "note": "session bmo/amc/intraday derived from Benzinga time ET; actual_* present once reported",
        "count": len(out),
        "rows": out,
    }


def _interval(now: datetime) -> int:
    return 300 if now.weekday() < 5 and 5 <= now.hour < 21 else 1800


async def run_forever() -> None:
    while True:
        try:
            payload = await _build()
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, OUT)
        except Exception:
            logger.exception("earnings_today: refresh failed")
        await asyncio.sleep(_interval(datetime.now(ET)))
