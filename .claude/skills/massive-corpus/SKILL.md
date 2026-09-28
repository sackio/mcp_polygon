---
name: massive-corpus
description: Read quantum-data's ingested market-data corpus via the massive/polygon MCP's corpus tools (SORTED, PIVOT, RAW forms). Use whenever a task needs historical trades/quotes/aggs for a whole market or a single ticker from the NAS corpus, not the live API. Trigger phrases: read the corpus, sorted corpus, per-ticker parquet, zticker, ts-sorted, quantum-data flatfiles, row group, historical trades, historical quotes, historical aggregates, historical bars, tick data, NBBO history, parquet corpus, market data on the NAS, price history for a ticker, full day of trades, whole market data, order book history, resolve_corpus_path, read_corpus_rows, get_corpus_file_info.
---

# massive-corpus

quantum-data's corpus, reachable through the massive/polygon MCP (`mcp__polygon__*`), exists
in **three forms** with different tools and different coverage. Pick the right one before
calling anything.

⛔⛔ **A TOOL FAILURE HERE IS NOT A DATA-ABSENCE CLAIM — DO NOT CONFLATE THEM.** Confirmed
2026-09-26: this MCP's corpus tools (`get_corpus_file_info` etc.) can fail entirely (measured:
`ModuleNotFoundError: No module named 'qfdata'`, a stale import path in this fork, unrelated to
the data) while the underlying parquet files are completely fine and directly readable — a
consumer with NAS access can read `/mnt/nas/data/quantum/replay/ts-sorted/<cluster>/<lane>/
<YYYY>/<MM>/<DD>.parquet` (this is the real path SHAPE — one file per day, not a single dated
filename) with their own parquet reader, no MCP tool required, whenever this seat's tool
wrapper is down. **"The corpus tool is broken" and "the corpus is unusable" are different
claims** — saying the wrong one caused a real consumer to nearly re-download ~150GB of
flatfiles that were already on the NAS. Check tool-vs-data separately before answering "do we
have X" while this MCP's own corpus tools are erroring.

| form | tools | scope | when |
|---|---|---|---|
| **SORTED** | `list_corpus_lanes`, `resolve_corpus_path`, `get_corpus_file_info`, `read_corpus_rows` | whole market, per-day, ts-ordered | you need a full day's cross-sectional data, or order matters |
| **PIVOT** | `resolve_pivot_path`, `get_pivot_file_info`, `read_pivot_rows` | **us_stocks_sip only**, per-ticker | you need one ticker's history — smaller, faster |
| **RAW** | `resolve_raw_path`, `get_raw_file_info`, `read_raw_rows` | vendor file order, unsorted | the 4 lanes with no sorted/pivot form (`us_options_opra` day/minute_aggs/trades, `us_indices` day_aggs — all stopped 2026-06-02), or you specifically need pre-sort vendor bytes |

⛔ **Never read a whole file.** A full day of `us_stocks_sip/quotes_v1` is ~10GB/419M rows.
Call `get_*_file_info` first (row count, row-group count, schema — cheap, no data read), then
`read_*_rows(row_group=N, columns=[...], limit=..., offset=...)`. `limit` is capped at 20,000
server-side regardless of what you ask for.

⛔ **`list_corpus_lanes`'s `span` end is live-checked; `days`/`rows` are NOT.** Fixed
2026-09-06 after the previous hardcoded end dates (stale since 2026-08-24) got read as a
real 2-week ingestion stall on `global_crypto` and reported as fact to two consumers and
Ben — quantum-data checked the actual files and found no gap at all. `span[1]` is now
verified against the filesystem on every call (`end_live_verified` says whether that check
found anything); `days`/`rows` are still a static baseline from
`days_rows_baseline_measured_at` (recomputing exact counts means reading every file) —
don't read those two as current.

## Schema gotchas — measured, not documented anywhere else

- `conditions` is a **comma-joined string** (`"12,37"`), not an int array. Split it yourself.
- Timestamps are **nanoseconds** for all 7 market lanes. `benzinga_news_v1/published_at` is
  the one exception: **milliseconds**.
- `size` is a `double` (fractional shares are real). `us_stocks_sip/quotes_v1` has real rows
  with `bid_price=ask_price=0, bid_size=ask_size=0` — pre-market placeholder quotes, not a
  decoding bug. Filter before computing spread (see `massive-microstructure`).
- ⛔⛔ **`quotes_v1`'s `bid_size`/`ask_size` change UNIT CONVENTION at a hard date boundary —
  2011-12-30 is the last day of RAW ROUND LOTS (`5` means 500 shares), 2012-01-03 is the first
  day of already-SHARE-equivalent values (always a clean multiple of 100).** Measured 2026-09-27
  directly against the parquet files on four independent liquid tickers (AAPL, MSFT, SPY, JPM —
  ruling out a single-ticker artifact): pre-boundary `ask_size` is tiny (median 2-13, max
  12-230, essentially never `% 100 == 0`); post-boundary it jumps by ~100x and is always a
  multiple of 100 (median 200-6000). Massive's own current docs (`flat-files/stocks/quotes.md`,
  `rest/stocks/trades-quotes/quotes.md`) say "shares" — **true only from 2012-01-03 on.** For any
  date before that, multiply `ask_size`/`bid_size` by 100 to get real shares, or displayed NBBO
  depth reads 100x too thin. Not yet checked whether PIVOT's copy of `quotes_v1`, or any other
  lane's size field, has an analogous break at the same or a different date.
- Corpus data is **raw, unadjusted** for splits/dividends. See `massive-adjustments`.
- PIVOT's canonical path builder (`qfdata.paths.zticker_partition`) only covers
  `us_stocks_sip`, and `resolve_pivot_path` takes no `cluster` argument at all — it's
  implicit. ⛔ **Passing a non-`us_stocks_sip` ticker (e.g. a crypto ticker like
  `X:BTC-USD`) does NOT error** — it silently builds a `us_stocks_sip/...` path anyway and
  returns `exists:false`, which reads exactly like "no data for this date" rather than
  "wrong cluster, the tool doesn't cover this." Confirmed 2026-08-31.
  ⚠️ **THE TOOL'S LIMIT IS NOT THE DATA'S LIMIT — this line used to say it was.** It read
  "there is no per-cluster pivot for other markets", and that is FALSE. Measured 2026-09-21
  by quantum-data: `zticker/global_crypto/minute_aggs_v1` and `zticker/global_crypto/trades_v1`
  each hold **1,016 `ticker=` partitions**, and the newest `X:BTC-USD` file (2026-09-20) is
  two days AHEAD of the newest `us_stocks_sip` AAPL file (2026-09-18). The crypto pivot
  exists, is current, and is maintained.
  ⇒ For crypto, read it DIRECTLY at
  `/mnt/nas/data/quantum/zticker/global_crypto/<lane>/ticker=<TICKER>/year_month=<YYYY-MM>/<YYYY-MM-DD>.parquet`
  rather than falling back to a multi-GB SORTED whole-market day file and filtering — that
  fallback is what this line used to recommend and it costs gigabytes for one ticker.
  `resolve_pivot_path` still cannot build that path for you; construct it yourself.
- ⛔ **No crypto quotes lane exists anywhere** — not SORTED, not flatfiles, not REST. Confirmed
  2026-08-31: `global_crypto` only has `day_aggs_v1`/`minute_aggs_v1`/`trades_v1`. A genuine
  vendor gap, not an ingestion gap — quantum-data never had this to ingest.
- `global_crypto/trades_v1` only has 5 possible exchange codes (`get_exchanges(asset_class=
  "crypto")`): Coinbase(1), Bitfinex(2), Bitstamp(6), Binance.US(10), Kraken(23) — "Binance"
  here is Binance.US specifically, not global Binance, much thinner liquidity than the name
  suggests. `conditions` for crypto trades is simple and useful, unlike stocks' SIP mess:
  0=regular, 1=sell-side, 2=buy-side — a real aggressor-side tag. Only `participant_timestamp`
  exists (ns) — no separate consolidated-tape timestamp, since crypto isn't SIP-consolidated
  the way equities are.
- ⛔ **Column types drift across years — SORTED and PIVOT drift on DIFFERENT columns at
  DIFFERENT times, not the same pattern.** Confirmed 2026-08-31 by directly comparing
  `get_corpus_file_info` (SORTED) vs `get_pivot_file_info` (PIVOT) for AAPL `trades_v1` and
  `minute_aggs_v1` across 2015–2026 — an earlier version of this note wrongly implied both
  corpora drift the same way. They don't:
  - **SORTED** `trades_v1.size`: `int64` (confirmed 2015-01-05) → `double` (confirmed
    2026-02-24), boundary pinned to **~2026-02-23** (Massive/Polygon's fixed-6-decimal
    formatting change — see memo `1d1bef48`). `trades_v1.id`: `int32` (2015) → `int64`
    (2026) — **never becomes a string on SORTED.**
  - **PIVOT** `trades_v1.size`: **no drift** — `double` the entire way back to 2015-01-05.
    Do not apply the SORTED size-drift date to PIVOT.
  - **PIVOT** `trades_v1.id`: `int64` (confirmed 2021-07-01) → `large_string` (confirmed
    2022-06-01) — boundary somewhere in that 11-month window, not narrowed further. This is
    the one that flips to a *string*, and it's PIVOT-only.
  - **PIVOT** `minute_aggs_v1.volume`: `int64` (confirmed 2026-01-26) → `double` (confirmed
    2026-02-05) — a real drift, but ~3 weeks earlier than the SORTED `trades_v1.size` boundary
    people tend to reflexively cite; don't assume the two dates coincide.
  Naive multi-day/multi-year concatenation either fails on the type mismatch (pyarrow raises
  `ArrowInvalid: ... truncated converting to int64` when it unifies a double fragment against
  an int64-majority schema — this is what a schema-drift failure actually looks like, not
  corrupt data) or silently coerces (code assuming `id` is numeric breaks quietly once PIVOT
  flips it to string). Cast explicitly per-file before concatenating; never assume one dtype
  holds across years, and never assume SORTED's drift dates apply to PIVOT or vice versa.

## ⛔⛔ PIVOT does NOT share SORTED's 2003-09-10 floor for `minute_aggs_v1`

Measured 2026-09-27 by tradedesk-11, confirmed independently by this seat: PIVOT
`us_stocks_sip/minute_aggs_v1` starts **2015-01** for every ticker sampled (AAPL, SPY, QQQ, IBM,
GE, MSFT — all 141 months 2015-01..2026-09, none earlier; a direct `get_pivot_file_info` check
for AAPL 2010-06-15 returns `No file at ...`, 2015-01-05 exists). **SORTED reaches 2003-09-10 for
the same lane** (`ts-sorted/us_stocks_sip/minute_aggs_v1/2003/09/10.parquet` exists) — this seat
previously said both forms "share the same 2003-09-10 vendor floor," which is wrong for PIVOT.
⇒ For a pre-2015 intraday need, SORTED (whole-market, ~53MB/~1.85M rows per day) is the only
lane — a bulk scan, not a targeted per-ticker read, a different cost class than the
daily-gap-screen-then-PIVOT pattern this skill recommends elsewhere, which only holds
2015-onward. Whether the 2015 floor is an ingestion boundary that could be backfilled, or
intentional, and whether a coarser pre-2015 whole-market aggregate exists — **not yet
answered**, ask quantum-data.

## Building a continuous per-ticker series

PIVOT already gives you one ticker across time — resolve each date's file, **cast columns to
a fixed schema per day** (see the `trades_v1` drift above), then concatenate. For SORTED,
you'd otherwise have to scan every day's whole-market file and filter — don't; use PIVOT for
single-ticker work, SORTED only for cross-sectional/whole-market questions.

## Practical row-count sizing (measured, AAPL)

One trading day: `trades_v1` ~520K–821K rows, 1–2 row groups, ~9MB — usually 1-2 `read_*_rows`
calls. `quotes_v1` ~2.57M rows — roughly **130 calls** at the 20,000-row server cap. Size a
loop budget accordingly before starting a multi-day pull, especially for quotes.

## Reference

`/mnt/nas/data/code/quantum-feed/specs/reference/sorted-corpus-replay-reference.md` — the
authoritative doc for SORTED (schemas, per-lane sizes, order keys, live-feed field mapping).
PIVOT and RAW are undocumented anywhere but this skill and the `polygon`/`massive` seat's
`CLAUDE.md`. If either seems off, ask the `polygon`/`massive` seat (they own this MCP) or
`quantum-data` (they own the corpus) before working around it.
