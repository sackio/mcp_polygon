---
name: massive-fundamentals
description: Get company financial statements (balance sheet, income statement, cash flow) through the massive/polygon MCP. Use for anything about financials, earnings, revenue, profit, balance sheet items, or fundamental analysis. Trigger phrases: financials, financial statements, balance sheet, income statement, cash flow statement, revenue, net income, earnings per share, EPS, gross profit, operating income, total assets, total liabilities, shareholder equity, quarterly earnings, annual report, 10-K, 10-Q, fundamental data, fundamentals.
---

# massive-fundamentals

`list_stock_financials(ticker, timeframe, filing_date/period_of_report_date filters, cik,
company_name, sic, limit)` — SEC-filing-sourced financials, verified live against AAPL FY2025.

`timeframe`: `"annual"` or `"quarterly"`. Filter by `period_of_report_date` (the fiscal period
covered) or `filing_date` (when it was actually filed with the SEC — these differ, sometimes
by weeks) depending on which you actually mean.

## Response shape

Each result has `financials` split into four sections, each a dict of `{field_name: {value,
unit, label, order}}`:

- `income_statement` — `revenues`, `cost_of_revenue`, `gross_profit`, `operating_expenses`,
  `operating_income_loss`, `net_income_loss`, `basic_earnings_per_share`,
  `diluted_earnings_per_share`, `research_and_development`,
  `selling_general_and_administrative_expenses`, etc.
- `balance_sheet` — `assets`, `current_assets`, `noncurrent_assets`, `liabilities`,
  `current_liabilities`, `noncurrent_liabilities`, `equity`, `long_term_debt`, `inventory`,
  `accounts_payable`, etc.
- `cash_flow_statement` — `net_cash_flow_from_operating_activities`,
  `net_cash_flow_from_investing_activities`, `net_cash_flow_from_financing_activities`,
  `net_cash_flow`, and `_continuing` variants.
- `comprehensive_income` — `comprehensive_income_loss` and its components.

Field presence varies by company/filing (not every company reports every field) — don't
assume a fixed schema, check what's actually in the response for the ticker you're working
with. `order` gives the field's canonical display order if you're building a statement view.

`source_filing_url`/`source_filing_file_url` link back to the actual SEC filing (XBRL) this
was extracted from, if you need to verify or go deeper than the structured fields.

## Not built for computed ratios

There's no separate "financial ratios" tool — P/E, margins, etc. are yours to compute from
these raw statement fields (and a price from `massive-corpus`/REST aggs) rather than something
this MCP returns pre-calculated. (There IS a `/stocks/financials/v1/ratios` REST endpoint —
found 2026-09-05 via `search_endpoints`, not yet verified live or wrapped by an explicit tool;
reach it via `call_api` if needed, but treat it as unverified until someone checks it.)

## History floor is ~2009-2011, and it's structural (XBRL), not a plan/coverage gap

⛔ **Confirmed 2026-09-05**: `list_stock_financials` has no annual data before fiscal_year 2009
for AAPL (earliest `end_date` 2009-09-26, 17 total annual records through FY2025, no gaps) or
before FY2011 for GE (earliest `end_date` 2011-12-31, 15 total annual records) — both companies
have traded for decades longer than that. tradedesk independently pulled Massive's newer
`/stocks/financials/v1/{income-statements,balance-sheets,cash-flow-statements}` REST endpoints
directly (paginated, 5,459 CIKs) and found the same wall: no `period_end` earlier than
2010-01-29 anywhere in the dataset.

This is the SEC's phased XBRL-tagging mandate, not a Massive limitation: large accelerated
filers were required to tag financial statements in XBRL starting with fiscal years ending
after 2009-06-15; other filers phased in through fiscal years ending after 2011-06-15 — which
is exactly why AAPL's floor is earlier than GE's. Massive's fundamentals product is XBRL-sourced,
so no flatfile lane, no other endpoint, and no plan upgrade gets you pre-XBRL fundamentals —
the structured data doesn't exist upstream. A benchmark window starting before ~2009 needs a
non-XBRL-derived source (e.g. parsed 10-K text/tables from elsewhere) — outside this supply.

## `filing_date` is the LATEST restatement, not the original filing — use the filings index for a PIT clock

⛔ **Measured 2026-09-05 (tradedesk, AAPL)**: the financials endpoints' own `filing_date` field
reflects the most recent restatement, not when that period was originally reported — AAPL's
FY2024 annual record carries `filing_date` 2025-10-31, sourced from the *FY2025* 10-K, i.e. it
lags the real filing by about a year. For point-in-time work (what did the market actually know,
and when), use `/stocks/filings/vX/index` with `form_type=10-K` instead — it gives the ORIGINAL
filing date. Measured for AAPL: 2025-10-31, 2024-11-01, 2023-11-03, 2022-10-28, 2021-10-29 —
each ~35 days after fiscal year end. This supersedes the flat period_end+6-months heuristic
some earlier work used as a workaround (see memo `30c4d4d8`) — prefer the filings-index route.

## `ticker=` (singular) is silently ignored on the newer financials REST endpoints

⛔ **Measured 2026-09-05 (tradedesk, direct REST)**: on `/stocks/financials/v1/*`,
`ticker=AAPL` (singular) is accepted with no error but silently ignored, returning an
arbitrary multi-ticker universe instead of just AAPL. The working filter is
`tickers.any_of=AAPL`. Every other documented filter (`period_end.gte`, `fiscal_year`, `sort`)
worked as expected — only the parameter name is the trap. This applies to hand-built
`call_api` requests against these specific endpoints; `list_stock_financials`'s own `ticker`
parameter has not been separately verified to map correctly (that tool wraps a related but
differently-shaped legacy endpoint — see field names above vs. `period_end`/CIK-count scale
tradedesk saw directly), so don't assume the wrapper is exempt without checking.
