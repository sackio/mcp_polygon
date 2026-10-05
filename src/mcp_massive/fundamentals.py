"""Company financial statements from Massive's /stocks/financials/v1/* endpoints.

Replaces the dead vX /reference/financials (HTTP 410, sunset 2026-10-09). Three statement
endpoints: income-statements, balance-sheets, cash-flow-statements. Flat records, XBRL-sourced.

Traps (measured 2026-09/10, see the massive-fundamentals skill):
  * `ticker=` (singular) is silently IGNORED and returns an arbitrary universe: the working
    filters are `tickers` / `tickers.any_of` and `cik`. This module maps `ticker` to
    `tickers.any_of` for you.
  * `filing_date` is the MOST RECENT filing that included the period (a later restatement),
    not the original filing date: not a point-in-time clock.
  * Data starts with XBRL (period_end ~2009-2011+); quarterly Q4 is derived from annual.
"""
import os
from typing import Any, Dict, List, Optional

import httpx

BASE = "https://api.massive.com"
STATEMENTS = {
    "income_statement": "income-statements",
    "balance_sheet": "balance-sheets",
    "cash_flow": "cash-flow-statements",
}
MAX_PAGES = 40


def _key() -> str:
    return os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")


async def _fetch(endpoint: str, params: Dict[str, Any], limit: int) -> Dict[str, Any]:
    url = f"{BASE}/stocks/financials/v1/{endpoint}"
    q = dict(params)
    rows: List[dict] = []
    pages = 0
    # key goes in a header, never the URL: httpx/rich logs full request URLs at INFO
    async with httpx.AsyncClient(timeout=60.0, headers={"Authorization": f"Bearer {_key()}"}) as c:
        while True:
            r = await c.get(url, params=q)
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}: {r.text[:200]}", "results": rows}
            j = r.json()
            rows += j.get("results", [])
            pages += 1
            nxt = j.get("next_url")
            if not nxt or len(rows) >= limit or pages >= MAX_PAGES:
                break
            url, q = nxt, {}
    return {"results": rows[:limit], "truncated": bool(nxt) and len(rows) >= limit}


async def list_financials(
    statement: str = "all",
    ticker: Optional[str] = None,
    tickers: Optional[str] = None,
    cik: Optional[str] = None,
    timeframe: Optional[str] = None,
    fiscal_year: Optional[int] = None,
    fiscal_quarter: Optional[int] = None,
    filters: Optional[Dict[str, Any]] = None,
    limit: int = 100,
    sort: Optional[str] = None,
) -> Dict[str, Any]:
    names = list(STATEMENTS) if statement == "all" else [statement]
    bad = [n for n in names if n not in STATEMENTS]
    if bad:
        return {"error": f"statement must be one of {list(STATEMENTS)} or 'all'"}
    p: Dict[str, Any] = {}
    t = tickers or ticker
    if t:
        p["tickers.any_of" if "," in t else "tickers"] = t
    for k, v in (("cik", cik), ("timeframe", timeframe), ("fiscal_year", fiscal_year),
                 ("fiscal_quarter", fiscal_quarter), ("sort", sort)):
        if v is not None:
            p[k] = v
    for k, v in (filters or {}).items():
        if v is not None:
            p[k] = str(v)[:10] if k.startswith(("period_end", "filing_date")) else v
    limit = max(1, min(int(limit), 50000))
    p["limit"] = min(limit, 50000)
    out: Dict[str, Any] = {"source": "/stocks/financials/v1/*", "statements": {}}
    for n in names:
        out["statements"][n] = await _fetch(STATEMENTS[n], p, limit)
    if statement != "all":
        out.update(out["statements"].pop(statement))
        out["statement"] = statement
        del out["statements"]
    return out
