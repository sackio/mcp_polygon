---
name: massive-adjustments
description: Apply split/dividend adjustment correctly when building a continuous price series from Massive/Polygon data. Use whenever a backtest or notebook spans a known split or dividend and needs prices that don't jump discontinuously. Trigger phrases: split adjusted, split-adjusted, continuous price series, backtest jumps, price discontinuity, price gap, price jump, adjusted close, unadjusted, raw prices, dividend adjustment, total return series, back-adjusted, why does the price jump, stock split price, does this account for splits, splits and dividends.
---

# massive-adjustments

⛔ **The NAS corpus (all three forms — sorted/pivot/raw) and Massive's raw S3 flatfiles are
UNADJUSTED — as-traded, not split- or dividend-adjusted.** Measured 2026-08-26: NVDA closed
~$1,196 on 2024-06-07 and ~$120.5 on 2024-06-10 (the 10-for-1 split's effective date) — a
clean ~10x discontinuity in the raw series. If your backtest reads straight from the corpus
or a flatfile, a split reads as a ~90% crash.

**The REST `call_api`/explicit-tool path is different**: `/v2/aggs/ticker/.../range/...`
defaults to `adjusted=true` (split-adjusted) unless you pass `adjusted=false`. This is the
opposite default from the corpus/flatfiles, and mixing the two without knowing which is which
is how a backtest gets a silent, direction-dependent error.

## What to do

1. **Prefer REST with `adjusted=true`** (the default) for anything that needs a clean,
   continuous series and doesn't need tick-level data — one call, already adjusted.
2. **If you must build from the corpus or flatfiles** (tick data, or a span REST can't serve
   efficiently), pull `list_splits`/`get_...` split and dividend data for the ticker over your
   date range and apply the standard back-adjustment yourself (multiply pre-split prices by
   the split ratio, working backward from the most recent date). Massive's `list_dividends`
   tool gives cash amounts and ex-dates if you also want dividend-adjusted (total-return)
   series, not just split-adjusted.
3. **Never assume "adjusted" without checking the source.** State explicitly in your
   notebook/report which series (raw or adjusted) you used and where the adjustment came
   from — a silently-wrong assumption here is a *directional* error, not a rounding one.

## Verifying your own adjustment logic

Pick a ticker+date with a well-known, unambiguous split (NVDA 2024-06-10 10:1; AAPL
2020-08-31 4:1; GOOG/GOOGL 2022-07-18 20:1) and confirm your adjusted series has **no**
discontinuity across that date — that's a cheap, concrete positive control.

## ⛔ `adjusted=true` doesn't fix a discontinuity `list_splits` doesn't know about

Confirmed live (`tradedesk`, 2026-08-30): **GOOG 2014-04-03** still shows a real ~50% single-day
drop with `adjusted=true` — `list_splits(ticker="GOOG"/"GOOGL")` has no record of that date at
all, only the 2022-07-18 20:1 split. The 2014 event was Google's Class C share creation, a
stock dividend effected as a 2:1 split rather than a plain split, and it's simply absent from
Massive's splits reference data. Forward-applying `list_splits` yourself hits the identical
gap — there's no route around this except hardcoding the known ratio for tickers/dates you
know are affected. **A clean series across a well-known split is not proof `adjusted=true`
is complete** — it only proves the splits it knows about are handled.

## ⛔ A huge single-day "adjusted" move can be a bad raw print, not a split residual

Also confirmed 2026-08-30: **SIRI 2007-06-14** showed adjusted close dropping ~89% the next
day, which looks exactly like an unapplied reverse split — but isn't. Unadjusted `get_aggs`
for that date shows open=2.79/low=2.75 (consistent with every surrounding day) alongside
close=high=25.55 — a ~9.2x bad tick in the raw feed itself. SIRI's real 2024-09-10 10:1
reverse split was applied *correctly*; it just faithfully scaled that one bad print up to
~$255 in the adjusted series. Before assuming a large adjusted-series move is a
missing/wrong split: check the **unadjusted** bar for that date first — if the anomaly is
already there pre-adjustment, it's a data-quality bug in the raw print, not an adjustment
problem, and no split-handling fix will touch it.
