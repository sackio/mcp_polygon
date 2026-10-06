"""Daily incremental refresh of the data/refdata parquets that other seats read in place.

Runs inside the MCP container as a background task (no LLM). Twice a day, 06:30 and 17:10 ET,
Mon-Sun (idempotent: each step only fetches what is newer than the file already holds):
  stock_day_aggs_unadj.parquet  new sessions from /v2/aggs/grouped (adjusted=false), src='grouped'
  splits.parquet                full re-pull (~28k rows)
  benzinga_earnings_full / benzinga_guidance_full / benzinga_ratings_full   upsert last N days
  sec_filings_index_2010        upsert last 7 days (Form 4, 8-K, 10-Q, 10-K)
  short_interest_all            settlement dates newer than the file's max
  dividends_all                 upsert last 30 days of ex-dates + declared future ones
  financials_bulk_v1            weekly full re-pull (Saturday run)
Files are replaced atomically (os.replace) so readers never see a partial parquet.
Status of the last run: /app/.cache/refdata_refresh_status.json
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

logger = logging.getLogger("mcp_massive.refdata_refresh")
ET = ZoneInfo("America/New_York")
REF = os.environ.get("REFDATA_DIR", "/app/data/refdata")
STATUS = "/app/.cache/refdata_refresh_status.json"
BASE = "https://api.massive.com"
RUN_TIMES = ((6, 30), (17, 10))


def _key() -> str:
    return os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")


def _client() -> httpx.Client:
    return httpx.Client(timeout=180.0, headers={"Authorization": f"Bearer {_key()}"})


def _get(c: httpx.Client, url: str, params=None) -> dict:
    for a in range(6):
        r = c.get(url, params=params)
        if r.status_code == 429:
            time.sleep(3 + 3 * a)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited: " + url)


def _pull(c: httpx.Client, path: str, params: dict) -> list:
    rows, url, p = [], BASE + path, dict(params)
    while url:
        j = _get(c, url, p)
        rows += j.get("results", [])
        url, p = j.get("next_url"), None
    return rows


def _atomic(df: pd.DataFrame, name: str) -> None:
    dst = os.path.join(REF, name)
    tmp = dst + ".tmp"
    df.to_parquet(tmp, index=False)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dst)


def _upsert(name: str, new: pd.DataFrame, key: list, since_col: str = None, since: str = None) -> int:
    old = pd.read_parquet(os.path.join(REF, name))
    if since_col and since:
        keep = old[old[since_col].astype(str) < since]
    else:
        keep = old
    out = pd.concat([keep, new], ignore_index=True)
    out = out.drop_duplicates(key, keep="last")
    sort_cols = [c for c in ("date", "filing_date", "settlement_date", "ex_dividend_date") if c in out.columns][:1]
    if sort_cols:
        out = out.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    _atomic(out, name)
    return len(new)


def _trading_days_missing(last: str) -> list:
    import pandas_market_calendars as mcal
    now = datetime.now(ET)
    end = now.date() if now.hour >= 17 else (now.date() - timedelta(days=1))
    sched = mcal.get_calendar("NYSE").valid_days(
        start_date=(datetime.fromisoformat(last) + timedelta(days=1)).date().isoformat(),
        end_date=end.isoformat())
    return [d.strftime("%Y-%m-%d") for d in sched]


def _day_aggs(c: httpx.Client) -> str:
    name = "stock_day_aggs_unadj.parquet"
    path = os.path.join(REF, name)
    last = pc.max(pq.read_table(path, columns=["date"])["date"]).as_py()
    days = _trading_days_missing(last)
    if not days:
        return f"day_aggs up to date ({last})"
    frames = []
    for d in days:
        j = _get(c, f"{BASE}/v2/aggs/grouped/locale/us/market/stocks/{d}", {"adjusted": "false"})
        r = j.get("results", [])
        if not r:
            continue
        df = pd.DataFrame(r).rename(columns={"T": "ticker", "o": "o", "h": "h", "l": "l", "c": "c", "v": "v", "n": "transactions"})
        df["date"] = d
        frames.append(df[["date", "ticker", "o", "h", "l", "c", "v", "transactions"]])
    if not frames:
        return f"day_aggs: no grouped data yet for {days}"
    new = pd.concat(frames)
    new["transactions"] = new["transactions"].fillna(0).astype("int64")
    new["v"] = new["v"].astype("float64")
    pf = pq.ParquetFile(path)
    sch = pf.schema_arrow
    tmp = path + ".tmp"
    with pq.ParquetWriter(tmp, sch, compression="zstd") as w:
        for b in pf.iter_batches(batch_size=2_000_000):
            w.write_table(pa.Table.from_batches([b], schema=sch))
        w.write_table(pa.Table.from_pandas(new, schema=sch, preserve_index=False))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return f"day_aggs +{len(new)} rows for {days[0]}..{days[-1]}"


def _benzinga(c: httpx.Client, ep: str, name: str, key: list, days: int, datecol="date") -> str:
    since = (datetime.now(ET).date() - timedelta(days=days)).isoformat()
    rows = _pull(c, f"/benzinga/v1/{ep}", {"limit": 50000, f"{datecol}.gte": since, "sort": f"{datecol}.asc"})
    if not rows:
        return f"{ep}: none"
    n = _upsert(name, pd.DataFrame(rows), key, datecol, since)
    return f"{ep} upsert {n} rows since {since}"


def _filings(c: httpx.Client) -> str:
    since = (datetime.now(ET).date() - timedelta(days=7)).isoformat()
    frames = []
    for ft in ("10-K", "10-Q", "8-K", "4"):
        rows = _pull(c, "/stocks/filings/vX/index", {"form_type": ft, "filing_date.gte": since, "limit": 50000})
        if rows:
            frames.append(pd.DataFrame(rows))
    if not frames:
        return "filings: none"
    n = _upsert("sec_filings_index_2010.parquet", pd.concat(frames), ["accession_number", "form_type", "cik"], "filing_date", since)
    return f"filings upsert {n} since {since}"


def _short_interest(c: httpx.Client) -> str:
    path = os.path.join(REF, "short_interest_all.parquet")
    last = pc.max(pq.read_table(path, columns=["settlement_date"])["settlement_date"]).as_py()
    rows = _pull(c, "/stocks/v1/short-interest", {"limit": 50000, "settlement_date.gt": last, "sort": "settlement_date.asc"})
    if not rows:
        return f"short_interest up to date ({last})"
    old = pd.read_parquet(path)
    out = pd.concat([old, pd.DataFrame(rows)], ignore_index=True).drop_duplicates(["ticker", "settlement_date"], keep="last")
    _atomic(out, "short_interest_all.parquet")
    return f"short_interest +{len(rows)} rows after {last}"


def _dividends(c: httpx.Client) -> str:
    since = (datetime.now(ET).date() - timedelta(days=30)).isoformat()
    rows = _pull(c, "/v3/reference/dividends", {"limit": 1000, "ex_dividend_date.gte": since, "order": "asc", "sort": "ex_dividend_date"})
    if not rows:
        return "dividends: none"
    n = _upsert("dividends_all.parquet", pd.DataFrame(rows), ["id"], "ex_dividend_date", since)
    return f"dividends upsert {n} since {since}"


def _splits(c: httpx.Client) -> str:
    rows = _pull(c, "/v3/reference/splits", {"limit": 1000, "order": "asc", "sort": "execution_date"})
    if len(rows) < 20000:
        return f"splits: suspicious count {len(rows)}, not written"
    df = pd.DataFrame(rows)[["ticker", "execution_date", "split_from", "split_to", "id"]]
    _atomic(df, "splits.parquet")
    return f"splits rewritten {len(df)}"


def _financials(c: httpx.Client) -> str:
    K3 = ["cik", "period_end", "timeframe"]
    out = []
    for tf in ("quarterly", "annual"):
        t = {}
        for ep in ("income-statements", "balance-sheets", "cash-flow-statements"):
            d = pd.DataFrame(_pull(c, f"/stocks/financials/v1/{ep}", {"timeframe": tf, "limit": 50000, "sort": "period_end.asc"}))
            d["ticker"] = d["tickers"].map(lambda x: ",".join(x) if isinstance(x, list) else x)
            t[ep] = d.drop(columns=["tickers"]).drop_duplicates(K3)
        i, b, cf = t["income-statements"], t["balance-sheets"], t["cash-flow-statements"]
        ic = [x for x in ["revenue", "operating_income", "consolidated_net_income_loss", "net_income_loss_attributable_common_shareholders", "basic_earnings_per_share", "diluted_earnings_per_share", "basic_shares_outstanding", "diluted_shares_outstanding", "ebitda"] if x in i]
        bc = [x for x in ["total_assets", "total_liabilities", "total_equity", "total_equity_attributable_to_parent", "total_current_assets", "total_current_liabilities", "cash_and_equivalents"] if x in b]
        cc = [x for x in ["net_cash_from_operating_activities", "dividends"] if x in cf]
        m = (i[K3 + ["ticker", "filing_date", "fiscal_year", "fiscal_quarter"] + ic]
             .merge(b[K3 + bc + ["filing_date"]].rename(columns={"filing_date": "filing_date_bs"}), on=K3, how="outer")
             .merge(cf[K3 + cc + ["filing_date"]].rename(columns={"filing_date": "filing_date_cf"}), on=K3, how="outer"))
        out.append(m)
    d = pd.concat(out)
    if len(d) < 200000:
        return f"financials: suspicious count {len(d)}, not written"
    _atomic(d, "financials_bulk_v1.parquet")
    return f"financials rewritten {len(d)}"


def run_once() -> dict:
    res = {}
    steps = [
        ("day_aggs", _day_aggs),
        ("splits", _splits),
        ("benzinga_earnings", lambda c: _benzinga(c, "earnings", "benzinga_earnings_full.parquet", ["benzinga_id"], 45)),
        ("benzinga_guidance", lambda c: _benzinga(c, "guidance", "benzinga_guidance_full.parquet", ["benzinga_id"], 45)),
        ("benzinga_ratings", lambda c: _benzinga(c, "ratings", "benzinga_ratings_full.parquet", ["benzinga_id"], 10)),
        ("filings", _filings),
        ("short_interest", _short_interest),
        ("dividends", _dividends),
    ]
    if datetime.now(ET).weekday() == 5:
        steps.append(("financials", _financials))
    with _client() as c:
        for name, fn in steps:
            t0 = time.time()
            try:
                res[name] = {"ok": True, "msg": fn(c), "s": round(time.time() - t0, 1)}
            except Exception as e:
                logger.exception("refdata_refresh %s failed", name)
                res[name] = {"ok": False, "msg": str(e)[:300], "s": round(time.time() - t0, 1)}
    res["_finished_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(STATUS, "w") as f:
            json.dump(res, f)
        os.chmod(STATUS, 0o644)
    except Exception:
        pass
    return res


def _next_run(now: datetime) -> datetime:
    cands = []
    for d in (0, 1):
        for h, m in RUN_TIMES:
            t = (now + timedelta(days=d)).replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                cands.append(t)
    return min(cands)


async def run_forever() -> None:
    while True:
        now = datetime.now(ET)
        nxt = _next_run(now)
        await asyncio.sleep(max(1.0, (nxt - now).total_seconds()))
        try:
            res = await asyncio.to_thread(run_once)
            logger.info("refdata_refresh done: %s", {k: (v["ok"] if isinstance(v, dict) else v) for k, v in res.items()})
        except Exception:
            logger.exception("refdata_refresh crashed")
