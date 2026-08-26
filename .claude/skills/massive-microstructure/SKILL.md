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

## Effective spread from quotes_v1

Columns: `ticker, ask_exchange, ask_price, ask_size, bid_exchange, bid_price, bid_size,
conditions, indicators, participant_timestamp, sequence_number, sip_timestamp, tape,
trf_timestamp`.

⛔ **Filter before computing anything:**

1. **Zero-price rows are real, not a bug** — pre-market placeholder quotes with
   `bid_price=ask_price=0, bid_size=ask_size=0`. Drop them; a spread computed against $0/$0 is
   nonsense.
2. **`indicators` carries NBBO condition flags** — comma-string, same shape as `conditions`.
   Verified against Massive's own conditions/indicators glossary: `84` = Crossed_Market,
   `85` = Locked_Market, `-1` = Invalid, `20` = NonFirm. Exclude quotes carrying any of these
   before treating bid/ask as a real, tradeable NBBO.

With clean quotes: effective spread for a trade = `2 * |trade_price - midpoint| / midpoint`
where midpoint is the prevailing NBBO midpoint at (or just before) the trade's `sip_timestamp`
— join trades to the most recent preceding quote, not the exact-timestamp quote (ties/latency
mean an exact match is rare and not necessarily "the quote the trade actually saw").

## Extended hours

Both lanes include pre-market/after-hours activity — don't assume you're looking at RTH-only
data. See `massive-corpus` if you need to isolate regular trading hours.
