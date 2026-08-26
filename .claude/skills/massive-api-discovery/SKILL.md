---
name: massive-api-discovery
description: Discover and call any Massive/Polygon REST endpoint through the massive/polygon MCP's generic proxy (search_endpoints, call_api, query_data), instead of guessing a path or assuming an explicit tool exists. Use whenever the ~65 explicit per-endpoint tools (get_aggs, list_trades, etc.) don't cover what you need. Trigger phrases: search_endpoints, call_api, query_data, find the right endpoint, no tool for this, massive REST API, polygon REST API, 1500 endpoints, is there an endpoint for, which endpoint, generic REST call, SQL on results, store results as a table, multi-step analysis.
---

# massive-api-discovery

The massive/polygon MCP exposes ~65 explicit per-endpoint tools (`get_aggs`, `list_trades`,
`get_snapshot_ticker`, etc. — kept for backward compatibility) **plus** a 3-tool generic REST
proxy that reaches everything else Massive documents (1,500+ endpoints):

## 1. `search_endpoints(query, market?, detail?, scope?)`

Natural-language search over Massive's own doc index. Always start here if you're not sure an
explicit tool exists — it's usually faster than guessing a REST path. `detail="more"` adds
query-parameter docs; `detail="verbose"` adds response shape + a sample response. `market`
pins results to an asset class (`"Stocks"`, `"Options"`, `"Crypto"`, etc.) when you already
know it. `scope="functions"` searches this MCP's own built-in post-processing functions
instead of/alongside REST endpoints.

## 2. `call_api(path, params?, store_as?, apply?)`

Fetches from a path returned by `search_endpoints` (or a known explicit path like
`/v3/snapshot/options/{underlyingAsset}`). Returns CSV by default. Paginated responses include
a `next_url`-derived hint with the exact `path`/`params` for the follow-up call — follow it,
don't assume one page is everything.

`store_as="table_name"` stores the result as an in-memory table instead of returning it
inline — use this for anything you'll then filter/join/aggregate rather than consume raw.
Tables auto-expire after 1 hour.

## 3. `query_data(sql)`

SQL (including CTEs, window functions, JOINs, ILIKE, and FTS5 full-text search on TEXT
columns) against tables stored via `call_api`'s `store_as`. `SHOW TABLES` / `DESCRIBE <table>`
/ `DROP TABLE <table>` are special commands, not real SQL. For full-text search, use
`WHERE {table} MATCH 'query'` and prefer `snippet()`/`highlight()` over raw long TEXT columns
in your SELECT list — full paragraphs (news body, filing text) run thousands of tokens each.

## When to use this vs. an explicit tool vs. the corpus

- Explicit tool exists and does what you need → use it, it's simpler.
- No explicit tool, or you need multi-step analysis (fetch → filter → join) → `search_endpoints`
  → `call_api(store_as=...)` → `query_data`.
- Bulk historical trades/quotes/aggs → see `massive-flatfiles`/`massive-corpus` instead; the
  REST path is for live/reference/moderate-volume data, not a bulk-download substitute.
