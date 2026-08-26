---
name: massive-news
description: Get company news, analyst ratings, price targets, earnings surprises, and management guidance through the massive/polygon MCP's Benzinga integration. Use for anything about news, sentiment, analyst opinions, ratings changes, price targets, or earnings surprises. Trigger phrases: news, company news, market news, headlines, analyst rating, analyst opinion, price target, upgrade, downgrade, rating change, consensus rating, earnings surprise, earnings beat, earnings miss, management guidance, sentiment, what are analysts saying.
---

# massive-news

Two sources, different scope:

## General news — `list_ticker_news`

Filter by `ticker`, `published_utc` (exact or range), `order`/`sort`, `limit`. Straightforward
recent-articles-for-a-ticker lookup.

## Benzinga integration — deeper, structured

| tool | what |
|---|---|
| `list_benzinga_news` | articles, filterable by `tickers`/`tags`/`channels` (single or `_any_of`/`_all_of`), `author`, `published` date range |
| `list_benzinga_analyst_insights` | analyst commentary tied to a specific rating action |
| `list_benzinga_analysts` | analyst directory (name, firm) |
| `list_benzinga_firms` | firm directory |
| `list_benzinga_ratings` | individual rating actions — `rating_action` (upgrade/downgrade/etc.), `price_target_action`, tied to `benzinga_analyst_id`/`benzinga_firm_id` |
| `list_benzinga_consensus_ratings` | aggregated consensus per ticker per date, not per-analyst |
| `list_benzinga_earnings` | earnings results incl. `eps_surprise_percent`, `revenue_surprise_percent`, `fiscal_period`/`fiscal_year` |
| `list_benzinga_guidance` | forward guidance, `positioning` (raised/lowered/maintained), tied to fiscal period |

Most of these join on `benzinga_analyst_id`/`benzinga_firm_id`/`benzinga_rating_id` — to go
from "what did analysts say about X" to "who said it and what firm", chain
`list_benzinga_ratings` (or `_analyst_insights`) → `list_benzinga_analysts`/`list_benzinga_firms`
rather than expecting one call to return everything joined.

Almost every list tool here takes range/set filters per field (`_gt`, `_gte`, `_lt`, `_lte`,
`_any_of`, `_all_of` suffixes on the base field name) — check the tool's own parameter list
before assuming you need to filter client-side.

## Searching article/filing TEXT, not just metadata

None of the above search *inside* article bodies. For that: `call_api(path=..., store_as=...)`
to land results in a table, then `query_data` with FTS5 (`WHERE {table} MATCH 'query'`,
`snippet()`/`highlight()` in SELECT rather than the raw body column) — see
`massive-api-discovery`.
