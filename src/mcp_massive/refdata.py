"""Read-only access to quantum-data's reference-data MongoDB (db `qf_feed`).

⛔ Scope is deliberately narrow, per quantum-data 2026-08-24: only the collections
below are reference data. The other ~40 collections in `qf_feed` are pipeline
state (17 `*_records` collections — all empty, spec-023 not live yet) or
experiment snapshots (`qf_feed_signals_warmup_corpus_*`, `__90day_baseline_*`), and
`quantum`/`quantum_predict`/`quantum_reactor` on the same Mongo server belong to
other teams entirely. Only `qf_feed` and only this whitelist are reachable here.

⛔ The credential behind QF_MONGO_URI is NOT scoped read-only at the database
level (there is no separate read-only Mongo user) — it's the same credential
quantum-data's own pipeline uses. This module is what enforces read-only: every
path here is a `find`/count, and query filters are rejected if they contain any
server-side-JS operator ($where/$function/$accumulator/$expr) that could do more
than filter documents.
"""
import os
from typing import Any, Dict, List, Optional

DB_NAME = "qf_feed"

# name -> (description, doc count as measured by quantum-data 2026-08-24 14:39 EDT)
ALLOWED_COLLECTIONS: Dict[str, Dict[str, Any]] = {
    "ticker_universe": {"description": "Ticker universe membership", "measured_count": 5084094},
    "etf_constituents": {"description": "ETF constituent holdings", "measured_count": 4056753},
    "historical_caps": {"description": "Historical market capitalization", "measured_count": 378257},
    "sub_universe_classification": {"description": "Sub-universe / sector classification", "measured_count": 107919},
    "ticker_details_cache": {"description": "Cached per-ticker detail records", "measured_count": 13553},
    "polygon_trade_conditions": {"description": "Trade condition code reference", "measured_count": 55},
}

_DISALLOWED_OPERATORS = {"$where", "$function", "$accumulator", "$expr"}

_client = None


def _get_client():
    global _client
    if _client is None:
        import pymongo  # noqa: PLC0415

        uri = os.environ.get("QF_MONGO_URI", "")
        if not uri:
            raise ValueError("QF_MONGO_URI not configured in environment")
        _client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000, appname="mcp_massive/refdata")
    return _client


def _check_filter_safe(value: Any) -> None:
    """Recursively refuse any server-side-JS operator anywhere in a filter."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _DISALLOWED_OPERATORS:
                raise ValueError(f"Operator {k} is not allowed in a filter")
            _check_filter_safe(v)
    elif isinstance(value, list):
        for item in value:
            _check_filter_safe(item)


def _sanitize(value: Any) -> Any:
    """Make a Mongo document JSON-safe (ObjectId, datetime, etc. -> str)."""
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _require_allowed(collection: str) -> None:
    if collection not in ALLOWED_COLLECTIONS:
        raise ValueError(
            f"{collection!r} is not in the reference-data whitelist. "
            f"Allowed: {sorted(ALLOWED_COLLECTIONS)}"
        )


def list_collections() -> List[Dict[str, Any]]:
    """List the whitelisted reference-data collections with a description and
    the doc count as last measured (not a live count — see get_collection_info)."""
    return [{"name": name, **info} for name, info in ALLOWED_COLLECTIONS.items()]


def get_collection_info(collection: str) -> Dict[str, Any]:
    """Live estimated document count and one sample document, for a whitelisted
    collection."""
    _require_allowed(collection)
    coll = _get_client()[DB_NAME][collection]
    sample = coll.find_one()
    return {
        "collection": collection,
        "estimated_count": coll.estimated_document_count(),
        "sample_document": _sanitize(sample) if sample else None,
    }


def query(
    collection: str,
    filter: Optional[Dict[str, Any]] = None,
    projection: Optional[List[str]] = None,
    limit: int = 50,
    sort: Optional[List[List[Any]]] = None,
) -> Dict[str, Any]:
    """Read-only find() against a whitelisted collection. `sort` is a list of
    [field, 1|-1] pairs. `limit` is capped at 500."""
    _require_allowed(collection)
    filter = filter or {}
    _check_filter_safe(filter)
    limit = max(0, min(limit, 500))

    coll = _get_client()[DB_NAME][collection]
    cursor = coll.find(filter, projection).limit(limit)
    if sort:
        cursor = cursor.sort([(field, direction) for field, direction in sort])

    docs = [_sanitize(doc) for doc in cursor]
    return {"collection": collection, "filter": filter, "returned": len(docs), "documents": docs}
