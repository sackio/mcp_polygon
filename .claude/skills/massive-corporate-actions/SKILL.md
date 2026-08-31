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

## Earnings ANNOUNCEMENT dates (BMO/AMC) — full playbook, not a single tool

Neither `/tmx/v1/corporate-events` nor `/benzinga/v1/earnings`/`list_benzinga_earnings` are
reachable on this account — confirmed live 2026-08-29, HTTP 403 "not entitled" on both
(Partners-tier products; the billed Benzinga News line only covers `/benzinga/v2/news`). Fixed
by Ben upgrading the plan is the only way to unblock these directly — don't retry, don't
assume transient. **Don't reach for these first anyway; two better routes exist below.**

### Forward calendar (upcoming earnings) — AlphaVantage, free, confirmed working

`mcp__alphavantage__EARNINGS_CALENDAR(symbol, horizon)` — a separate fleet MCP, no Massive
entitlement needed. **Omit `symbol` for the WHOLE MARKET in one call** — confirmed live
2026-08-31: `horizon="3month"` with no symbol returned 1,589 tickers' upcoming earnings in a
single CSV response, one request. `timeOfTheDay` (BMO/AMC) is present but sparse — filled on
~9% of rows (142/1,589) in that pull — check per-row, don't assume it's there. AlphaVantage's
free tier caps at **25 requests/day total**, so pulling the full calendar once (or a couple
times) a day is well within budget; don't loop it per-ticker, one no-symbol call already gets
everything.

### Historical earnings-date/timing — ask `mind`, not Massive

`mind` (peer agent, own independent EDGAR ingestion) has SEC 8-K Item 2.02 filings — the
SEC-mandated earnings-results disclosure — as of 2026-08-31: ~81,600 filings, 5,906 tickers,
`filed_at` spanning 2020-01-02–present. ⛔ **You MUST check `edgar_filing.filed_at_precision`
before trusting `filed_at` for timing** — corrected same-day by `mind` after an initial wrong
answer:
- `filed_at_precision='second'` (~3.6% of rows, **2025-08-04–present only**): real SEC
  acceptance timestamp, second-accurate, real BMO/AMC structure. Use `filed_at` directly.
- `filed_at_precision='date'` (~96.4% of rows, **2020-01-02–2026-08-11**): midnight-stamped,
  **zero intraday timing information** — not correctable with a cutoff heuristic, there is
  simply no time-of-day here. Gives you the announcement DATE only.

⇒ **For a multi-year backtest needing session-level (BMO/AMC) assignment, `mind`'s real-timing
coverage only reaches back to 2025-08-04** — roughly the last 13 months as of this writing.
Before that, you get date-only. This is a genuine, currently open gap, not resolved by "ask
mind" alone. Three live options, all Ben's call on cost, none started as of 2026-08-31:
(1) pay for the Massive/TMX add-on for the pre-2025-08-04 window, (2) ask `mind` to run a
per-filing SEC backfill of the true acceptance timestamp (they have the extractor, haven't
run it, not authorized to spend that fetch budget unasked), (3) accept date-only coverage
before 2025-08-04 and gate your design around that limitation explicitly.

Also flagged by `mind`, worth knowing regardless of which option is chosen: yearly filing
volume in their corpus is uneven (2021-23 noticeably thinner) — this is their ingestion
history, not fewer real earnings that year; normalize by a per-year denominator or a backtest
will read the hole as a regime change.

### What NOT to use

- The ingested Benzinga `news_v1` corpus (RAW, 2009-01-01–present) tags articles with an
  `earnings`/`earnings beats` channel + `published_at` — a noisy same-day-recap proxy at best,
  not a structured event record, no BMO/AMC field.
- `mind`'s `earnings_event` table / `mind_earnings_brief` tool — forward-calendar only
  (rolling ~4 months), zero history, `actual` empty on every row. 88% of it is sourced from
  the same AlphaVantage `EARNINGS_CALENDAR` above — call that directly instead.
- `mind`'s `event.earnings_result.occurred_at` — corrupted/unreliable, timestamps span
  year 1099 to 2033. Never use as an announcement date.
