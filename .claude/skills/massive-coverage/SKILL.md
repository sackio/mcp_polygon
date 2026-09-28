---
name: massive-coverage
description: One reference for what market data exists on this Massive/Polygon account, how far back, and in which lane (REST, corpus, flatfiles) — before assuming a data class is missing or re-deriving coverage from scratch. Use whenever a task asks "do we have X data" or "how far back does Y go" for any asset class. Trigger phrases: do we have this data, how far back, data coverage, history floor, earliest date, since when, what data exists, is this available.
---

# massive-coverage

⛔ **"Do we have X" has three possible answers — REST (live/reference), a saved corpus (if
materialized), or flatfiles on demand (S3 pull).** Check all three before answering "no" — see
`massive-flatfiles` for the full decision tree. This skill is the coverage FLOORS reference;
it does not replace checking which of the three lanes actually has what you need today.

⚠️ Every floor below is a MEASURED fact as of the date shown, not a permanent guarantee — vendor
coverage can extend backward or a lane can stop being maintained. Re-verify a floor before
citing it in anything that outlives a session.

⛔⛔ **`get_ticker_details().list_date` is company-listing metadata, NOT proof of that TICKER's
price-series depth — verify with a real aggs call, not this field.** Measured 2026-09-27 on
Alphabet: `GOOGL`'s `list_date` reads 2004-08-19 (the company's real IPO date), but `GOOGL` as a
ticker only exists from Alphabet's 2014 share-class reorganization — its own aggs start
2014-04-03, 3,139 daily bars. The pre-2014 history (5,561 daily bars back to 2004-08-19) lives
under `GOOG`, a DIFFERENT ticker. A study that reads `list_date` and assumes the price series
matches loses a decade silently, with no error and nothing visible in a plot — caught only by
comparing session COUNTS across names in the same basket (GOOGL printed 3,139 where every peer
printed 5,561). ⇒ For any dual-class or reorganized company, check which class ticker actually
carries the deep history (usually the original pre-reorganization symbol) before trusting
`list_date` for span planning, and consider a basket-wide session-count sanity check (a name
covering well under the group's longest span is a signal, not noise).

⛔⛔ **A ticker RENAME (not a share-class split) leaves a silent mid-series hole, not a missing
tail — checking bar existence at the endpoints of a span is not enough.** Measured 2026-09-27,
caught by tradedesk-11 after this seat wrongly called `QQQ` "clean" from spot-checking 2003 and
March-2011 bars only: `QQQ` daily has a real 1,588-session hole, **2004-12-01 → 2011-03-22**
(Nasdaq's QQQQ→QQQ symbol change, 2011-03-23) — `list_aggs` jumps straight from the 2004-11-30
bar to 2011-03-23, a fake +42.4% overnight step that is a ticker-continuity artifact, not a
market move. The old `QQQQ` symbol carries the missing 1,588 sessions exactly (`list_aggs`
2003-09-10→2011-12-31, verified: 1,588 rows land precisely inside the hole, zero overlap with
`QQQ`, last `QQQQ` close 55.40 on 2011-03-22 → first `QQQ` bar 55.71 on 2011-03-23 = a real
+0.56% step, no rescale needed at the join — both symbols are already on one price scale). Full
span check: 4,210 `QQQ` rows + 1,588 `QQQQ` rows = 5,798 = SPY's and IWM's session count over
the same window, exact match. ⇒ For pre-2011 QQQ history, splice `QQQQ` (2004-12-01..2011-03-22)
to `QQQ` (2011-03-23 on); `QQQ`'s own 2003-09-10..2004-11-30 bars are fine standalone. ⇒
**The general lesson: verifying a span means counting sessions across the WHOLE window (or at
minimum probing inside it, not just at the two ends) — a hole in the middle is invisible to an
endpoints-only check**, same failure shape as the `list_date` trap above, different mechanism
(a real rename vs. a share-class reorg).

## US equities (`us_stocks_sip`)

**Hard floor: 2026-09-10 → 2003-09-10**, confirmed three independent ways 2026-09-21 (memo
`b7c86725`): REST `get_aggs` returns genuinely empty (`queryCount:0`) before this date for ANY
ticker; corpus `list_corpus_lanes` spans start exactly here for trades/quotes/minute_aggs; S3
flatfiles have zero dates before it. **This is a vendor-wide floor, not a per-ticker inception
date** — QQQ (real listing 1999) and TLT (2002) both show nothing before 2003-09-10 same as
any other ticker. Current end: latest trading day (checked live via `list_flatfile_dates`).

⛔⛔ **A custom `multiplier` aggregate bucket is NOT session-anchored — it sits on a fixed clock
grid, not 9:30 ET.** Measured 2026-09-28: `get_aggs`/`list_aggs(multiplier=65, timespan="minute")`
on AAPL 2024-01-02 returns bucket boundaries at 08:15, 09:20, 10:25 UTC... — none of which is
14:30 UTC (= 9:30 ET, that day's session open), so the open falls in the MIDDLE of a bucket,
mixing premarket into the first "regular session" bar. This breaks any strategy assuming N clean
same-length candles starting exactly at the open (e.g. "6 equal 65-minute candles over the
390-minute session"). `multiplier=30` happens to land on session open (the 30-min grid coincides
with :00/:30-past-the-hour, and 9:30 ET is always on that grid in both EST/EDT) — treat this as a
coincidence of 30 dividing the clock evenly, not a guaranteed vendor behavior, and don't assume it
holds for other periods. ⇒ For any session-relative custom-period intraday bar, pull 1-minute
bars, filter to RTH (9:30-16:00 ET) yourself, then resample anchored fresh at 9:30 ET each
session — never rely on the REST API's native `multiplier` param for a session-relative bar count
claim.

## US options (`us_options_opra`)

- **Day aggregates** (trade-derived OHLC/volume): flatfiles continuous **2014-06-02 → current**
  (verified via `list_flatfile_dates` per-year, 2026-09-21 — `list_flatfiles` without a year
  filter caps at 100 files and will make this look like it ends in 2014, see `massive-flatfiles`).
- **NBBO quotes**: only **2022-03 → current** (memo `bb972aaf`). Genuinely different coverage
  window from day_aggs above — never assume the two share a start date.
- Pre-2015: expiry convention was the Saturday after the third Friday, not the Friday itself —
  a third-Friday-only query finds nothing for that era (memo `44612fa4`).
- quantum-data's own SORTED/PIVOT ingestion of options day/minute_aggs and trades stopped
  2026-06-02 (CLAUDE.md) — that's an ingestion-pipeline stop, NOT a vendor data stop; the
  flatfiles above continue being published past that date. Use RAW or flatfiles for anything
  after 2026-06-02, not SORTED/PIVOT.

## US indices (`us_indices`)

Minute aggregates share the equities floor, 2003-09-10. **Index VALUES (`values_v1`) are a
separate, much later floor: 2023-02-14** (measured via `list_corpus_lanes`, 2026-09-21) — don't
assume index-level history goes back as far as the minute-bar history.

## Forex (`C:` tickers) and spot metals (`C:XAUUSD` etc.)

Real floor 2009-09-25 04:00 UTC (measured 2026-09-27 on `C:EURUSD` minute bars — genuinely zero
rows before that instant), matching a long-standing but previously unverified desk note.

⛔⛔ **Hour-scale and 30-minute aggregates work FINE for FX and spot metals — a same-day
self-correction, kept here as a live example of the trap it actually was.** A first pass
2026-09-27 used `list_aggs` with a small `limit` (5-10) on `timespan="hour"`/`minute×60`/
`minute×30` for `C:EURUSD` and `C:XAUUSD`, got `resultsCount:0` every time, and wrongly
concluded the aggregation was broken for this ticker class. It was the already-catalogued
`limit` trap (memo `44612fa4`): `limit` caps SCANNED BASE BARS, not returned output buckets —
with `timespan="minute"` as the underlying granularity, `limit=5` scans 5 *minutes* of data,
nowhere near enough to complete even one hour-scale bucket, so the aggregation correctly
returns nothing. Re-run with `limit=50000`: `C:EURUSD` hour/1 on 2024-01-02 → 24 real bars;
`C:XAUUSD` minute/30 over 2024-01-01..05 → 187 real bars. Both fine. ⇒ Whenever `resultsCount`
is 0 but `queryCount` is nonzero and small, that is the `limit` trap, not a coverage gap — raise
`limit` before concluding anything is missing, on ANY ticker class, not just this one.

## Crypto (`global_crypto`)

`trades_v1` from **2017-10-02**; `minute_aggs_v1` from **2013-11-04** (measured via
`list_corpus_lanes`) — two different floors for the same asset class, trades starting much
later than aggregates. ⛔ **No crypto quotes lane exists anywhere** — not corpus, not
flatfiles, not REST (confirmed 2026-08-31). Crypto/XAU aggregates ARE available via REST
despite not appearing as a named billed line item on the plan (memo `396aba53`) — don't
conclude "unavailable" from a billing description alone.

## News (`benzinga_news_v1` corpus)

From **2009-01-01** (measured via `list_corpus_lanes`).

## Benzinga structured products (ratings/earnings/guidance)

Entitled 2026-09-17 (ratings) / 2026-09-19 (earnings, guidance) — see `massive-news` for the
per-product entitlement history and known traps (`date_status` mislabeling, bucketed `time`
field pre-2026). Ratings claim full history back to **2011-12-08** with real-time updates
(vendor doc claim, spot-checked live).

## Corporate actions

Splits: 18,397 rows from **2011** (measured, see memo `44612fa4`). Dividends similarly deep.
Use the explicit `list_splits`/`list_dividends` tools, not `call_api` on
`/v3/reference/splits`/`/dividends`, which silently return zero rows for every query (same
memo).

## SEC filings (Form 3/4, 8-K, EDGAR index)

Historical depth NOT YET MEASURED by this seat — see `massive-filings` for the endpoints and
their (dot-suffixed) filter params. Don't assume a floor here without checking; report back
if you measure one.

## Fundamentals

Point-in-time traps (restated `filing_date`, `ticker=` bugs, a hard history floor) are
documented separately in `massive-fundamentals` and memo `a56854f8` — check there rather than
assuming ratios/financials share any of the floors above.
