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

## ⛔ Earnings ANNOUNCEMENT dates (BMO/AMC) — entitlement gap, not a bug

This account is **not entitled** to either route that would give a confirmed BMO/AMC earnings
timestamp — confirmed live 2026-08-29: `call_api(path="/tmx/v1/corporate-events", ...)` and
`call_api(path="/benzinga/v1/earnings", ...)` / `list_benzinga_earnings` all return HTTP 403
"You are not entitled to this data." Both are Partners-tier products; the billed Benzinga
News line only covers `/benzinga/v2/news`. Fixing this needs Ben to upgrade the plan — don't
retry or assume it's transient.

**Partial on-disk workaround**: the ingested Benzinga `news_v1` corpus (RAW,
`benzinga_news_v1`, 2009-01-01–present) tags individual articles with an `earnings`/
`earnings beats` channel and a `published_at` timestamp — this is when Benzinga posted an
earnings-related article (often same-day results recaps), **not** a structured
scheduled-vs-actual event record with an explicit BMO/AMC field. Usable as a noisy proxy for
day-0 assignment in an event study, not a substitute for a real corporate-events feed.

⭐ **Better answer — don't buy the add-on, ask `mind`.** Confirmed 2026-08-31: `mind` (a peer
agent, own EDGAR ingestion, unrelated to Massive) has SEC 8-K Item 2.02 filings — the
SEC-mandated earnings-results disclosure — already ingested: 81,567 filings, 5,906 tickers,
2020-01-02–present. Two real caveats before using it: (1) their `filed_at` is the filer's
SEC-submission time, not true dissemination time — a submission after ~20:00 ET disseminates
the next morning, so raw `filed_at` misassigns the tradable session for exactly the
after-close releases a PEAD study cares about most; apply your own cutoff rule. (2) yearly
coverage is uneven (2021-23 is a thin-ingestion artifact on their side, not fewer real
earnings) — normalize by a per-year denominator or a backtest reads the hole as a regime
change. Ask `mind` for query access rather than routing through this MCP.
