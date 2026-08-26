---
name: massive-options
description: Get options chains, contract reference data, snapshots, greeks/IV, and historical options trades/quotes through the massive/polygon MCP. Use whenever a task needs option contract data — chains, a specific contract's quote, or bulk historical options data. Trigger phrases: option chain, option contracts, option snapshot, options data, options quote, greeks, delta, gamma, theta, vega, implied volatility, IV, open interest, OPRA, options trades, options quotes, options aggregates, calls and puts, strike price, expiration date.
---

# massive-options

No saved corpus for options exists on the NAS (checked 2026-08-26 — see `massive-corpus`).
Two live paths, pick based on shape:

## Chains, snapshots, reference — REST

- **Single contract**: `get_snapshot_option(underlying_asset, option_contract)`.
- **Full chain**: `call_api(path="/v3/snapshot/options/{underlyingAsset}")` — one call returns
  every contract for an underlying with quote, trade, greeks, IV, open interest, and the
  underlying's own price/break-even. Filter with `strike_price`, `expiration_date` (or
  `.gte`/`.lte` variants), `contract_type` params. `limit` defaults to 10, max 250 — page with
  the returned `next_url`.
- **Anything else** (contract reference lookup, historical options aggregates via REST):
  `search_endpoints(query="...", market="Options")` to find the exact path, then `call_api`.

Chains/snapshots aren't a flatfile product — don't go looking for them in the corpus tools.

## Bulk historical trades/quotes/aggs — on-demand S3 flatfiles

`list_flatfile_asset_classes`/`list_flatfile_data_types(asset_class="us_options_opra")` shows
`day_aggs_v1`, `minute_aggs_v1`, `quotes_v1`, `trades_v1` — all still live on Massive's S3,
just never ingested into the NAS corpus. `download_flatfile` to pull a specific day. See
`massive-flatfiles` for the general S3 mechanics.

⚠️ quantum-data's own RAW ingestion of `us_options_opra` (day_aggs/minute_aggs/trades, no
quotes) stopped 2026-06-02 and lives at `/mnt/store/zpolygon` (server5, mounted into the MCP
container at `/mnt/server5/zpolygon`) — reachable read-only via `resolve_raw_path`/
`get_raw_file_info`/`read_raw_rows` for dates up to 2026-06-02 only. For anything newer, use
the S3 flatfiles above.
