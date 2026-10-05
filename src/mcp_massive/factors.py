"""Published asset-pricing factor models (Fama-French, Carhart, q-factors, AQR, ...).

Thin wrapper over our fork of `getfactormodels` (github.com/sackio/getfactormodels, pinned
to a commit in pyproject.toml). It fetches each dataset live from its source (Ken French,
AQR, global-q.org, ...) — nothing is stored here and Massive/Polygon is not involved.

⛔ Data currency is PER MODEL and moves: a factor set that ends years ago still joins
happily to recent returns and silently drops the unmatched rows. Every result carries
first_date/last_date — check last_date before trusting a regression.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mcp_massive.factors")

MAX_ROWS = 5000


def list_factor_models() -> Dict[str, Any]:
    from getfactormodels.utils.registry import _MODEL_REGISTRY

    models = [
        {"key": k, "name": v.get("name"), "aliases": v.get("aliases", [])}
        for k, v in _MODEL_REGISTRY.items()
    ]
    return {
        "count": len(models),
        "models": models,
        "note": "frequency d/w/m/y varies per model; region 'usa' default (some models "
        "support developed/global). Check last_date in get_factor_model results.",
    }


def get_factor_model(
    model: str = "ff3",
    region: str = "usa",
    frequency: str = "m",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 1000,
    tail: bool = False,
) -> Dict[str, Any]:
    import getfactormodels as gfm

    limit = max(1, min(int(limit), MAX_ROWS))
    m = gfm.model(
        model=model,
        region=region,
        frequency=frequency,
        start_date=start_date,
        end_date=end_date,
    )
    df = m.to_pandas()
    if df is None or len(df) == 0:
        return {"model": model, "rows": 0, "data": []}
    idx_name = df.index.name or "date"
    out = df.reset_index().rename(columns={df.index.name or "index": idx_name})
    first, last = str(out[idx_name].iloc[0])[:10], str(out[idx_name].iloc[-1])[:10]
    n = len(out)
    out = out.tail(limit) if tail else out.head(limit)
    out[idx_name] = out[idx_name].astype(str).str[:10]
    return {
        "model": model,
        "region": region,
        "frequency": frequency,
        "first_date": first,
        "last_date": last,
        "total_rows": n,
        "returned": len(out),
        "truncated": n > limit,
        "columns": [str(c) for c in out.columns],
        "data": out.where(out.notna(), None).to_dict(orient="records"),
    }
