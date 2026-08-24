"""Read-only access to quantum-data's sorted flatfile corpus on the NAS.

This is a DIFFERENT corpus from flatfiles.py: flatfiles.py downloads directly from
Massive's S3. This module reads quantum-data's already-ingested, sorted, per-day
parquet corpus at /mnt/nas/data/quantum/replay/ts-sorted, built and owned by the
quantum-feed pipeline.

⛔ Path construction is NOT re-implemented here. It is imported from quantum-feed's
own single source of truth (`qfdata.paths.replay_day`) per quantum-data's directive
2026-08-24 — a second path builder is the exact defect quantum-feed's spec-030
preservation gate exists to catch. That module is stdlib-only (see its own
docstring), so importing it costs nothing beyond the sys.path append.

Reference: /mnt/nas/data/code/quantum-feed/specs/reference/sorted-corpus-replay-reference.md
(HEAD ec108bbd9, 2026-08-24) — read that before changing this file's tool surface.

⛔ A single day of a large lane (e.g. us_stocks_sip/quotes_v1) is 10GB / 419M rows
across ~1,045 row groups. Every read here is bounded to one row group and a caller-
supplied column projection — there is no "read the whole day" tool, deliberately.
"""
import os
import sys
from typing import Any, Dict, List, Optional

_QFDATA_TOOLS_DIR = "/mnt/nas/data/code/quantum-feed/tools/data"

# DECLARED, from quantum-feed's sorted-corpus-replay-reference.md §1 (measured
# 2026-08-24). The lane set is CLOSED by operator ruling — quantum-feed offers 33
# candidate lanes and only these 8 are taken. Re-verify against that doc if this
# list is ever suspected stale; it is not re-derived here on every call.
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

# Order-key column per lane, and its timestamp unit — DECLARED/MEASURED in the same
# reference doc §3, §3.1. benzinga is the one exception: milliseconds, not nanoseconds.
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


def _qf_paths():
    """Import quantum-feed's canonical path builder. Requires the NAS mount, which
    on the deployed container is bind-mounted read-only at the same absolute path."""
    if _QFDATA_TOOLS_DIR not in sys.path:
        sys.path.insert(0, _QFDATA_TOOLS_DIR)
    from qfdata import paths as qf_paths  # noqa: PLC0415
    return qf_paths


def resolve_path(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """Resolve the on-disk path for one lane-day via quantum-feed's own builder."""
    path = _qf_paths().replay_day(cluster, lane, date)
    exists = os.path.exists(path)
    result: Dict[str, Any] = {"cluster": cluster, "lane": lane, "date": date, "path": path, "exists": exists}
    if exists:
        result["size_bytes"] = os.path.getsize(path)
    return result


def get_file_info(cluster: str, lane: str, date: str) -> Dict[str, Any]:
    """Parquet metadata only (row count, row-group count, schema) — no data read."""
    import pyarrow.parquet as pq  # noqa: PLC0415

    path = _qf_paths().replay_day(cluster, lane, date)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No file for {cluster}/{lane}/{date}: {path}")

    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    order_key, unit = ORDER_KEYS.get((cluster, lane), (None, None))

    return {
        "cluster": cluster,
        "lane": lane,
        "date": date,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "row_count": pf.metadata.num_rows,
        "row_group_count": pf.metadata.num_row_groups,
        "columns": [{"name": f.name, "type": str(f.type)} for f in schema],
        "order_key": order_key,
        "order_key_unit": unit,
    }


def read_rows(
    cluster: str,
    lane: str,
    date: str,
    row_group: int,
    columns: Optional[List[str]] = None,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    """Read one row group, column-projected, with an offset/limit slice inside it.
    A single row group can still be hundreds of thousands of rows — limit is capped
    at 20,000 per call regardless of what's requested."""
    import pyarrow.parquet as pq  # noqa: PLC0415

    limit = max(0, min(limit, 20000))
    path = _qf_paths().replay_day(cluster, lane, date)
    if not os.path.exists(path):
        raise FileNotFoundError(f"No file for {cluster}/{lane}/{date}: {path}")

    pf = pq.ParquetFile(path)
    if row_group < 0 or row_group >= pf.metadata.num_row_groups:
        raise ValueError(f"row_group {row_group} out of range 0..{pf.metadata.num_row_groups - 1}")

    table = pf.read_row_group(row_group, columns=columns)
    sliced = table.slice(offset, limit)

    return {
        "cluster": cluster,
        "lane": lane,
        "date": date,
        "row_group": row_group,
        "row_group_rows": table.num_rows,
        "offset": offset,
        "returned_rows": sliced.num_rows,
        "rows": sliced.to_pylist(),
    }
