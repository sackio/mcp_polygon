---
name: massive-features
description: Find and use the quantum bar/label/indicator/mask feature corpus for training and testing — historical files from quantum-data and the same features live from quantum-engine. Use whenever a task needs ML features, training data, indicator history, signal masks, forward-return labels, or backtest/live feature parity. Trigger phrases: features, training data, indicator history, mask history, signal masks, labels, forward returns, bar spec, spec_id, emit_seq, manifestgrid, qf-data, zquantum, xsect, cross-sectional features, train/test features, live features, feature parity.
---

# massive-features

Authorized by Ben 2026-10-03 (ask `a8`, #massive). Everything below is **peer-reported**
(quantum-data DM 2026-10-03 16:51 EDT, quantum-engine DM 2026-10-03 16:51 EDT) except where marked
MEASURED. Owners: **quantum-data** = historical bars + labels; **quantum-engine** = live stream;
indicator/mask computation = qf-library (see `massive-quantum-library`). This seat does not own
or edit any of it. Re-verify counts and dates before quoting them.

⛔ **There are NO indicator or mask FILES.** quantum-data publishes **bars and labels only**.
Indicators and masks exist (a) live on NATS, and (b) computed on demand from bars with qf-library.

## Historical — bars (quantum-data)

Root (server5 local ZFS): `/mnt/store/zquantum/qf-data/` — the same export is `/mnt/server5/zquantum/qf-data/`
from server4. **Not on the NAS.** MEASURED 2026-10-03: both names resolve on server5; the
single-ticker root has `SPEC-LIST.txt` with 988 lines.

| lane | root dir | shape |
|---|---|---|
| single-ticker (ST) | `bars-manifestgrid1114-i9b9a-20260702` | 988 specs, 13,575+ US stock tickers, 30 shards, **unmerged** |
| multi-ticker + index (MT) | `bars-manifestgrid1114-mt-iaec2-20260702` | 128 specs, fixed 25-name roster, merged: `bars/<YYYY-MM-DD>.parquet` |

- ST layout: `<root>/batch-<YYYYMMDD>-r0-<run>/s<N>/bars-qf-egress-<N>.parquet`, N=0..29.
  A ticker lives in exactly one shard: `name_shard(ticker, 30)` = FNV-1a + Fibonacci mix % 30
  (qf-engine `symbols.rs:133`). **Compute the shard; never list directories to find it.**
  Beta reference funds are replicated into every shard. ~105M rows/shard, ~78 GB/day.
- Per day: `<root>/<YYYY-MM-DD>/MANIFEST.json` (day, batch_dir, image, specs_sha256, spec_count,
  shard_count, rows_total, silent_specs, shards). `.done` = day complete.
- Coverage: starts 2026-07-02; the end date moves (a daily increment was built but not live when written,
  2026-10-03) — read the current span from `list_feature_corpus`, don't trust a date written here.
- Schema (36 cols, both lanes): `spec_id, symbol, market`, `open/high/low/close` (nullable double),
  `volume, transactions`, `emit_seq` (row key within spec_id+symbol), `start_ts_ns, end_ts_ns`
  (epoch ns UTC — **inferred**, engine to confirm), `oc_absent, hl_absent`, open_/close_ bid/ask/size/
  quote_ts_ns, `metric, form_t_trades, max_skew_ns, late_volume, nbbo_inherited`, microstructure
  (`adverse_selection, effective_spread, kyle_lambda, quote_imbalance, quoted_spread,
  realized_spread, signed_volume`).
- Spec semantics and params belong to quantum-engine. Catalog: `<root>/SPEC-LIST.txt`.

## Historical — labels (forward-return targets)

Key `(spec_id, symbol, emit_seq)` — joins to bars. **Join on `emit_seq`, not timestamps.**

- ST current: `labels-manifestgrid1114-i9b9a-20260702-h3/labels/<day>/s<N>.parquet` — horizons
  h2/h16/h128, zstd. **In progress** (47 days; done ~Oct 8-9).
- ST reference: `…-i9b9a-20260702/labels/<day>/s<N>.parquet` — 8 horizons + `end_ts_ns`, snappy, 13 days.
- MT: `labels-manifestgrid1114-mt-iaec2-20260702` — old 8-horizon format, 60 days.
- ⚠️ **Two label schemas coexist** until the h3 compaction. Any horizon can also be computed on
  read from bars (quantum-lab's reader matched stored labels bit-for-bit on 2026-07-08/s0).

## Read gotchas (quantum-data)

Project columns (shards ~100M rows). Filter `spec_id` first. Bars carry **no RTH filter**
(extended hours included). Use `emit_seq` to join.

## Live — quantum-engine (all 18 shards)

NATS core `nats.quantum-feed.svc:4222`, **ClusterIP only** — no NodePort, no creds; from outside the
cluster use `kubectl port-forward`. No JetStream, no retention; a slow subscriber is dropped silently.
Encoding **MessagePack with named fields (not JSON)**.

- Bars: see `massive-live`.
- Indicators: `market.<stocks|crypto>.indicator.<indicator_key>.<ticker>`; payload one `IndicatorValue`
  (`qf-types/src/protocol/messages.rs:1241`): signal_id, indicator_key, ticker, bar_spec_id, ts_ns,
  value, columns[≤4], warmed_up. One value per message.
- Masks: `market.<cls>.mask.<surface>.<ticker>`; the subject names the SURFACE; payload an ARRAY of 12
  `SignalEvent` (`messages.rs:1267`): signal_id (= mask_id), ticker, ts_ns, mask, direction.
  Roster: `/mnt/nas/data/quantum/lab/masks/roster/mask-universe-v1.2-casefix.tsv` (5,736 rows = 478 surfaces × 12; `.sha256` beside it; supersedes `mask-universe-v1-3e2248256f31.tsv`, 2,820 rows re-spelled in the registry-casing fix). ⚠️ quantum-lab says the live bank enrolls by registry key, so 2,488 masks may never have been built live under the old spellings — peer-reported, unverified; ask quantum-engine.
- Cross-sectional (NEW 2026-10-03): `market.<cls>.xsect.<key>.<spec>`; one array per CS key per row,
  joined by `roster_digest` to `market.<cls>.xsect.roster.<spec>`; specs ≥1m only, 87 keys. Crypto
  started 16:45 ET 10-03, no value yet confirmed on the wire; stocks from Monday's open.
- ⛔ **Subscribe NARROW, through ONE relay.** Never `market.*.indicator.>`: indicators run at tens of
  millions of msgs/hour and the broker host already drops packets at its receive ring. Use
  `indicator.<key>.*` or a ticker list. Scope = whatever prints (no allow-list); derive the spec set
  with `indicator-census --groups`, do not quote a count.

## Parity (historical vs live)

Same engine code and specs as quantum-data's runs (historical image
`qf-engine@sha256:aec20e69…`, batch-9b9aadb53; whether live runs that exact image is quantum-engine's
to state). **Live ≠ flatfile byte-for-byte** (vendor arrival order vs sorted; fractional `ds` sizes
live only; image versions). **Streaming≡batch equality against a live session is UNPROVEN** — do not
assume features trained on files will reproduce live to the bit; measure on your own model.

## Standing corpus universe (quantum-lab) — v1.3.2, surface files NOT READY as of 2026-10-04

Owner quantum-lab (thread-quantum-lab-1); spec `/mnt/nas/data/quantum/lab/universe/UNIVERSE-v1.md`.
Ben (2026-10-04 11:40, #quantum-lab-search) made THIS seat responsible for telling tradedesk seats
about the corpus, esp. the model-based RL seat (thread-tradedesk-10, projects/rl), once it is ready.
- Bars: all 1,114 specs (`bars-manifestgrid1114-i9b9a-20260702`) + index/multi-ticker specs.
- DRIVER set (bar specs that indicators and masks are computed on): `bars-driver-v2.2.txt` = 322 =
  301 independent (v2, cut on indicator-output correlation) + 6 per-ticker `betarole_*` + 9 index +
  6 cross-asset beta. Supersedes the 220/248-spec lists. Per-ticker beta live for crypto now, stocks
  from Monday 2026-10-05 open (engine); "beta for every ticker" rung `betarole_{market,index,sector}_1m_w30_e1_ols` is live on
  market.*.bar (engine fc13516a1, peer-reported): catch-all for tickers too thin for the 1s/5s w200 rungs; crypto has only the market role.
- Indicators: 389 per-ticker surfaces (`indicator-surfaces-v1.2.txt`, registry-cased) + 87 cross-sectional
  alphas on specs ≥1m (pinned qf-library `3a8e93152`; anything earlier is void; generate split by spec).
- Masks: 4,304 (`masks-v1.2.txt`); roster `masks/roster/mask-universe-v1.3.tsv`.
- HISTORICAL v2 surfaces STARTED (quantum-lab, peer-reported; split across stores per Ben): find files ONLY through the index `/mnt/server6_store/zquantum/qf-data/surfaces-v2-st-20260702/INDEX.tsv` (cols day, shard, store, indicators path, masks path; a row appears only when that shard-day is complete). MT and betarole roots follow in the same format. Older `-drv2*`/`-drvcore254`/`-drvadd158` roots are superseded. Count index rows for progress; do not glob the directories.
- LIVE ADDITIONS (quantum-lab, peer-reported; UNIVERSE-v1.md §v2.1): cross-sectional alphas, 87 keys, stocks and crypto: `market.<class>.xsect.<key>.<spec>`, joined by `roster_digest` to `market.<class>.xsect.roster.<spec>`; driver list now v3.2 (436 specs). HISTORICAL surface backfill is PAUSED pending Ben's storage decision.
- CORPUS LISTS v2 (Ben approved; quantum-lab, peer-reported): indicators lab/universe/indicator-surfaces-v2.txt = 473 (param variants spelled `key?param=value`), masks masks-v2.txt = 9,595; details UNIVERSE-v1.md §v2.0. Historical is being REGENERATED on v2. Live stays on v1.2 (389 indicators / 4,304 masks) until engine switches; the `lists` digest in each live row says which list produced it.
- Historical surface files (quantum-data, 60 days, RUNNING, not ready; data will DM when complete):
  `{indicators,masks,state}/<day>/s<N>.parquet` under `/mnt/server6_store/zquantum/qf-data/`:
  `surfaces-manifestgrid1114-i9b9a-20260702-drvcore254` + `…-drvadd158` (union = lab driver v3, 412 specs; {indicators,masks}/<day>/s<N>.parquet; backfill in progress; old `-drv2`/`-drv2betarole` roots are superseded, incomplete),
  `surfaces-manifestgrid1114-mt-iaec2-20260702-drv2mt`. The server4 root is VOID. NOTE the new
  `/mnt/server6_store` location (bars/labels above are on server5 `/mnt/store/zquantum`).
- Live (quantum-engine): built on v2.2. Not complete until engine says it serves the full set.
- "KEEP" in the spec = not ruled out on structure; nothing has been measured for profit.
- Per-ticker beta/driver counts here are peer-reported; the spec file is authoritative.

## MCP tools (live since 2026-10-04 11:52 ET) — bars + labels only

`list_feature_corpus` (lanes, day coverage, spec counts) · `list_feature_specs(lane, contains, limit)` ·
`resolve_feature_file(kind, lane, day, ticker)` (path + parquet metadata; shard computed with
name_shard) · `read_feature_rows(kind, lane, day, ticker, spec_id, columns, limit<=20000)`.
kind `bars` (lane `st`|`mt`) or `labels` (lane `st_h3`|`st_ref`|`mt`). Code: `src/mcp_massive/features.py`.
- MEASURED: shard hash reproduces quantum-data's layout (AAPL→s13, SPY→s17, X:BTC-USD→s8); a
  bounded read of AAPL/time_1m (3 rows) took 1.1 s through the live MCP. Row groups are NOT sorted by
  spec/symbol, so a read with no early stop scans key columns of ~90 row groups (INFERRED ~10-15 s);
  scans abort at 120 s rather than return a biased subset.
- Container mount: `/mnt/server5/zquantum/qf-data` read-only (docker-compose.yml). Surface files
  (indicators/masks/state, server6) are NOT mounted or served yet.
- ⚠️ ABSENT OHLC IS NULL IN THE PARQUET CORPUS, not 0.0: where `oc_absent`/`hl_absent` is true, open/high/low/close
  are NULL (quantum-data: all 197,703 `oc_absent` rows on 2026-07-02 s7 rg0; tradedesk-10 measured; MEASURED here:
  AAPL/time_1m 2026-09-25 first row has all four OHLC None). Guard with `isnull`/`oc_absent`, never `close == 0.0`.
  The `massive-live` skill's "0.0 sentinel" line describes the live wire (NATS msgpack), not these files — whether
  the wire really uses 0.0 is quantum-engine's to confirm.
- `close_bid`/`close_ask` are NULL on ~0.92% of parquet rows, independent of `oc_absent` (9,511 of 9,599 have the flag false); engine: the NULL is itself the flag, there is no other (tradedesk-10 measured).
- Day 2026-09-25 file contains a bar whose start_ts_ns precedes the session day (state carried from the
  prior day): do not assume every bar in a day file starts on that date.

## Live per-ticker beta bars (quantum-engine DM 2026-10-04 11:47)

`market.<stocks|crypto>.bar.betarole_<market|index|sector>_<1s|5s>_w200_e1_ols.<TICKER>` on
`nats://<any node>:30422` (nats-external) or in-cluster `nats.quantum-feed.svc:4222`. MessagePack Bar:
beta = close (= open/high/low), alpha = `metric`, transactions = observations in the window (200-obs OLS
on paired 1s/5s bars) vs market (SPY; BTC-USD for crypto), the ticker's index ETF, its sector ETF.
A role exists only if refdata `beta_reference/current.json` assigns one (every ticker gets market).
Crypto verified on the wire 10-04; stocks from Monday 10-05 open. 1-minute beta (`…_1m_w30_e1_ols`) not live.
NOT live yet: the v2.2 feature universe as one row per driver bar on `market.<class>.surface.<spec>.<ticker>`
(values + fire/eval bit-planes + list digest); engine will DM the schema when it serves.

## LIVE universe surface rows — serving (quantum-engine DM 2026-10-04 16:38 ET)

All 256 buckets have a consumer, stocks + crypto. NATS `nats://192.168.1.168:30425` (Service externalTrafficPolicy Cluster, so any node IP serves it; MEASURED INFO
received from server5 2026-10-04). ⛔ server5 cannot reach NodePorts on its own IP (192.168.1.42) — use .168 from s5;
in-cluster `nats://qf-surface-rows-nats.quantum-feed.svc.cluster.local:4222`. Subject
`market.<stocks|crypto>.surface.<spec_id>.<ticker>` — ONE row per driver bar (driver list v3.1, 433 specs;
supersedes v2.2's 322): every universe indicator and mask computed on that bar. MessagePack, named fields:
`spec_id, symbol, emit_seq` (= Bar.series_seq = parquet emit_seq), `end_ts_ns`, `fed` (false ⇒ bar skipped:
all values NaN, all bits 0), `values` f32[389] in `indicator-surfaces-v1.2.txt` order (NaN = none),
`fire`/`eval` bytes for 4,304 masks in `masks-v1.2.txt` order (mask m = byte m/8, bit m%8, LSB-first;
⛔ `fire` is meaningless where `eval`=0), `lists` = digest of the three lists (changes ⇒ list order changed).
- Live is the same SurfaceStream object surface-day runs: a live row and a batch row match on
  (spec_id, symbol, emit_seq). Parity check (U4) NOT yet run.
- Grid driver bars (g0_*, not on market.*.bar): `nats://192.168.1.168:30424`,
  `market.<class>.ubar.<bucket>.<spec>.<ticker>`, Bar MessagePack with short keys k/y/o/h/l/c/v/n/s/e/sq.
- Row lag: warm 1-4 s, may lag at the close. State is per process: a pod restart starts series cold.
- `betarole_*` duplicate copies on market.*.bar (~4.6%) were FIXED by quantum-engine at 17:19 ET on 2026-10-04 (measured warm: 0 dupes over 120 s); no dedupe needed for bars produced after that. Surface rows: bars in == rows out on all 69 pods.
- Historical surface files (server6) still running; the live stream is the only served surface path today.

## GPU batch indicators (quantum-library, Rust only)

Peer-reported by quantum-library 2026-10-03 (origin/master, last gpu commit `42ae45b8b` as of 2026-10-04). ⛔ **Not
reachable from Python or `qf_bindings`** — there is no GPU entry point there. Rust crate feature
`gpu` (default OFF). Batch only: no streaming, no save/load state, no masks, no cross-sectional
(wqa101_cs) surfaces.

- API: `Gpu::new(ordinal)`; `Ohlcv{open,high,low,close,volume: &[f64]}` (equal lengths; NaN close =
  absent bar); `gpu.batch(key, &[Ohlcv], period) -> Vec<Vec<f64>>` (primary output);
  `gpu.batch_outputs(...)` (primary then aux columns, CPU order); `gpu::GPU_SURFACES` lists what has
  a kernel — any key not in it errors "no GPU kernel". One period per launch; other params stay at CPU defaults.
- 186 surfaces on origin at `6b0f8f057` (every wqa101_ts alpha now on GPU; added pandas_ta KST(+signal) CTI KDJ SKEW KURTOSIS KVO ABERRATION(4 cols) ACCBANDS; wqa 2/29/81/84 use log/pow = gpu::TRANSCENDENTAL, max rel <=3.8e-16, all others byte-identical to CPU; 152 at `8ca616fa5`, added wqa101_ts 1,3-8,10,13-22,25-28,30-32,34-45,47,50,52,54-57,60, byte-identical to CPU; 108 at `0564304b2`, added pandas_ta TR ATR VORTEX DONCHIAN UI PGO EOM KC MASSI AO TSI(tsi,signal) COPPOCK, byte-identical to CPU; 96 at `42ae45b8b`, 89 at `d3ff86c12`, 49 before; added pandas_ta PVT ZSCORE SLOPE FWMA HMA ZLMA OBV, byte-identical; a parameter the CPU refuses now returns Err from `batch_outputs`). Earlier 89-surface breakdown: talib set + pandas_ta (bind `length`: HLC3 WCP BIAS DPO
  MAD VWMA SWMA PWMA VHF NVI PVI QSTICK CMF EFI) + wqa101_ts (9 12 23 24 33 41 46 49 51 53 101) + alias
  spellings (technical: MOM ROC WILLIAMS_PERCENT ATR_PERCENT DEMA STOCH BOLLINGER_BANDS MACD; pandas_ta:
  MOM MIDPOINT MIDPRICE TEMA MFI VARIANCE UO). Read `gpu::GPU_SURFACES` for the authoritative list.
- Parity: +,-,×,÷,√ surfaces are byte-identical to CPU batch (`--fmad=false`); only `talib:LINEARREG_ANGLE` is transcendental and is held to rel diff ≤ 1e-12. Live engine runs CPU streaming ≡ CPU batch, so
  exact GPU surfaces match live by that chain; no gate compares GPU output to live directly.
- Hardware: NVIDIA + CUDA 12.x driver at run time. Build image `192.168.1.207:5050/qf-library-gpu:2026-09-29`.
  qf-library's card is gpu2 (RTX 3060 12 GiB) in ns `qf-library`; other agents need their own GPU
  allocation from `cluster` or `models`. Never gpu1 (whisperx). No env vars.
- Limits: no chunking (caller splits series to fit); speedup comes from number of series, not length.
- Example: `let g = qf_library::gpu::Gpu::new(0)?; g.batch("talib:RSI", &[s], 14)?`

## Who to ask

quantum-data (files, coverage, increments) · quantum-engine (live subjects, spec semantics, parity) ·
quantum-lab (consumer; qf-search, bar-label reader) · this seat (MCP tooling, vendor questions).
