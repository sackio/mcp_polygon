---
name: massive-flatfiles
description: Decide between REST, the saved NAS corpus, and on-demand S3 flatfiles when asked whether Massive/Polygon data exists for something, and how to page/cache flatfile downloads without blowing disk. Use before answering "do we have X data" and before any bulk historical pull. Trigger phrases: "do we have this data", "flatfile vs rest", "download flatfiles", "S3 pull", "bulk historical data", "which tool should I use for".
---

# massive-flatfiles

⚑ **"Do we have X" has up to three answers — check all three before answering.**

| path | tools | when |
|---|---|---|
| **REST** (live/reference) | `call_api`, `search_endpoints`, or an explicit tool (`get_snapshot_option`, `get_aggs`, etc.) | chains, snapshots, reference data, anything not a bulk historical pull — REST is often the *right* answer, not a fallback |
| **Saved NAS corpus** | `list_corpus_lanes`, `resolve_corpus_path`/`resolve_pivot_path`/`resolve_raw_path` (see `massive-corpus`) | bulk historical trades/quotes/aggs for the lanes quantum-data has already ingested — free, no download wait |
| **On-demand S3 flatfiles** | `list_flatfiles`, `download_flatfile`, `get_flatfile_info`, `list_flatfile_dates`, `list_flatfile_asset_classes`, `list_flatfile_data_types` | bulk historical data NOT in the saved corpus (e.g. options — see below) — costs a download, but it's there |

2026-08-26 lesson: asked whether options data existed, answered only "no saved corpus" +
"here's the on-demand S3 path" and left out REST entirely — for something shaped like an
options chain, REST is the right tool (chains/snapshots aren't a flatfile product at all).
Don't narrow to whichever path was already top of mind.

## S3 flatfile mechanics

- Client: boto3, S3v4 signing, endpoint/bucket from `POLYGON_S3_ENDPOINT`/`POLYGON_S3_BUCKET`
  env vars (not AWS defaults — there's no safe default for either).
- Key layout: `{asset_class}/{data_type}/{YYYY}/{MM}/{YYYY-MM-DD}.csv.gz` — CSV, not parquet.
  This is Massive's raw feed shape, different from the NAS corpus's parquet.
- `list_flatfiles` paginates for you; `download_flatfile` caches locally (survives container
  restarts) and won't re-download unless you pass `force=true`.
- No retry/backoff hardening beyond boto3's defaults, and no bulk-pull helper — for more than
  a handful of files, script your own loop with `list_flatfile_dates` to know what actually
  exists first (a 404 can mean "non-trading day" or "not published yet", not "gap").

## What's NOT on the NAS

Options (all of OPRA — chains, trades, quotes, aggs) — no saved corpus at all. Use REST for
chains/snapshots, on-demand S3 flatfiles for bulk historical (still live on Massive's S3, just
never ingested by quantum-data).
