---
name: massive-corporate-actions
description: Get dividend history, stock split history, and IPO calendar/history through the massive/polygon MCP. Use for anything about dividends, splits, or IPOs — dates, amounts, ratios, upcoming or historical. Trigger phrases: dividends, dividend history, dividend dates, ex-dividend date, when does X pay dividends, dividend yield, stock split, split history, split ratio, reverse split, IPO calendar, upcoming IPOs, when did X IPO, IPO date, IPO price, new listings, corporate actions.
---

# massive-corporate-actions

Three explicit tools, all real-time REST reference data (no saved corpus, no on-demand
flatfile needed — these are small, structured, and cheap to call live every time):

## Dividends — `list_dividends`

Filter by `ticker`, `ex_dividend_date` (exact or range via params), `frequency` (1=annual,
2=semi-annual, 4=quarterly, 12=monthly, 52=weekly, 0=one-time), `dividend_type` (`CD` = cash
dividend is the common case). Returns `cash_amount`, `declaration_date`, `ex_dividend_date`,
`record_date`, `pay_date` per event — real per-payment history, not a smoothed yield number.
For dividend-adjusted (total-return) price series, see `massive-adjustments` — this tool gives
you the raw events, adjustment logic is separate.

## Splits — `list_splits`

Filter by `ticker`, `execution_date`, `reverse_split` (bool). Use this to find the *exact*
date a split you're seeing as a price discontinuity happened, and its ratio, rather than
guessing from a chart — see `massive-adjustments` for why this matters (raw corpus/flatfile
data is unadjusted, so splits appear as real price jumps).

## IPOs — `list_ipos`

Filter by `ticker`, `ipo_status` (upcoming/history), `listing_date` (exact or range). Covers
both a forward-looking calendar and historical listings — same tool for both, just filter by
status/date direction.
