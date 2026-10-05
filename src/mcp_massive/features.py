"""Read-only access to quantum-data's published feature corpus: BARS and LABELS
(2026-07-02 onward, grid1114). Indicator/mask/state SURFACE files are not served
yet (quantum-data's backfill under server6 is still running) — see the
massive-features skill.

Owner of the data: quantum-data. Spec/universe: quantum-lab
(/mnt/nas/data/quantum/lab/universe/UNIVERSE-v1.md). This module only locates
and reads files; it computes nothing.

Layout (peer-reported by quantum-data 2026-10-03, verified on disk 2026-10-04):
  bars   ST : <root>/bars-manifestgrid1114-i9b9a-20260702/<day>/MANIFEST.json ->
              batch_dir/s<N>/bars-qf-egress-<N>.parquet, N=0..29, UNMERGED.
              A ticker lives in shard name_shard(ticker, 30)
              (FNV-1a 64 + Fibonacci mix, qf-engine symbols.rs:133).
  bars   MT : <root>/bars-manifestgrid1114-mt-iaec2-20260702/bars/<day>.parquet (merged)
  labels ST_H3  : <root>/labels-manifestgrid1114-i9b9a-20260702-h3/labels/<day>/s<N>.parquet
  labels ST_REF : <root>/labels-manifestgrid1114-i9b9a-20260702/labels/<day>/s<N>.parquet
  labels MT     : <root>/labels-manifestgrid1114-mt-iaec2-20260702/labels/<day>.parquet

⛔ Shards hold ~100M rows in ~90 row groups that are NOT sorted by spec_id or
symbol, so there is no row-group pruning: a (day, ticker, spec) read scans the two
key columns of every row group (~10-15 s) and then reads the requested columns only
for matching rows. Every read is capped (MAX_ROWS) and column-projected.
"""
import glob
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mcp_massive.features")

# Same export, two names: server4 (where the container runs) sees server5's
# /mnt/store as /mnt/server5; on server5 itself it is /mnt/store.
_ROOT_CANDIDATES = [
    "/mnt/server5/zquantum/qf-data",
    "/mnt/store/zquantum/qf-data",
]

BARS_ST = "bars-manifestgrid1114-i9b9a-20260702"
BARS_MT = "bars-manifestgrid1114-mt-iaec2-20260702"
LABELS = {
    "st_h3": "labels-manifestgrid1114-i9b9a-20260702-h3",   # h2/h16/h128, zstd, in progress
    "st_ref": "labels-manifestgrid1114-i9b9a-20260702",     # 8 horizons + end_ts_ns, snappy
    "mt": "labels-manifestgrid1114-mt-iaec2-20260702",      # 8-horizon format
}
N_SHARDS = 30
MAX_ROWS = 20000
SCAN_BUDGET_S = 120.0


def _root() -> str:
    for r in _ROOT_CANDIDATES:
        if os.path.isdir(r):
            return r
    raise FileNotFoundError(
        "quantum-data feature root not mounted (tried %s)" % ", ".join(_ROOT_CANDIDATES)
    )


def name_shard(name: str, n: int = N_SHARDS) -> int:
    """qf-engine symbols.rs name_shard: FNV-1a 64, Fibonacci mix, >>32, % n."""
    h = 0xCBF29CE484222325
    for b in name.encode("utf-8"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return (((h * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF) >> 32) % n


def _days(path: str) -> List[str]:
    try:
        names = os.listdir(path)
    except FileNotFoundError:
        return []
    out = []
    for n in names:
        d = n[:-8] if n.endswith(".parquet") else n
        if len(d) == 10 and d[4] == "-" and d[7] == "-":
            out.append(d)
    return sorted(set(out))


def _spec_list(root: str, lane_dir: str) -> List[str]:
    p = os.path.join(root, lane_dir, "SPEC-LIST.txt")
    if not os.path.isfile(p):
        return []
    with open(p) as f:
        return [l.strip() for l in f if l.strip()]


def list_feature_corpus() -> Dict[str, Any]:
    """What is published, with day coverage and spec counts (listing only, fast)."""
    root = _root()
    bars_st_days = [d for d in _days(os.path.join(root, BARS_ST))
                    if os.path.isfile(os.path.join(root, BARS_ST, d, "MANIFEST.json"))]
    out: Dict[str, Any] = {
        "root": root,
        "owner": "quantum-data (files), quantum-lab (universe spec), quantum-engine (live)",
        "bars": {
            "st": {"dir": BARS_ST, "specs": len(_spec_list(root, BARS_ST)),
                   "days": [bars_st_days[0], bars_st_days[-1]] if bars_st_days else None,
                   "n_days": len(bars_st_days), "shards": N_SHARDS,
                   "layout": "unmerged: ticker in shard name_shard(ticker, 30)"},
            "mt": {"dir": BARS_MT, "specs": len(_spec_list(root, BARS_MT)),
                   "days": None, "layout": "merged one file per day"},
        },
        "labels": {},
        "not_served": "indicator/mask/state surface files (quantum-data backfill running; "
                      "see massive-features skill)",
    }
    mt_days = _days(os.path.join(root, BARS_MT, "bars"))
    if mt_days:
        out["bars"]["mt"]["days"] = [mt_days[0], mt_days[-1]]
        out["bars"]["mt"]["n_days"] = len(mt_days)
    for k, d in LABELS.items():
        days = _days(os.path.join(root, d, "labels"))
        out["labels"][k] = {"dir": d, "days": [days[0], days[-1]] if days else None,
                            "n_days": len(days)}
    out["labels_note"] = ("two label schemas coexist until the h3 compaction: st_h3 has "
                          "h2/h16/h128, st_ref/mt have 8 horizons + end_ts_ns. Join to bars on "
                          "(spec_id, symbol, emit_seq), never on timestamps.")
    return out


def list_feature_specs(lane: str = "st", contains: Optional[str] = None,
                       limit: int = 200) -> Dict[str, Any]:
    root = _root()
    d = {"st": BARS_ST, "mt": BARS_MT}.get(lane)
    if d is None:
        raise ValueError("lane must be 'st' or 'mt'")
    specs = _spec_list(root, d)
    if contains:
        specs = [s for s in specs if contains in s]
    return {"lane": lane, "count": len(specs), "specs": specs[: max(1, min(limit, 2000))],
            "truncated": len(specs) > limit}


def _file_for(kind: str, lane: str, day: str, ticker: Optional[str]) -> str:
    root = _root()
    if kind == "bars":
        if lane == "st":
            if not ticker:
                raise ValueError("ticker required for lane 'st' (unmerged shards)")
            mf = os.path.join(root, BARS_ST, day, "MANIFEST.json")
            if not os.path.isfile(mf):
                raise FileNotFoundError("no MANIFEST for day %s in %s" % (day, BARS_ST))
            with open(mf) as f:
                m = json.load(f)
            batch = m["batch_dir"]
            # manifest paths use the server5 export name; map to whichever root exists
            for pre in ("/mnt/server5/zquantum/qf-data", "/mnt/store/zquantum/qf-data"):
                if batch.startswith(pre):
                    batch = root + batch[len(pre):]
                    break
            s = name_shard(ticker, N_SHARDS)
            hits = glob.glob(os.path.join(batch, "s%d" % s, "*.parquet"))
            if not hits:
                raise FileNotFoundError("no shard file for %s (shard s%d) on %s" % (ticker, s, day))
            return hits[0]
        if lane == "mt":
            return os.path.join(root, BARS_MT, "bars", day + ".parquet")
        raise ValueError("bars lane must be 'st' or 'mt'")
    if kind == "labels":
        d = LABELS.get(lane)
        if d is None:
            raise ValueError("labels lane must be one of %s" % list(LABELS))
        if lane == "mt":
            return os.path.join(root, d, "labels", day + ".parquet")
        if not ticker:
            raise ValueError("ticker required for ST labels (unmerged shards)")
        return os.path.join(root, d, "labels", day, "s%d.parquet" % name_shard(ticker, N_SHARDS))
    raise ValueError("kind must be 'bars' or 'labels'")


def resolve_feature_file(kind: str, lane: str, day: str, ticker: Optional[str] = None) -> Dict[str, Any]:
    p = _file_for(kind, lane, day, ticker)
    out: Dict[str, Any] = {"kind": kind, "lane": lane, "day": day, "ticker": ticker,
                           "path": p, "exists": os.path.isfile(p)}
    if ticker and lane != "mt":
        out["shard"] = name_shard(ticker, N_SHARDS)
    if out["exists"]:
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(p)
        out.update({"rows": pf.metadata.num_rows, "row_groups": pf.num_row_groups,
                    "columns": pf.schema_arrow.names})
    return out


def read_feature_rows(kind: str, lane: str, day: str, ticker: str,
                      spec_id: Optional[str] = None,
                      columns: Optional[List[str]] = None,
                      limit: int = 1000) -> Dict[str, Any]:
    """Rows for one ticker (and optionally one spec_id) on one day.

    Scans the key columns of every row group, then reads `columns` only for the
    matching rows. Capped at MAX_ROWS and SCAN_BUDGET_S.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    limit = max(1, min(int(limit), MAX_ROWS))
    p = _file_for(kind, lane, day, ticker if lane != "mt" else ticker)
    if not os.path.isfile(p):
        return {"error": "file not found", "path": p}
    pf = pq.ParquetFile(p)
    names = pf.schema_arrow.names
    key_sym = "symbol"
    if key_sym not in names:
        return {"error": "no 'symbol' column in %s" % p, "columns": names}
    cols = list(columns) if columns else names[: min(12, len(names))]
    bad = [c for c in cols if c not in names]
    if bad:
        return {"error": "unknown columns %s" % bad, "columns": names}
    need = set(cols)
    key_cols = [key_sym] + (["spec_id"] if spec_id and "spec_id" in names else [])
    t0 = time.time()
    parts = []
    got = 0
    scanned = 0
    for g in range(pf.num_row_groups):
        if time.time() - t0 > SCAN_BUDGET_S:
            return {"error": "scan budget %ds exceeded after %d/%d row groups; "
                             "returned nothing partial to avoid a biased subset"
                             % (SCAN_BUDGET_S, g, pf.num_row_groups), "path": p}
        k = pf.read_row_group(g, columns=key_cols)
        scanned += k.num_rows
        mask = pc.equal(k[key_sym], ticker)
        if spec_id and "spec_id" in names:
            mask = pc.and_(mask, pc.equal(k["spec_id"], spec_id))
        if not pc.any(mask).as_py():
            continue
        t = pf.read_row_group(g, columns=sorted(need | set(key_cols)))
        t = t.filter(mask)
        parts.append(t.select(cols))
        got += t.num_rows
        if got >= limit:
            break
    if not parts:
        return {"rows": 0, "path": p, "row_groups_scanned": pf.num_row_groups,
                "seconds": round(time.time() - t0, 1)}
    tbl = pa.concat_tables(parts).slice(0, limit)
    return {"path": p, "rows": tbl.num_rows, "truncated": got >= limit,
            "seconds": round(time.time() - t0, 1), "columns": cols,
            "data": tbl.to_pylist()}
