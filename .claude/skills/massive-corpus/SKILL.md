---
name: massive-corpus
description: Read quantum-data's ingested market-data corpus via the massive/polygon MCP's corpus tools (SORTED, PIVOT, RAW forms). Use whenever a task needs historical trades/quotes/aggs for a whole market or a single ticker from the NAS corpus, not the live API. Trigger phrases: read the corpus, sorted corpus, per-ticker parquet, zticker, ts-sorted, quantum-data flatfiles, row group, historical trades, historical quotes, historical aggregates, historical bars, tick data, NBBO history, parquet corpus, market data on the NAS, price history for a ticker, full day of trades, whole market data, order book history, resolve_corpus_path, read_corpus_rows, get_corpus_file_info.
---

# massive-corpus

quantum-data's corpus, reachable through the massive/polygon MCP (`mcp__polygon__*`), exists
in **three forms** with different tools and different coverage. Pick the right one before
calling anything.

| form | tools | scope | when |
|---|---|---|---|
| **SORTED** | `list_corpus_lanes`, `resolve_corpus_path`, `get_corpus_file_info`, `read_corpus_rows` | whole market, per-day, ts-ordered | you need a full day's cross-sectional data, or order matters |
| **PIVOT** | `resolve_pivot_path`, `get_pivot_file_info`, `read_pivot_rows` | **us_stocks_sip only**, per-ticker | you need one ticker's history — smaller, faster |
| **RAW** | `resolve_raw_path`, `get_raw_file_info`, `read_raw_rows` | vendor file order, unsorted | the 4 lanes with no sorted/pivot form (`us_options_opra` day/minute_aggs/trades, `us_indices` day_aggs — all stopped 2026-06-02), or you specifically need pre-sort vendor bytes |

⛔ **Never read a whole file.** A full day of `us_stocks_sip/quotes_v1` is ~10GB/419M rows.
Call `get_*_file_info` first (row count, row-group count, schema — cheap, no data read), then
`read_*_rows(row_group=N, columns=[...], limit=..., offset=...)`. `limit` is capped at 20,000
server-side regardless of what you ask for.

## Schema gotchas — measured, not documented anywhere else

- `conditions` is a **comma-joined string** (`"12,37"`), not an int array. Split it yourself.
- Timestamps are **nanoseconds** for all 7 market lanes. `benzinga_news_v1/published_at` is
  the one exception: **milliseconds**.
- `size` is a `double` (fractional shares are real). `us_stocks_sip/quotes_v1` has real rows
  with `bid_price=ask_price=0, bid_size=ask_size=0` — pre-market placeholder quotes, not a
  decoding bug. Filter before computing spread (see `massive-microstructure`).
- Corpus data is **raw, unadjusted** for splits/dividends. See `massive-adjustments`.
- PIVOT's canonical path builder (`qfdata.paths.zticker_partition`) only covers
  `us_stocks_sip` — there is no per-cluster pivot for other markets.

## Building a continuous per-ticker series

PIVOT already gives you one ticker across time — just resolve each date's file and
concatenate. For SORTED, you'd otherwise have to scan every day's whole-market file and
filter — don't; use PIVOT for single-ticker work, SORTED only for cross-sectional/whole-market
questions.

## Reference

`/mnt/nas/data/code/quantum-feed/specs/reference/sorted-corpus-replay-reference.md` — the
authoritative doc for SORTED (schemas, per-lane sizes, order keys, live-feed field mapping).
PIVOT and RAW are undocumented anywhere but this skill and the `polygon`/`massive` seat's
`CLAUDE.md`. If either seems off, ask the `polygon`/`massive` seat (they own this MCP) or
`quantum-data` (they own the corpus) before working around it.
