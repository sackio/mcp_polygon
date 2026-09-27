---
name: massive-filings
description: Look up SEC filings — Form 3/4 insider transactions, 8-K material-event disclosures and full text, and the master EDGAR index — through the massive/polygon MCP. Use for insider buying/selling, ownership changes, material-event classification, or discovering any filing by CIK/ticker/form type/date. Trigger phrases: insider transactions, insider buying, insider selling, Form 4, Form 3, beneficial ownership, 8-K, material event, disclosure category, EDGAR index, SEC filings, insider trading data, who filed, filing lookup.
---

# massive-filings

All four endpoints below are entitled and return real data (verified live 2026-09-23/26) via
`call_api`. ⛔ **Every one of them uses dot-suffixed filter params — a bare `ticker=`/`tickers=`
silently returns the UNFILTERED first page with no error**, exactly the trap documented in
memo `44612fa4` instance #8. Always call `search_endpoints(..., detail="more")` for the real
param list before building a query for any endpoint in this family — don't trust the short
description alone.

## Form 3 / Form 4 — insider ownership and transactions

`GET /stocks/filings/vX/form-4` (transactions — buys/sells/grants/exercises) and
`/vX/form-3` (initial ownership baseline). Same param shape on both:

- `tickers` (`.any_of`/`.all_of`) — **not** `ticker=` or `issuer_ticker=`.
- `issuer_cik` / `owner_cik` (`.any_of`) — resolve ticker→CIK via `/v3/reference/tickers` if
  filtering by CIK directly.
- `filing_date` (`.gt`/`.gte`/`.lt`/`.lte`) — **not** `transaction_date=`, which does not exist
  as a filter param and is silently ignored.
- `form_type` — `'4'`/`'4/A'` (or `'3'`/`'3/A'`).
- `transaction_code` (form-4 only) — `'P'` purchase, `'S'` sale, `'A'` grant/award, `'M'`
  exercise/conversion, etc.

Verified working call:
```
call_api(path="/stocks/filings/vX/form-4",
         params={"tickers.any_of":"AAPL","filing_date.gte":"2026-08-01"})
```
Key response fields: `issuer_name`, `issuer_trading_symbol`, `owner_name`,
`is_director`/`is_officer`/`is_ten_percent_owner`, `transaction_date`, `transaction_code`,
`transaction_acquired_disposed` (`A`/`D`), `transaction_shares`, `transaction_price_per_share`,
`transaction_value`. `sort=` only documented in the combined `col.asc`/`col.desc` form — an
old-style `sort=col&order=asc` pair errors.

## 8-K — material-event disclosures and full text

`GET /stocks/filings/8-K/vX/disclosures` — one row per tagged disclosure (a filing can produce
several), classified into a 3-tier taxonomy (primary/secondary/tertiary). Params: `cik.any_of`,
`tickers.any_of`/`.all_of`, `filing_date` (`.any_of`/`.gt`/`.gte`/`.lt`/`.lte`),
`tertiary_category` (exact match only — look up valid values first).

`GET /stocks/taxonomies/vX/disclosures` — the controlled vocabulary (primary/secondary/tertiary
category names + descriptions) that powers the above; check this before filtering on a
category name.

`GET /stocks/filings/8-K/vX/text` — parsed plain text of the core Items sections.
⚠️ **Uses `ticker` (singular), not `tickers`** — a genuine inconsistency with the Form-3/4 and
disclosures endpoints above in the same filings family (unverified whether a mismatched plural
silently no-ops here the same way `ticker=` does on form-4; assume it does until checked).
Params: `cik.any_of`, `ticker.any_of`, `form_type.any_of` (`'8-K'`/`'8-K/A'`), `filing_date`
(`.gt`/`.gte`/`.lt`/`.lte`). `limit` defaults to 10, max 100 — much lower than the other
filings endpoints (max 1000-10000) since this one returns full text bodies.

## EDGAR master index — discover any filing

`GET /stocks/filings/vX/index` — form types, filing dates, CIKs, tickers, issuer names,
accession numbers, and a direct SEC.gov link, across ALL form types. Params: `cik.any_of`,
`ticker.any_of` (singular, same as 8-K text), `form_type.any_of` (`'10-K'`, `'10-Q'`, `'8-K'`,
`'S-1'`, `'4'`, etc.), `filing_date` (`.gt`/`.gte`/`.lt`/`.lte`). `limit` defaults to 1000, max
10000 — the highest cap in this family, since it's metadata-only (no text body).

## ⛔⛔ 8-K disclosures is NOT a complete event universe — measured gap, not a filter bug

`tertiary_category=quarterly_earnings` looks like an authoritative "who reported earnings
today" list and is not one. Measured 2026-09-27 by tradedesk-12: on 2025-10-30 (a peak earnings
day), the filtered query returns 22 rows; the same endpoint with NO category filter, same day,
returns 441 rows total (mostly other categories — investor_presentation 58, dividend_declaration
36, quarterly_earnings 22, credit_facility 21, ...); `list_benzinga_earnings` with
`date_status=confirmed` returns 321 confirmed reporters the same day. **AAPL — which reported
Q4 that exact day via a real Item 2.02 8-K — has zero rows of ANY tertiary_category for its CIK
over 2025-10-27..2025-11-03.** Pagination was ruled out (stable 22 rows at limit=1000/200/25,
one page, same accessions every time) — this is a real coverage gap in what this lane ingests,
not a tagging or query-shape problem. `mind` separately reports ~69 Item 2.02 8-Ks/trading day
since 2024-07-01 with real second-precision `accepted_at` populated on 37,499/37,643 — denser
and already timestamped.

⇒ **Use this endpoint as a category-ENRICHMENT join (its taxonomy is genuinely useful and
undocumented elsewhere), never as the event universe itself.** For "which companies reported
earnings on date X," use `list_benzinga_earnings` (date_status=confirmed) or mind's own Item
2.02 population — not this lane alone.

## Who owns what

This seat (`massive`/`polygon`) surfaces Massive's own EDGAR ingestion. `mind` (see
`massive-mind`) runs an independent EDGAR ingestion with its own 8-K Item 2.02 timing work —
cross-check between the two if a filing-derived fact (especially timing) matters to a decision.
