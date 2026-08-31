---
name: massive-microstructure
description: Build tick/volume/dollar bars from raw trades, and measure effective spread from raw NBBO quotes, using the massive/polygon MCP's corpus tools. Use whenever a task needs market-microstructure detail (spread, liquidity, execution cost) rather than OHLCV bars. Trigger phrases: tick bars, volume bars, dollar bars, effective spread, bid-ask spread, bid ask spread, NBBO, crossed quote, locked quote, slippage measurement, market microstructure, liquidity, execution cost, transaction cost, quote data, trade data, order flow, market impact.
---

# massive-microstructure

Source data: `us_stocks_sip/trades_v1` and `us_stocks_sip/quotes_v1`, via PIVOT for one
ticker (`resolve_pivot_path`/`read_pivot_rows`, see `massive-corpus`) or SORTED for
cross-sectional. Both are consolidated-SIP totals (all exchanges combined via the tape), not
exchange-specific.

## Tick / volume / dollar bars from trades_v1

Columns: `ticker, conditions, correction, exchange, id, participant_timestamp, price,
sequence_number, sip_timestamp, size, tape, trf_id, trf_timestamp`. `size` is a double
(fractional shares are real, not an error). Order by `sip_timestamp` (nanoseconds) — sequential
scan of a file is already in emission order, no sort needed. Bar construction is just a
running accumulator over the ordered rows: N trades (tick), N shares (volume), or N dollars of
`price*size` (dollar) per bar — nothing Massive-specific here once you have clean ordered
trades, except: `conditions` is a comma-joined string (`"12,37"`), not an array — split it if
you need to exclude non-regular trades (odd-lot, average-price, etc.) from your bars.

⛔ **`minute_aggs_v1.volume` is NOT simply `sum(trades_v1.size)` filtered on `updates_volume`
— don't reach for the `list_conditions` flag alone.** The condition-codes reference
(`/v3/reference/conditions`) marks only codes 15/16/38 as `updates_volume: false`; odd-lot
code 37 is genuinely `updates_volume: true` (not a metadata error). But excluding only
{15,16,38} leaves a real residual against `minute_aggs_v1.volume` — confirmed 2026-08-30
across three independent measurements (`tradedesk` on Agilent and GME, this seat on
CENX 2025-01-06): **the exclusion set that actually reconciles to <1% is `{15, 16, 38, 8, 22}`**
— i.e. also drop condition 8 (opening/closing auction cross) and 22 (Prior Reference Price),
and **do NOT exclude odd lots (37)** — odd lots count toward `minute_aggs_v1.volume` despite
their small size. CENX check: `sum(trades_v1.size)` with only {15,16,38} excluded = 1,710,609
and matches `get_aggs(timespan="day").v` exactly, but `minute_aggs_v1.volume` summed across
that day's minute bars is 1,489,465 — a **different number from the day bar** — and only
`{15,16,38,8,22}` gets within ~0.6% of that minute-summed figure. ⚠️ **Day-bar volume and
sum(minute-bar volume) are not the same number for the same ticker-day** — pick the right
target before reconciling, they can differ by >10%. No Massive doc found describing either
computation explicitly; this exclusion set is empirical, not documented.

## Effective spread from quotes_v1

Columns: `ticker, ask_exchange, ask_price, ask_size, bid_exchange, bid_price, bid_size,
conditions, indicators, participant_timestamp, sequence_number, sip_timestamp, tape,
trf_timestamp`.

⛔ **Filter before computing anything:**

1. **Zero-price rows are real, not a bug** — pre-market placeholder quotes with
   `bid_price=ask_price=0, bid_size=ask_size=0`. Drop them; a spread computed against $0/$0 is
   nonsense.
2. ⛔ **`indicators`/`conditions` do NOT carry the documented flags in the stored NAS corpus —
   filter on price instead.** Massive's own glossary documents `84`=Crossed_Market,
   `85`=Locked_Market, `-1`=Invalid, `20`=NonFirm, but measured against SPY 2024-06-03
   (`us_stocks_sip/quotes_v1`, 2,572,150 rows, vbt 2026-08-26): `indicators` only ever takes
   `'1'` or `null`, `conditions` is the constant `'1,81'` on every single row, and 17,377 quotes
   are crossed by price (bid > ask) — including 16,585 inside 09:30–15:59 ET — with indicators
   and conditions identical to every clean row. **The documented flags mark 0 of them.** Not
   yet known whether ingestion drops the flags or upstream never carried them for this lane —
   ask quantum-data if it matters for your use case. **What actually works: filter
   `ask_price > bid_price > 0`.** An unfiltered spread on this corpus reaches -20,000 bps;
   filtered, SPY 2024-06-03 median spread was 0.379 bps, p99 0.95, max 26.7.

With clean quotes: effective spread for a trade = `2 * |trade_price - midpoint| / midpoint`
where midpoint is the prevailing NBBO midpoint at (or just before) the trade's `sip_timestamp`
— join trades to the most recent preceding quote, not the exact-timestamp quote (ties/latency
mean an exact match is rare and not necessarily "the quote the trade actually saw").

## Extended hours

Both lanes include pre-market/after-hours activity — don't assume you're looking at RTH-only
data. See `massive-corpus` if you need to isolate regular trading hours.
