---
name: massive-fundamentals
description: Get company financial statements (balance sheet, income statement, cash flow) through the massive/polygon MCP. Use for anything about financials, earnings, revenue, profit, balance sheet items, or fundamental analysis. Trigger phrases: financials, financial statements, balance sheet, income statement, cash flow statement, revenue, net income, earnings per share, EPS, gross profit, operating income, total assets, total liabilities, shareholder equity, quarterly earnings, annual report, 10-K, 10-Q, fundamental data, fundamentals.
---

# massive-fundamentals

`list_stock_financials(ticker, cik, statement='all', timeframe, fiscal_year, fiscal_quarter,
period_end_gte/lte, filing_date_gte/lte, limit, sort)` wraps Massive's
`/stocks/financials/v1/{income-statements,balance-sheets,cash-flow-statements}` (XBRL-sourced
flat records). ⛔ It replaced the vX `/vX/reference/financials` endpoint, which returns **HTTP
410** (sunset header 2026-10-09, successor = these endpoints). `statement` is `income_statement`,
`balance_sheet`, `cash_flow` or `all` (returns the three under `statements`). `ticker` (comma list
ok) is mapped to `tickers.any_of`; `period_of_report_date_gte/lte` are accepted as aliases of
`period_end_gte/lte`. Direct REST works too via `call_api`.

`timeframe`: `quarterly`, `annual`, `trailing_twelve_months`. Filters on the REST side:
`tickers[.any_of|.all_of]`, `cik[.any_of|.gt...]`, `period_end[.gte...]`, `filing_date[.gte...]`,
`fiscal_year`, `fiscal_quarter`, `timeframe`, `sort` (`period_end.desc`), `limit` (max 50,000).

## Response shape (flat, one record per period)

Identity: `tickers` (array), `cik` (10-digit string), `period_end`, `filing_date`, `fiscal_year`,
`fiscal_quarter`, `timeframe`. Income statement: `revenue`, `cost_of_revenue`, `gross_profit`,
`selling_general_and_administrative`, `research_development`, `operating_income`,
`income_before_income_taxes`, `income_taxes`, `consolidated_net_income_loss`,
`net_income_loss_attributable_common_shareholders`, `basic/diluted_earnings_per_share`,
`basic/diluted_shares_outstanding`, `ebitda`, ... Balance sheet and cash flow carry their own
fields (e.g. CFO is `net_cash_from_operating_activities`). Field presence varies by company
(e.g. `debt_current` null on ~1/3 of filers; banks' `operating_income` is pre-tax-like): check
what the response holds. Old nested `financials.{income_statement,...}` shape and `end_date`,
`period_of_report_date`, `acceptance_datetime`, `source_filing_url` no longer exist: the
stand-ins are `period_end` (period) and `filing_date` (below).

## Not built for computed ratios

There's no separate "financial ratios" tool — P/E, margins, etc. are yours to compute from
these raw statement fields (and a price from `massive-corpus`/REST aggs) rather than something
this MCP returns pre-calculated. (There IS a `/stocks/financials/v1/ratios` REST endpoint —
found 2026-09-05 via `search_endpoints`, not yet verified live or wrapped by an explicit tool;
reach it via `call_api` if needed, but treat it as unverified until someone checks it.)

## History floor is ~2009-2011, and it's structural (XBRL), not a plan/coverage gap

⛔ **Confirmed 2026-09-05**: the financials data has no annual data before fiscal_year 2009
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
`ticker=AAPL` (singular) is accepted with no error but silently ignored (the MCP tool maps it for you), returning an
arbitrary multi-ticker universe instead of just AAPL. The working filter is
`tickers.any_of=AAPL`. Every other documented filter (`period_end.gte`, `fiscal_year`, `sort`)
worked as expected — only the parameter name is the trap. `list_stock_financials` maps `ticker` to `tickers.any_of` for you (verified 2026-10-05: AAPL in, AAPL rows out).
