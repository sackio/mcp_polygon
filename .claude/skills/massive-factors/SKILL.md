---
name: massive-factors
description: Asset-pricing factor model returns (Fama-French 3/4/5/6, Carhart, q-factors, AQR HML-Devil/QMJ/BAB/VME/6-factor, mispricing, liquidity, DHS, ICR, Barillas-Shanken) and exchange market calendars/holidays/sessions from the shared Massive MCP. Use for factor regressions, alpha/beta attribution, risk-free rate, trading-day calendars, holidays, early closes. Trigger phrases: factor model, Fama-French, Carhart, momentum factor, SMB, HML, RMW, CMA, q-factor, AQR factors, risk free rate, market holidays, trading calendar, early close, trading sessions.
---

# massive-factors

Both are served by the shared MCP (`http://server4:24400/mcp/v1`). Neither comes from Massive's API.

## Factor models
- `list_factor_models` → 18 keys (ff3 ff4 ff5 ff6 q qc hmld qmj bab vme aqr6 mis liq icr dhs bs hcapm pcapm) with aliases.
- `get_factor_model(model, region='usa', frequency='m', start_date, end_date, limit=1000, tail=False)` → rows plus `first_date`, `last_date`, `total_rows`. Cap 5,000 rows per call; `tail=true` returns the newest rows.
- Source: our fork `github.com/sackio/getfactormodels` (checkout `/mnt/nas/data/code/forks/getfactormodels`), pinned to commit `b9675ab` in `pyproject.toml` by tarball URL (container has no git). Data is fetched LIVE from the authors' sites (Ken French, AQR, global-q.org); nothing is stored here.
- ⛔ **Data currency is per model and moves**: a factor set that ended years ago joins silently to recent returns and drops the unmatched rows. Always read `last_date` before a regression. (Measured at install: ff3 monthly to 2026-08; vbt's header in `scripts/requirements/factors.txt` lists per-model ends, e.g. Stambaugh-Yuan mispricing stops 2016-12.)
- ⛔ Upstream is pre-alpha; the fork carries 3 fixes (int `model=`, string-dtype index, check_connection). Bump the pin deliberately and tell `vbt`, whose image pins the same commit. Never force-push the fork.
- Returns are in decimal (0.0256 = 2.56%) as served; verify units per model before use.

## Calendars
`get_market_holidays` (Massive REST), `list_trading_calendars`, `get_trading_sessions` (pandas_market_calendars, 211 exchanges, open/close incl. early closes).
