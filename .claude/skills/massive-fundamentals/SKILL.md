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
this MCP returns pre-calculated.
