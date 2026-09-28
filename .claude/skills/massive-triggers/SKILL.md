---
name: massive-triggers
description: Register a live alert on quantum-engine's or mind's real-time feeds through the massive/polygon MCP — bars, raw trades/quotes, tape events, or mind's news/EDGAR/earnings stream — instead of building your own NATS/SSE client or polling. Use whenever a task needs "tell me the moment X happens" on a specific ticker rather than periodic polling. Trigger phrases: register a trigger, live alert, register_live_alert, watch for a condition, tell me when, alert me when, earnings drop alert, tape event trigger, price trigger, watchlist alert, notify_to.
---

# massive-triggers

v1, built 2026-09-27 per Ben's directive (plan: `https://atc.sack.io/f/up-1ccffa2f7cf6b7a981c9f7ab13debc53/index.html`).
This is the consumer-facing layer over `src/mcp_massive/live_ingest.py` (quantum-engine's NATS
feed) and `src/mcp_massive/mind_ingest.py` (mind's SSE feed + earnings-announce-push sink) — see
`massive-live` for the raw wire protocol if you need it directly; this skill is for registering
a trigger through this seat instead.

## The tool

`register_live_alert(condition, notify_to, owner=None)` — `condition["kind"]` was `threshold`/
`engine_health` only; this adds a third kind, `"trigger"`, for arbitrary logic across six
sources:

```python
register_live_alert(
  condition={
    "kind": "trigger",
    "source": "quantum_bar" | "quantum_trade" | "quantum_quote" | "quantum_tape" | "mind_sse" | "mind_earnings_push",
    "spec_id": "time_1m",        # REQUIRED for quantum_bar, omit otherwise
    "event_type": "sweep",       # optional, quantum_tape only — filters the `kind` field client-side
    "tickers": ["AAPL", "MSFT"], # REQUIRED, non-empty — every trigger is scoped, never "anything"
    "expr": "close > 150 and volume > 1000000",
  },
  notify_to="your-agent-name",  # or "slack:U..."
)
```

Fires once via ATC DM to `notify_to` when `expr` first evaluates true (edge-triggered — same
semantics as the existing `threshold` kind), re-arming only after it goes false again.
`list_my_live_alerts(owner)` / `cancel_live_alert(alert_id)` manage what you've registered.
`owner` defaults to `notify_to`.

## Updating a trigger without hand-tracking its `alert_id` — the `label` field

Added 2026-09-28 for tradedesk.fundamentals' rebalance-driven books (per-ticker thresholds that
need a fresh value on a schedule, same conceptual alert). Any `condition` dict may carry an
optional `"label"` — a string you choose, stable across re-registrations. Registering again with
the same `(owner, label)` cancels the previous alert under that label first, so you get one
logical alert per label instead of an ever-growing pile of alert_ids you have to track and cancel
yourself. `register_live_alert`'s response includes `upserted_previous_alert_id` (the id that was
just replaced, or `null` on the first registration under that label). Omit `label` for the
original always-insert behavior — nothing changes for existing callers.

## Reference values `expr` can read back — `set_trigger_references`

Added 2026-09-28, Ben's directive (#tradedesk-fundamentals): *"if you need a prior close
reference you can set up code to store that for yourself and then get it — it's a good example
of something I would want them to build."* General-purpose, not prior-close-specific — any owner
can store an arbitrary named per-ticker value and have every one of their triggers see it as a
plain field.

```python
set_trigger_references(owner="me", ref_key="prior_close", values={"AAPL": 227.55, "MSFT": 510.2})

register_live_alert(condition={
    "kind": "trigger", "source": "quantum_bar", "spec_id": "time_1m", "tickers": ["AAPL"],
    "expr": "prior_close is not None and abs(close/prior_close - 1) >= 0.20"},
  notify_to="me")
```

`prior_close` (the `ref_key` you chose) appears directly in `expr`'s scope for that ticker, no
lookup syntax — same field-injection pattern as everything else in this skill. A companion
`{ref_key}_updated_unix_ns` is injected alongside it for staleness checks you write yourself (no
built-in max-age policy — this system doesn't decide what's stale, you do).

⛔ **Referencing a `ref_key` you never set for that ticker raises `NameNotDefined`, same as any
typo'd field name** — this is a normal per-event failure (see auto-disable above), not special
behavior. Always guard: `prior_close is not None and ...`, never assume every ticker has a value.

⛔⛔ **Isolation is per OWNER, not global — verified 2026-09-28.** Two different owners' triggers
on the same (source, ticker) never share reference values, even though they're evaluated from the
same underlying event in the same loop iteration internally. You only ever see your own
`set_trigger_references` writes.

`list_trigger_references(owner, ref_key=None)` reads back what's stored. Capped at
`MASSIVE_LIVE_MAX_REFERENCES_PER_OWNER` (default 20000) distinct `(ref_key, ticker)` pairs per
owner — `set_trigger_references` raises rather than silently dropping past the cap. No history:
re-calling with the same `(owner, ref_key, ticker)` overwrites, it does not append.

**This is NOT built for you** — nobody populates `prior_close` or any other ref_key
automatically. You compute the value (e.g. via `get_previous_close_agg`/`get_aggs`) and write it
yourself, on whatever schedule your use case needs (tradedesk.fundamentals' price-shock case:
daily, before market open, via the same `set_trigger_references` call, since prior close changes
every session).

## ⛔⛔ `expr` is NOT Python `eval()` — it's a restricted grammar, and one specific thing is easy to get wrong

Evaluated via `simpleeval`'s `EvalWithCompoundTypes` — comparisons (`>`,`<`,`==`,`in`, ...),
boolean logic (`and`/`or`/`not`), list/dict/tuple literals (`ticker in ['NVDA','AMD']` works).
**No attribute access, no imports, no function calls, no comprehensions.** This runs inside the
one shared FastMCP process every seat's tool calls go through — that's the whole reason it's
restricted this way, not a design preference.

⛔ **Plain `simpleeval.simple_eval` (the DEFAULT mode) rejects list/dict literals outright** —
`FeatureNotAvailable`. This bit the very first draft of this system: the exact worked example
below (`ticker in [...]`) passed syntax validation and then failed on every single real event
until the evaluator was switched to `EvalWithCompoundTypes`. Registration now feature-checks
your `expr` against the real evaluator class before accepting it, so an unsupported construct
is rejected immediately with a clear error — not accepted and left to fail silently on live
traffic. If registration succeeds, the construct is supported.

## A field is `None` when a source doesn't grade it — not zero, and your `expr` must say so

Comparing `None > 0.5` (or similar) *raises* in Python — it doesn't silently evaluate to False.
**Write `field is None or field > threshold`** if `None` should be excluded rather than crash
the comparison. A raised exception on a SINGLE event is treated as "this event doesn't match,"
not as a broken trigger — see auto-disable below for where the line actually is.

## Per-source field reference — what's in `expr`'s scope

| `source` | fields available |
|---|---|
| `quantum_bar` | `spec_id, ticker, open, high, low, close, volume, trade_count, oc_absent, hl_absent, unreliable` (same shape as `get_live_bar`) |
| `quantum_trade` | `trade_price, size, conditions, exchange, timestamp_ns` |
| `quantum_quote` | `bid_price, ask_price, bid_size, ask_size, mid_price` (computed), `timestamp_ns` |
| `quantum_tape` | `kind, trade_price, quote_midpoint, quote_level, size, strength, direction, timestamp_ns` — see the four traps below |
| `mind_sse` | whatever mind's event JSON carries for that `event_type` (varies by taxonomy type — `ticker`/`subject`, plus type-specific fields) + `received_unix_ns` |
| `mind_earnings_push` | `ticker, announced_at, ingested_at`, occasionally `period`/`matched` + `received_unix_ns` |

## ⛔⛔ `quantum_tape`'s four payload traps — resolved into the fields above, but know why

quantum-engine's wire audit (2026-09-27): one NATS subject
(`market.<mkt>.event.tape.<ticker>`) carries all six detector kinds together
(`sweep`/`absorption`/`iceberg`/`queue_depletion`/`block`/`flicker`) via the `kind` field — use
`event_type` in your registration to filter to one, or read `kind` inside `expr` yourself.

1. **The raw wire's `price` field means four different things depending on `kind`** — Sweep/Block:
   a real trade price. Absorption: the quote midpoint. Iceberg/QueueDepletion/Flicker: a quote
   level. This module resolves it BEFORE your `expr` ever runs — you get `trade_price`,
   `quote_midpoint`, and `quote_level` as three separate fields, only one of which is non-`None`
   for any given event, so you can never accidentally compare a trade price against a quote
   level. Don't look for a generic `price` field; it doesn't exist here on purpose.
2. **`strength: None` means the detector doesn't grade that kind — not zero.** See the `is None`
   guidance above; this is exactly the field it's about.
3. **`direction`, when present, is always the AGGRESSOR's side** — uniformly, across every kind
   that carries one. Not the passive side. (Quantum-engine: paraphrasing this backwards nearly
   cost a real study on 2026-09-20.)
4. **Ticker registration is auto-normalized to match the wire's own sanitization**
   (`BRK.B` → `BRK-B` — a literal dot would split a NATS subject token). Register against
   `"BRK.B"` or `"BRK-B"`, either works; you'll never see the dot form on the wire itself.

## ⛔ The 23 roster names — unresolved for trade/quote/tape too, not just bars

`massive-live` documents that single-ticker BARS on 23 names (AAPL, MSFT, SPY, sector ETFs,
etc. — see that skill for the full list) can disagree across shards with no safe client-side
fix. **Whether the same fan-out problem affects trade/quote/tape events on those same names is
UNCONFIRMED** — the measured dup problem was characterized on bars specifically. Treat a
trigger on one of these 23 names for `quantum_trade`/`quantum_quote`/`quantum_tape` as
potentially subject to the same issue until someone measures it; this is an open item, not
something this system resolves.

## Auto-disable — when a trigger stops firing on its own

A trigger disables itself and sends ONE notification to its owner (not silent, not repeated) in
two cases: (1) a single evaluation takes over ~50ms (`MASSIVE_LIVE_TRIGGER_EVAL_MAX_SECONDS`) —
a real resource-pathology signal; (2) **20 consecutive raised exceptions with zero successful
evaluations in between** — every event this trigger has ever seen failed the same way, almost
always a typo'd field name for that source. A single occasional raise (e.g. `strength` being
`None` on an ungraded tape kind, if your `expr` didn't guard for it) does NOT count toward this
— only a trigger that has NEVER once evaluated successfully gets disabled.

## Worked example: watch for earnings on your own list, without mind's bespoke pipeline

⛔⛔ **`tradedesk-earnings` already has its own working, low-latency (p50 1.98s) earnings-drop
pipeline** (mind's `earnings-announce-push.sh` → ATC DM + `announcements.jsonl` →
`thread-tradedesk-12` trading directly off it). **Do not register a trigger here for that same
purpose — it would be a second, competing path into the same downstream consumer.** This
system's earnings value is for OTHER agents who don't already have that bespoke integration:

```python
register_live_alert(
  condition={
    "kind": "trigger",
    "source": "mind_earnings_push",
    "tickers": ["NVDA", "AMD", "SMCI"],
    "expr": "ticker in ['NVDA','AMD','SMCI']",
  },
  notify_to="your-agent-name",
)
```

This reads the SAME sink mind already produces (`announcements.jsonl`, tailed as a plain file —
not a reimplementation of mind's extraction) and inherits its per-ticker-per-day dedupe. Payload
you receive is exactly `ticker` + `announced_at` — no beat/miss, that's stripped at the source on
purpose. Real traps in this specific feed (from mind directly): it's a headline regex with an
unmeasured miss rate (a floor, not a census), and items landing around 03:00 ET arrive ~1,293s
behind on average — a real, structural delay for that slice, not a bug.

## Other worked examples

```python
# A quantum bar threshold, expressed as a trigger (equivalent to the older
# {"kind":"threshold"} shorthand, which still works unchanged):
register_live_alert(
  condition={"kind": "trigger", "source": "quantum_bar", "spec_id": "time_1m",
             "tickers": ["AAPL"], "expr": "close > 150 and volume > 1000000"},
  notify_to="your-agent-name")

# A tape sweep on a specific name, guarding None correctly:
register_live_alert(
  condition={"kind": "trigger", "source": "quantum_tape", "event_type": "sweep",
             "tickers": ["BRK.B"],
             "expr": "trade_price > 100 and (strength is None or strength > 0.5)"},
  notify_to="your-agent-name")
```

## Not available — out of scope for v1, don't assume otherwise

- **Indicators**: `QF_PUBLISH_INDICATORS=0` on the live tier — nothing is published on
  `market.<mkt>.indicator.>` today, by config, not by wire limitation. Enabling it is a live-tier
  change with an unmeasured resulting message rate; needs Ben's explicit sign-off, not assumed
  here. Until then, an indicator-based trigger isn't offered by this system — compute it
  yourself from `quantum_bar` events via `qf_bindings`/`qf_library` (`massive-quantum-library`
  skill).
- **Signal masks**: never published anywhere on the engine (confirmed against source,
  `bin/mask-census.rs` is the only place `SignalSpec`/`create_signal` appear, and it's an
  offline census binary) — same as indicators, compute client-side if you need one.
- **Weekday equities recheck**: the wire audit this system's tape/trade/quote handling is built
  on was measured Sunday, crypto-only — `market.stocks.*` was legitimately absent (market
  closed), not confirmed working. Equities-specific triggers on these three sources haven't been
  exercised against real weekday stock traffic yet.

## Who owns what

This seat (`massive`/`polygon`) built and owns this trigger layer. `quantum-engine` owns the
underlying NATS wire (see `massive-live` for anything about the wire itself). `mind` owns the
SSE feed and the earnings-announce-push sink (see `massive-mind`) — this system reads both,
neither is owned or modified here.
