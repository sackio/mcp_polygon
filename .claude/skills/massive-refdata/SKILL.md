---
name: massive-refdata
description: Query quantum-data's read-only reference-data MongoDB (ticker universe, ETF constituents, market caps, classification) via the massive/polygon MCP, and build a point-in-time universe without survivorship bias. Use whenever a backtest needs "what tickers existed/were in an index on date X" rather than today's list. Trigger phrases: "survivorship bias", "point in time universe", "ticker universe", "ETF constituents", "historical market cap", "sector classification", "index membership on a date".
---

# massive-refdata

Read-only MongoDB access via `list_ref_collections`, `get_ref_collection_info`,
`query_ref_collection` (mcp__polygon__*). **Whitelisted to 6 collections in db `qf_feed`** —
this is the whole menu, nothing else is reachable through this MCP:

| collection | what it holds |
|---|---|
| `ticker_universe` | universe membership over time |
| `etf_constituents` | ETF holdings over time |
| `historical_caps` | market cap history |
| `sub_universe_classification` | sector/sub-universe classification |
| `ticker_details_cache` | cached per-ticker detail records |
| `polygon_trade_conditions` | trade condition code reference |

⛔ **Read-only is enforced in application code, not by the credential.** `query_ref_collection`
only exposes `find` semantics (filter/projection/sort/limit ≤500) and rejects
`$where`/`$function`/`$accumulator`/`$expr` in filters — there is no write path through this
tool, full stop.

## Point-in-time universe, not today's list

The whole reason these collections exist as *history*, not a snapshot: `ticker_universe` and
`etf_constituents` carry membership over time, not just current membership. A backtest that
queries "give me S&P 500 members" without a date filter and applies today's membership to
2015 data has survivorship bias baked in — dead/delisted/removed tickers silently vanish from
the sample, and the backtest looks better than any real strategy could have been. Filter by
whatever the collection's own time field is (check `get_ref_collection_info` for a sample
document first — the shape isn't documented anywhere else) before using membership as a
universe filter for a historical date.

## What's explicitly NOT here

The 17 `*_records` pipeline-state collections (spec-023 staging, currently all **empty** — not
built yet, not "no data") and `qf_feed_signals_warmup_corpus_*`/`__90day_baseline_*`
(experiment snapshots) are deliberately excluded — quantum-data's call, not a gap to work
around. Sibling databases `quantum`/`quantum_predict`/`quantum_reactor` on the same Mongo
server belong to other teams and aren't reachable through this tool either.
