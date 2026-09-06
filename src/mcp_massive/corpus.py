"""Read-only access to quantum-data's flatfile corpus on the NAS/server5, in its
three forms: SORTED (ts-ordered, per-day), PIVOT (per-ticker), and RAW (vendor
bytes, unsorted).

This is a DIFFERENT corpus from flatfiles.py: flatfiles.py downloads directly from
Massive's S3. This module reads quantum-data's already-ingested corpus, built and
owned by the quantum-feed pipeline.

⛔ SORTED and PIVOT path construction is NOT re-implemented here. Both are imported
from quantum-feed's own single source of truth (`qfdata.paths.replay_day` /
`qfdata.paths.zticker_partition`) per quantum-data's directive 2026-08-24 — a second
path builder is the exact defect quantum-feed's spec-030 preservation gate exists to
catch. RAW has no canonical builder in qfdata.paths as of 2026-08-24 (checked); its
layout below is transcribed from quantum-data's direct description and spot-checked
against real files on disk — switch to a canonical builder if/when quantum-feed adds
one.

Reference: /mnt/nas/data/code/quantum-feed/specs/reference/sorted-corpus-replay-reference.md
(HEAD ec108bbd9, 2026-08-24) — read that before changing this file's tool surface.
That doc covers SORTED only; PIVOT and RAW are documented nowhere but this file and
quantum-data's 2026-08-24 messages to this seat.

⛔ A single day of a large lane (e.g. us_stocks_sip/quotes_v1) can be 10GB / 419M
rows. Every read here is bounded to one row group and a caller-supplied column
projection — there is no "read the whole file" tool, deliberately.
"""
import os
import sys
from datetime import date as _date, timedelta as _timedelta
from typing import Any, Dict, List, Optional

_QFDATA_TOOLS_DIR = "/mnt/nas/data/code/quantum-feed/tools/data"

# RAW master (vendor bytes, unsorted, 22TB). Lives on server5's local ZFS pool,
# NFS-exported and mounted on server4 (where this container runs) at this path —
# confirmed via server4's /proc/mounts 2026-08-24. NOT the same path other hosts
# would see (each mounts server5's export under its own local name), but that's
# fine: this container runs only on server4 and reads the file itself server-side —
# it never hands a raw filesystem path to a caller expecting to open it locally.
_RAW_ROOT = "/mnt/server5/zpolygon"

# DECLARED, from quantum-feed's sorted-corpus-replay-reference.md §1 (measured
# 2026-08-24). The SORTED lane set is CLOSED by operator ruling — quantum-feed
# offers 33 candidate lanes and only these 8 are taken in sorted form. Re-verify
# against that doc if this list is ever suspected stale; it is not re-derived here
# on every call.
LANES = [
    {"cluster": "us_stocks_sip", "lane": "trades_v1", "span": ["2003-09-10", "2026-08-21"], "days": 5774, "rows": 245160141275},
    {"cluster": "us_stocks_sip", "lane": "quotes_v1", "span": ["2003-09-10", "2026-08-21"], "days": 5774, "rows": 977908649360},
    {"cluster": "us_stocks_sip", "lane": "minute_aggs_v1", "span": ["2003-09-10", "2026-08-21"], "days": 5774, "rows": 7512166590},
    {"cluster": "us_indices", "lane": "values_v1", "span": ["2023-02-14", "2026-08-21"], "days": 919, "rows": 192428746503},
    {"cluster": "us_indices", "lane": "minute_aggs_v1", "span": ["2003-09-10", "2026-08-21"], "days": 5810, "rows": 5357408169},
    {"cluster": "global_crypto", "lane": "trades_v1", "span": ["2017-10-02", "2026-08-23"], "days": 3248, "rows": 9658511222},
    {"cluster": "global_crypto", "lane": "minute_aggs_v1", "span": ["2013-11-04", "2026-08-23"], "days": 4670, "rows": 468593016},
    {"cluster": "benzinga_news_v1", "lane": "news_v1", "span": ["2009-01-01", "2026-08-20"], "days": 6441, "rows": 3553538},
]

_LANES_BASELINE_MEASURED_AT = "2026-08-24"


def _find_latest_available_date(cluster: str, lane: str, lookback_days: int = 45) -> Optional[str]:
    """Scan backward from today for the most recent day-file that exists, using
    the canonical path builder (no second path implementation, per the module
    docstring's rule). Cheap — os.path.exists only, no data read — so this is
    safe to run on every list_corpus_lanes call, unlike re-deriving day/row counts.

    Added 2026-09-06 after LANES' hardcoded `span` end dates were mistaken for
    live coverage (they were 2 weeks stale) and reported to two consumers as a
    real ingestion stall that did not exist — quantum-data caught it by checking
    the actual files. This function exists so that mistake can't recur silently:
    `span[1]` in list_corpus_lanes is now live-verified every call."""
    today = _date.today()
    for i in range(lookback_days):
        d = (today - _timedelta(days=i)).isoformat()
        try:
            path = _qf_paths().replay_day(cluster, lane, d)
        except Exception:
            continue
        if os.path.exists(path):
            return d
    return None


def get_lanes_with_live_span() -> List[Dict[str, Any]]:
    """LANES with each lane's span END date replaced by a live filesystem check.
    `days`/`rows` stay as the static baseline from _LANES_BASELINE_MEASURED_AT —
    recomputing those exactly would mean reading every file. `span[0]` (start)
    also stays static; corpora only grow forward, so that end doesn't go stale
    the way the live end did."""
    lanes = []
    for lane in LANES:
        live_end = _find_latest_available_date(lane["cluster"], lane["lane"])
        entry = dict(lane)
        entry["span"] = [lane["span"][0], live_end or lane["span"][1]]
        entry["end_live_verified"] = live_end is not None
        entry["days_rows_baseline_measured_at"] = _LANES_BASELINE_MEASURED_AT
        lanes.append(entry)
    return lanes


# RAW-only lanes — no sorted or pivot counterpart, per quantum-data 2026-08-24. All
# four stopped 2026-06-02.
RAW_ONLY_LANES = [
    {"cluster": "us_options_opra", "lane": "day_aggs_v1"},
    {"cluster": "us_options_opra", "lane": "minute_aggs_v1"},
    {"cluster": "us_options_opra", "lane": "trades_v1"},
    {"cluster": "us_indices", "lane": "day_aggs_v1"},
]

# Order-key column per lane, and its timestamp unit — DECLARED/MEASURED in the same
# reference doc §3, §3.1, for SORTED lanes. benzinga is the one exception:
# milliseconds, not nanoseconds. Unknown for RAW-only lanes (not documented anywhere
# — omitted rather than guessed).
ORDER_KEYS = {
    ("us_stocks_sip", "trades_v1"): ("sip_timestamp", "ns"),
    ("us_stocks_sip", "quotes_v1"): ("sip_timestamp", "ns"),
    ("us_stocks_sip", "minute_aggs_v1"): ("window_start", "ns"),
    ("us_indices", "values_v1"): ("timestamp", "ns"),
    ("us_indices", "minute_aggs_v1"): ("window_start", "ns"),
    ("global_crypto", "trades_v1"): ("participant_timestamp", "ns"),
    ("global_crypto", "minute_aggs_v1"): ("window_start", "ns"),
    ("benzinga_news_v1", "news_v1"): ("published_at", "ms"),
}


def _split_date(date: str):
    parts = date.split("-")
    if len(parts) != 3 or [len(p) for p in parts] != [4, 2, 2] or not all(p.isdigit() for p in parts):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
    return parts[0], parts[1], parts[2]


def _qf_paths():
    """Import quantum-feed's canonical path builder. Requires the NAS mount, which
    on the deployed container is bind-mounted read-only at the same absolute path."""
    if _QFDATA_TOOLS_DIR not in sys.path:
        sys.path.insert(0, _QFDATA_TOOLS_DIR)
    from qfdata import paths as qf_paths  # noqa: PLC0415
    return qf_paths


def raw_day(cluster: str, lane: str, date: str) -> str:
    """RAW vendor-file path: `{RAW_ROOT}/{cluster}/{lane}/YYYY/MM/YYYY-MM-DD.parquet`,
    except benzinga_news_v1, which has no `lane` component and is year-first:
    `{RAW_ROOT}/benzinga_news_v1/YYYY/MM/YYYY-MM-DD.parquet`."""
    y, m, _d = _split_date(date)
    if cluster == "benzinga_news_v1":
        return f"{_RAW_ROOT}/benzinga_news_v1/{y}/{m}/{date}.parquet"
    return f"{_RAW_ROOT}/{cluster}/{lane}/{y}/{m}/{date}.parquet"


# ── generic path-info / metadata / bounded-read, shared by all three corpus forms ──


def _path_info(path: str, extra: Dict[str, Any]) -> Dict[str, Any]:
    exists = os.path.exists(path)
    result: Dict[str, Any] = {**extra, "path": path, "exists": exists}
    if exists:
        result["size_bytes"] = os.path.getsize(path)
    return result


def _file_info(path: str, extra: Dict[str, Any], order_key: Optional[str] = None, order_unit: Optional[str] = None) -> Dict[str, Any]:
    import pyarrow.parquet as pq  # noqa: PLC0415

    if not os.path.exists(path):
        raise FileNotFoundError(f"No file at {path}")

    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow

    return {
        **extra,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "row_count": pf.metadata.num_rows,
        "row_group_count": pf.metadata.num_row_groups,
        "columns": [{"name": f.name, "type": str(f.type)} for f in schema],
        "order_key": order_key,
        "order_key_unit": order_unit,
    }


def _read_rows(
    path: str,
    extra: Dict[str, Any],
    row_group: int,
    columns: Optional[List[str]],
    limit: int,
    offset: int,
) -> Dict[str, Any]:
    import pyarrow.parquet as pq  # noqa: PLC0415

    limit = max(0, min(limit, 20000))
    if not os.path.exists(path):
        raise FileNotFoundError(f"No file at {path}")

    pf = pq.ParquetFile(path)
    if row_group < 0 or row_group >= pf.metadata.num_row_groups:
        raise ValueError(f"row_group {row_group} out of range 0..{pf.metadata.num_row_groups - 1}")

    table = pf.read_row_group(row_group, columns=columns)
    sliced = table.slice(offset, limit)

    return {
        **extra,
        "row_group": row_group,
        "row_group_rows": table.num_rows,
        "offset": offset,
        "returned_rows": sliced.num_rows,
        "rows": sliced.to_pylist(),
    }


# ── SORTED ──────────────────────────────────────────────────────────────────────


def resolve_path(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """Resolve the on-disk path for one SORTED lane-day via quantum-feed's builder."""
    path = _qf_paths().replay_day(cluster, lane, date)
    return _path_info(path, {"cluster": cluster, "lane": lane, "date": date})


def get_file_info(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """Parquet metadata only for one SORTED lane-day — no data read."""
    path = _qf_paths().replay_day(cluster, lane, date)
    order_key, unit = ORDER_KEYS.get((cluster, lane), (None, None))
    return _file_info(path, {"cluster": cluster, "lane": lane, "date": date}, order_key, unit)


def read_rows(
    cluster: str,
    lane: str,
    date: str,
    row_group: int,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """Read one row group of one SORTED lane-day, column-projected."""
    path = _qf_paths().replay_day(cluster, lane, date)
    return _read_rows(path, {"cluster": cluster, "lane": lane, "date": date}, row_group, columns, limit, offset)


# ── PIVOT (per-ticker) — us_stocks_sip only; that's what qfdata.paths supports ──


def resolve_pivot_path(lane: str, ticker: str, date: str) -> Dict[str, Any]:
    """Resolve the on-disk path for one PIVOT ticker-day. us_stocks_sip only — the
    canonical builder (qfdata.paths.zticker_partition) doesn't take a cluster."""
    year_month = date[:7]
    partition = _qf_paths().zticker_partition(lane, ticker, year_month)
    path = f"{partition}/{date}.parquet"
    return _path_info(path, {"cluster": "us_stocks_sip", "lane": lane, "ticker": ticker, "date": date})


def get_pivot_file_info(lane: str, ticker: str, date: str) -> Dict[str, Any]:
    """Parquet metadata only for one PIVOT ticker-day — no data read."""
    year_month = date[:7]
    partition = _qf_paths().zticker_partition(lane, ticker, year_month)
    path = f"{partition}/{date}.parquet"
    order_key, unit = ORDER_KEYS.get(("us_stocks_sip", lane), (None, None))
    return _file_info(path, {"cluster": "us_stocks_sip", "lane": lane, "ticker": ticker, "date": date}, order_key, unit)


def read_pivot_rows(
    lane: str,
    ticker: str,
    date: str,
    row_group: int = 0,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """Read one row group of one PIVOT ticker-day, column-projected. Most ticker-days
    are a single row group; use get_pivot_file_info first to check."""
    year_month = date[:7]
    partition = _qf_paths().zticker_partition(lane, ticker, year_month)
    path = f"{partition}/{date}.parquet"
    extra = {"cluster": "us_stocks_sip", "lane": lane, "ticker": ticker, "date": date}
    return _read_rows(path, extra, row_group, columns, limit, offset)


# ── RAW (vendor bytes, unsorted) ─────────────────────────────────────────────────


def resolve_raw_path(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """Resolve the on-disk path for one RAW lane-day."""
    path = raw_day(cluster, lane, date)
    return _path_info(path, {"cluster": cluster, "lane": lane, "date": date})


def get_raw_file_info(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """Parquet metadata only for one RAW lane-day — no data read."""
    path = raw_day(cluster, lane, date)
    order_key, unit = ORDER_KEYS.get((cluster, lane), (None, None))
    return _file_info(path, {"cluster": cluster, "lane": lane, "date": date}, order_key, unit)


def read_raw_rows(
    cluster: str,
    lane: str,
    date: str,
    row_group: int,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """Read one row group of one RAW lane-day, column-projected. RAW is vendor file
    order, not timestamp-sorted — prefer the SORTED tools for anything order-sensitive."""
    path = raw_day(cluster, lane, date)
    return _read_rows(path, {"cluster": cluster, "lane": lane, "date": date}, row_group, columns, limit, offset)
