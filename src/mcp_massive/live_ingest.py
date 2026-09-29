"""Background consumer for quantum-engine's live NATS bar system, plus a
persistent alert registry evaluated against it.

⛔ This is a DIFFERENT data path from everything else in this repo: REST
(server.py), S3 flatfiles (flatfiles.py), and quantum-data's NAS corpus
(corpus.py) all answer "what has already happened." This module answers
"what is happening right now" by holding a live subscription open to
quantum-engine's broker for the life of the process — it owns no data itself,
it is a cache in front of someone else's feed.

Wire protocol reference: .claude/skills/massive-live/SKILL.md (owned by
quantum-engine, not this seat). Confirmed live against the real broker
2026-09-27 from server5 (bar/event/meta subjects all decode; the skill's
documented field names are long-form aliases quantum-engine uses in prose —
the actual wire keys are the short msgpack field names below, taken directly
off real traffic, not the skill's prose).

⛔⛔ THE 23 ROSTER NAMES: single-ticker bars on these names can disagree on
CLOSE PRICE across shards (up to 34.9bps measured, massive-live skill) and
there is no known client-side fix. `unreliable` on a cached bar entry is set
whenever the ticker is one of these AND the spec_id is not a confirmed
cross-asset spec (checked against the live self-description's own
`specs.stage1_cross_asset`, never a hardcoded spec-id list — the skill is
explicit that spec-id membership changes with every engine image).
"""
import ast
import asyncio
import json
import logging
import operator
import os
import sqlite3
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
import msgpack
import nats
from simpleeval import EvalWithCompoundTypes, FeatureNotAvailable, NameNotDefined

logger = logging.getLogger("mcp_massive.live_ingest")

# The quantum-feed ClusterIP, confirmed reachable directly from server5 (and,
# per massive-live, server3/server4) without going through the NodePort the
# skill also documents — that NodePort's DNS name ("nats-external") doesn't
# resolve on this LAN as of 2026-09-27, so it is not used as the default.
NATS_URL = os.environ.get("MASSIVE_LIVE_NATS_URL", "nats://10.152.183.166:4222")

# The ATC broker's plain HTTP endpoint (server/src/messages.ts, POST
# /messages) — used instead of the atc_reply/atc_send MCP tools, which are
# only reachable from an interactive Claude Code session, not from a plain
# asyncio task inside this server process.
ATC_URL = os.environ.get("MASSIVE_LIVE_ATC_URL", "http://192.168.1.168:3030")
ATC_FROM = os.environ.get("MASSIVE_LIVE_ATC_FROM", "massive-live-alerts")

ALERTS_DB_PATH = os.environ.get("MASSIVE_LIVE_ALERTS_DB", "/app/.cache/live_alerts.db")

HISTORY_LIMIT = 500
ENGINE_HEALTH_TICK_SECONDS = 5

# ⛔ ROOT CAUSE of the 2026-09-28 OOM (mcp_polygon_server hit 59.9GB, killed by
# `system`): HISTORY_LIMIT bounds each (spec_id, ticker) key's deque, but
# nothing previously bounded the NUMBER OF KEYS — quantum-engine discovers
# tickers on first print and evicts nothing (massive-live skill), so
# _state.bars/_state.history grew one entry per distinct (spec_id, ticker)
# ever seen, forever, for the life of the process. This caps total tracked
# keys with LRU eviction (oldest-updated key dropped first) instead. Sizing:
# HISTORY_LIMIT(500) * MAX_TRACKED_KEYS entries, each entry roughly ~1KB
# (payload fields duplicated once into `raw`) -> ~500KB/key -> ~2GB at the
# default below. This is an ESTIMATE, not a measured entry size.
MAX_TRACKED_KEYS = int(os.environ.get("MASSIVE_LIVE_MAX_TRACKED_KEYS", "4000"))

# Confirmed independently outside these 23, dup ratio is exactly 1.0000 over
# 853k rows (massive-live skill, 2026-09-22). Do not add to or infer this set
# from anything at runtime — it's structural (cross-asset basket fan-out to
# all 18 shards), not a live property the wire reports.
ROSTER_23 = frozenset({
    "AAPL", "AMZN", "GOOGL", "JNJ", "JPM", "META", "MSFT", "NVDA", "QQQ", "SPY",
    "TSLA", "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU",
    "XLV", "XLY", "XOM",
})

_NUMERIC_FIELDS = {"open", "high", "low", "close", "volume", "trade_count"}
_OPS = {
    ">": operator.gt, "<": operator.lt, ">=": operator.ge,
    "<=": operator.le, "==": operator.eq, "!=": operator.ne,
}

# v1 generalized trigger API (Ben, #massive 2026-09-27, plan
# https://atc.sack.io/f/up-1ccffa2f7cf6b7a981c9f7ab13debc53) — a third condition
# kind alongside threshold/engine_health, evaluating an arbitrary restricted
# expression against a normalized event envelope from any of five sources.
_TRIGGER_SOURCES = {"quantum_bar", "quantum_trade", "quantum_quote", "quantum_tape", "mind_sse"}
_TRIGGER_EVAL_MAX_SECONDS = float(os.environ.get("MASSIVE_LIVE_TRIGGER_EVAL_MAX_SECONDS", "0.05"))
# A single raise is usually normal (e.g. `strength > 0.5` on a tape kind that
# doesn't grade — see the tape section below), not evidence the expr is
# broken — only disable after this many CONSECUTIVE raises with zero
# successful evaluations between them, which means every event this trigger
# has ever seen failed the same way (a typo'd field name, not a None).
_TRIGGER_MAX_CONSECUTIVE_ERRORS = 20
# 2026-09-28: a single slow eval used to disable immediately — wrong. Found
# live, tradedesk.fundamentals alert 9a3ea8e5 (a 6-comparison boolean expr,
# nothing pathological) disabled on one 223.6ms eval, then again (as 6ff033fc)
# on one 364ms eval ~90min later, recurring — that's shared-process contention
# (this evaluator runs inside the one FastMCP process every seat's tool calls
# go through), not evidence THIS expr is slow. Require N consecutive
# over-budget evals, same doctrine as the error counter above, before
# disabling — a real pathological expr (an accidental O(n^2), a huge dict)
# will still hit this fast since it's slow on every event, not just once.
_TRIGGER_MAX_CONSECUTIVE_SLOW = int(os.environ.get("MASSIVE_LIVE_TRIGGER_MAX_CONSECUTIVE_SLOW", "5"))


def _sanitize_ticker(ticker: str) -> str:
    """Match the wire's own subject sanitization so a trigger registered
    against "BRK.B" still matches the "BRK-B" subject/symbol it will actually
    see — a dot in a NATS subject token splits it into extra tokens and a `*`
    wildcard stops matching, which is why quantum-engine sanitizes at publish
    time (massive-live skill / quantum-engine's wire audit, 2026-09-27)."""
    return ticker.replace(".", "-")


class _LiveState:
    def __init__(self) -> None:
        # OrderedDict, not dict: _on_bar uses move_to_end()+popitem(last=False)
        # for LRU eviction across both, keyed identically since they're always
        # written together (see MAX_TRACKED_KEYS above).
        self.bars: "OrderedDict[Tuple[str, str], dict]" = OrderedDict()
        self.history: "OrderedDict[Tuple[str, str], Deque[dict]]" = OrderedDict()
        self.engine_instances: Dict[str, dict] = {}
        self.cross_asset_spec_ids: set = set()
        self.event_count = 0
        self.indicator_count = 0
        self.last_event_unix_ns: Optional[int] = None
        self.last_indicator_unix_ns: Optional[int] = None


_state = _LiveState()

# (spec_id, ticker) -> [alert row, ...], rebuilt on every register/cancel so a
# hot bar path never touches SQLite.
_threshold_index: Dict[Tuple[str, str], List[dict]] = {}
_engine_health_alerts: List[dict] = []
# (source, ticker) -> [trigger alert row, ...] — the generalized "trigger" kind.
# A quantum_bar trigger also carries its own spec_id inside condition and is
# filtered on it inline (see _evaluate_trigger_alerts) rather than folded into
# the key, so one index shape covers every source.
_trigger_index: Dict[Tuple[str, str], List[dict]] = {}
# alert_id -> consecutive-raise counter, reset to 0 on any successful
# evaluation. In-memory only — a restart re-arms every trigger's counter,
# which is fine, this is a resource-pathology guard, not a durable record.
_trigger_error_counts: Dict[str, int] = {}
# alert_id -> consecutive-over-budget-eval counter, reset to 0 on any eval
# under _TRIGGER_EVAL_MAX_SECONDS (success or raise, latency is orthogonal to
# correctness). Same in-memory-only reasoning as the error counter above.
_trigger_slow_counts: Dict[str, int] = {}
# (owner, ticker) -> {ref_key: value, f"{ref_key}_updated_unix_ns": ts, ...} —
# per-owner reference values (Ben, #tradedesk-fundamentals 2026-09-28: "if you
# need a prior close reference you can set up code to store that for yourself
# and then get it"). Bounded by MAX_REFERENCES_PER_OWNER at write time, unlike
# the 2026-09-28 OOM's unbounded key growth — this key space is entirely
# driven by explicit, intentional writes from a known set of callers, not by
# whatever tickers the market happens to print.
_reference_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
MAX_REFERENCES_PER_OWNER = int(os.environ.get("MASSIVE_LIVE_MAX_REFERENCES_PER_OWNER", "20000"))

# ⛔ (alert_id, ticker) -> satisfied — 2026-09-28 correctness fix. The "trigger"
# kind's edge-trigger state used to live on the shared alert `rec` dict's
# "currently_satisfied" key, which is correct for `threshold` (inherently
# single-ticker) and `engine_health` (inherently alert-global) but WRONG for
# `trigger`: one alert can be scoped to hundreds of tickers, and every ticker
# shared that ONE flag. Found live in production (KOD +158%, tradedesk.
# fundamentals, alert 3d065eb6): any OTHER ticker's non-match reset the shared
# flag, so KOD's alert both re-fired every minute it stayed satisfied AND
# could have silently swallowed a genuine fire for a different ticker that
# happened to evaluate while the flag was already True from KOD. Per-ticker
# state fixes both directions at once.
_trigger_ticker_state: Dict[Tuple[str, str], bool] = {}

_db_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        path = Path(ALERTS_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        _db_conn = sqlite3.connect(str(path), check_same_thread=False)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_alerts (
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                notify_to TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                created_unix_ns INTEGER NOT NULL,
                last_fired_unix_ns INTEGER,
                fire_count INTEGER NOT NULL DEFAULT 0,
                currently_satisfied INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 2026-09-28: added to an existing table, so CREATE TABLE IF NOT
        # EXISTS above won't add it on an already-migrated DB — ALTER TABLE
        # ADD COLUMN isn't itself idempotent in sqlite, hence the try/except.
        # Set only by _disable_pathological_trigger; an owner's own
        # cancel_alert()/label-upsert leaves this NULL, which is exactly the
        # distinction list_alerts uses to decide what's worth surfacing.
        try:
            _db_conn.execute("ALTER TABLE live_alerts ADD COLUMN disabled_reason TEXT")
        except sqlite3.OperationalError:
            pass
        _db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trigger_ticker_state (
                alert_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                satisfied INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (alert_id, ticker)
            )
            """
        )
        _db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trigger_references (
                owner TEXT NOT NULL,
                ref_key TEXT NOT NULL,
                ticker TEXT NOT NULL,
                value REAL NOT NULL,
                updated_unix_ns INTEGER NOT NULL,
                PRIMARY KEY (owner, ref_key, ticker)
            )
            """
        )
        _db_conn.commit()
    return _db_conn


def _validate_condition(condition: Dict[str, Any]) -> None:
    if not isinstance(condition, dict) or "kind" not in condition:
        raise ValueError("condition must be a dict with a 'kind' key")
    kind = condition["kind"]
    if kind == "threshold":
        missing = [k for k in ("spec_id", "ticker", "field", "op", "value") if k not in condition]
        if missing:
            raise ValueError(f"threshold condition missing required key(s): {missing}")
        if condition["field"] not in _NUMERIC_FIELDS:
            raise ValueError(f"field must be one of {sorted(_NUMERIC_FIELDS)}, got {condition['field']!r}")
        if condition["op"] not in _OPS:
            raise ValueError(f"op must be one of {sorted(_OPS)}, got {condition['op']!r}")
    elif kind == "engine_health":
        if "max_silence_seconds" not in condition:
            raise ValueError("engine_health condition requires max_silence_seconds")
    elif kind == "trigger":
        missing = [k for k in ("source", "tickers", "expr") if k not in condition]
        if missing:
            raise ValueError(f"trigger condition missing required key(s): {missing}")
        if condition["source"] not in _TRIGGER_SOURCES:
            raise ValueError(f"source must be one of {sorted(_TRIGGER_SOURCES)}, got {condition['source']!r}")
        if condition["source"] == "quantum_bar" and "spec_id" not in condition:
            raise ValueError("trigger condition on source='quantum_bar' requires spec_id")
        tickers = condition.get("tickers")
        if not isinstance(tickers, list) or not tickers:
            raise ValueError("tickers must be a non-empty list — every trigger must be scoped, never unscoped")
        condition["tickers"] = [_sanitize_ticker(t) for t in tickers]
        try:
            ast.parse(condition["expr"], mode="eval")
        except SyntaxError as e:
            raise ValueError(f"expr is not a valid expression: {e}")
        # Feature-check against the SAME evaluator class used at event time
        # (EvalWithCompoundTypes, not plain simpleeval — that distinction is
        # exactly what would otherwise let a syntactically valid expr like
        # `ticker in ['NVDA','AMD']` pass registration silently and then fail
        # (and eventually auto-disable) on every single real event, since
        # plain simple_eval rejects list literals outright. No real field
        # names exist yet at registration time, so NameNotDefined here is
        # expected and swallowed; FeatureNotAvailable means the expr uses
        # something this evaluator can never support, regardless of data —
        # reject it now, not after it's live.
        try:
            EvalWithCompoundTypes(names={}).eval(condition["expr"])
        except FeatureNotAvailable as e:
            raise ValueError(f"expr uses an unsupported construct: {e}")
        except NameNotDefined:
            pass
        except Exception:
            pass
    else:
        raise ValueError(f"unknown condition kind: {kind!r} (expected 'threshold', 'engine_health' or 'trigger')")


def _load_alerts_into_index() -> None:
    global _threshold_index, _engine_health_alerts, _trigger_index
    threshold_index: Dict[Tuple[str, str], List[dict]] = {}
    engine_health_alerts: List[dict] = []
    trigger_index: Dict[Tuple[str, str], List[dict]] = {}
    rows = _db().execute(
        "SELECT id, owner, notify_to, condition_json, currently_satisfied FROM live_alerts WHERE cancelled = 0"
    ).fetchall()
    for alert_id, owner, notify_to, condition_json, satisfied in rows:
        condition = json.loads(condition_json)
        rec = {
            "id": alert_id,
            "owner": owner,
            "notify_to": notify_to,
            "condition": condition,
            "currently_satisfied": bool(satisfied),
        }
        if condition["kind"] == "threshold":
            key = (condition["spec_id"], condition["ticker"])
            threshold_index.setdefault(key, []).append(rec)
        elif condition["kind"] == "trigger":
            for ticker in condition["tickers"]:
                trigger_index.setdefault((condition["source"], ticker), []).append(rec)
        else:
            engine_health_alerts.append(rec)
    _threshold_index = threshold_index
    _engine_health_alerts = engine_health_alerts
    _trigger_index = trigger_index


def _load_references_into_index() -> None:
    global _reference_index
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    rows = _db().execute("SELECT owner, ref_key, ticker, value, updated_unix_ns FROM trigger_references").fetchall()
    for owner, ref_key, ticker, value, updated_unix_ns in rows:
        entry = index.setdefault((owner, ticker), {})
        entry[ref_key] = value
        entry[f"{ref_key}_updated_unix_ns"] = updated_unix_ns
    _reference_index = index


def set_references(owner: str, ref_key: str, values: Dict[str, float]) -> Dict[str, Any]:
    """Bulk upsert (owner, ref_key, ticker) -> value, e.g. set_references(
    "tradedesk.fundamentals", "prior_close", {"AAPL": 227.55, "MSFT": 510.2}).
    Every trigger `expr` owned by `owner` then sees `prior_close` (and
    `prior_close_updated_unix_ns`, for callers who want to guard staleness
    themselves) as a plain field alongside the event's own fields, scoped to
    the ticker that event is for — see _evaluate_trigger_alerts. Re-calling
    with the same (owner, ref_key, ticker) overwrites the value; there is no
    history, only the latest."""
    if not values:
        raise ValueError("values must be a non-empty {ticker: value} dict")
    sanitized = {_sanitize_ticker(t): v for t, v in values.items()}
    conn = _db()
    existing = conn.execute(
        "SELECT COUNT(*) FROM trigger_references WHERE owner = ?", (owner,)
    ).fetchone()[0]
    new_keys = sum(
        1
        for t in sanitized
        if not conn.execute(
            "SELECT 1 FROM trigger_references WHERE owner = ? AND ref_key = ? AND ticker = ?", (owner, ref_key, t)
        ).fetchone()
    )
    if existing + new_keys > MAX_REFERENCES_PER_OWNER:
        raise ValueError(
            f"would exceed MAX_REFERENCES_PER_OWNER ({MAX_REFERENCES_PER_OWNER}) for owner {owner!r}: "
            f"{existing} existing + {new_keys} new"
        )
    now_ns = time.time_ns()
    conn.executemany(
        "INSERT INTO trigger_references (owner, ref_key, ticker, value, updated_unix_ns) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(owner, ref_key, ticker) DO UPDATE SET value = excluded.value, updated_unix_ns = excluded.updated_unix_ns",
        [(owner, ref_key, t, v, now_ns) for t, v in sanitized.items()],
    )
    conn.commit()
    _load_references_into_index()
    return {"owner": owner, "ref_key": ref_key, "count": len(sanitized), "updated_unix_ns": now_ns}


def list_references(owner: str, ref_key: Optional[str] = None) -> Dict[str, Any]:
    conn = _db()
    if ref_key:
        rows = conn.execute(
            "SELECT ref_key, ticker, value, updated_unix_ns FROM trigger_references WHERE owner = ? AND ref_key = ?",
            (owner, ref_key),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ref_key, ticker, value, updated_unix_ns FROM trigger_references WHERE owner = ?", (owner,)
        ).fetchall()
    return {
        "owner": owner,
        "references": [
            {"ref_key": r[0], "ticker": r[1], "value": r[2], "updated_unix_ns": r[3]} for r in rows
        ],
    }


def _load_trigger_ticker_state() -> None:
    global _trigger_ticker_state
    rows = _db().execute("SELECT alert_id, ticker, satisfied FROM trigger_ticker_state").fetchall()
    _trigger_ticker_state = {(alert_id, ticker): bool(satisfied) for alert_id, ticker, satisfied in rows}


def _set_trigger_ticker_satisfied(rec: dict, ticker: str, satisfied: bool) -> None:
    """Per-(alert_id, ticker) edge-trigger state for the 'trigger' kind —
    see _trigger_ticker_state's module-level comment for why this can't share
    `threshold`/`engine_health`'s alert-level `currently_satisfied`. Also
    bumps the alert-level fire_count/last_fired_unix_ns on `live_alerts` when
    satisfied — those remain reasonable AGGREGATE stats across every ticker
    this alert covers, unlike the per-ticker boolean itself."""
    _trigger_ticker_state[(rec["id"], ticker)] = satisfied
    conn = _db()
    conn.execute(
        "INSERT INTO trigger_ticker_state (alert_id, ticker, satisfied) VALUES (?, ?, ?) "
        "ON CONFLICT(alert_id, ticker) DO UPDATE SET satisfied = excluded.satisfied",
        (rec["id"], ticker, int(satisfied)),
    )
    if satisfied:
        conn.execute(
            "UPDATE live_alerts SET last_fired_unix_ns = ?, fire_count = fire_count + 1 WHERE id = ?",
            (time.time_ns(), rec["id"]),
        )
    conn.commit()


def register_alert(condition: Dict[str, Any], notify_to: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """`condition["label"]` (optional) is an UPSERT key, not a validated
    field — _validate_condition doesn't know about it, it's read here only.
    When present, any existing non-cancelled alert for this OWNER with the
    same label is cancelled before the new one is inserted, so a rebalance
    (new thresholds, same conceptual alert) updates in place under a stable
    name instead of accumulating a fresh alert_id every time. No label means
    the old always-insert behavior, unchanged — this is additive, not a
    schema migration (label lives inside condition_json like everything
    else, no new column)."""
    _validate_condition(condition)
    label = condition.get("label")
    owner = owner or notify_to
    conn = _db()
    upserted_id = None
    if label:
        rows = conn.execute(
            "SELECT id, condition_json FROM live_alerts WHERE owner = ? AND cancelled = 0", (owner,)
        ).fetchall()
        for row_id, condition_json in rows:
            if json.loads(condition_json).get("label") == label:
                conn.execute("UPDATE live_alerts SET cancelled = 1 WHERE id = ?", (row_id,))
                upserted_id = row_id
    alert_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO live_alerts (id, owner, notify_to, condition_json, created_unix_ns) VALUES (?, ?, ?, ?, ?)",
        (alert_id, owner, notify_to, json.dumps(condition), time.time_ns()),
    )
    conn.commit()
    _load_alerts_into_index()
    result = {"alert_id": alert_id, "owner": owner, "notify_to": notify_to, "condition": condition}
    if label:
        result["upserted_previous_alert_id"] = upserted_id
    return result


def list_alerts(owner: str) -> List[Dict[str, Any]]:
    # cancelled=1 rows are included too, but ONLY when disabled_reason is set
    # (2026-09-28) — an owner's own cancel_alert()/label-upsert leaves that
    # NULL and stays invisible as before; an auto-disable is the one case
    # worth surfacing, since the owner otherwise has no way to recover the
    # condition to re-register it (see tradedesk.fundamentals incident:
    # "had to rebuild mine from source").
    rows = _db().execute(
        "SELECT id, notify_to, condition_json, created_unix_ns, last_fired_unix_ns, fire_count, "
        "currently_satisfied, disabled_reason FROM live_alerts WHERE owner = ? "
        "AND (cancelled = 0 OR disabled_reason IS NOT NULL)",
        (owner,),
    ).fetchall()
    out = []
    for r in rows:
        alert_id, condition = r[0], json.loads(r[2])
        entry = {
            "alert_id": alert_id,
            "notify_to": r[1],
            "condition": condition,
            "created_unix_ns": r[3],
            "last_fired_unix_ns": r[4],
            "fire_count": r[5],
        }
        if r[7]:
            entry["state"] = "disabled"
            entry["disabled_reason"] = r[7]
        else:
            entry["state"] = "active"
        if condition.get("kind") == "trigger":
            if condition.get("repeat"):
                # repeat mode fires on every match with no edge-state tracked
                # at all — satisfied_tickers has no meaning here.
                entry["repeat"] = True
            else:
                # Per-(alert, ticker) state, not the alert-level flag below
                # (see _trigger_ticker_state) — this is the field that's
                # actually meaningful for a multi-ticker trigger.
                entry["satisfied_tickers"] = sorted(
                    t for t in condition.get("tickers", []) if _trigger_ticker_state.get((alert_id, t))
                )
        else:
            entry["currently_satisfied"] = bool(r[6])
        out.append(entry)
    return out


def cancel_alert(alert_id: str) -> Dict[str, Any]:
    conn = _db()
    cur = conn.execute("UPDATE live_alerts SET cancelled = 1 WHERE id = ? AND cancelled = 0", (alert_id,))
    conn.commit()
    _load_alerts_into_index()
    return {"alert_id": alert_id, "cancelled": cur.rowcount > 0}


async def _fire_alert(rec: dict, detail: str, payload: Optional[Dict[str, Any]] = None) -> None:
    """Delivery target is `rec["notify_to"]`. An "http://"/"https://" value is
    POSTed to directly as a webhook (added 2026-09-28 for headless consumers —
    a k8s Deployment placing orders has no live session to receive an ATC DM
    at all); anything else goes through ATC as before. Same best-effort
    semantics either way: 5s timeout, logged and dropped on failure, no
    retry — a consumer that needs delivery guarantees polls list_my_live_alerts
    itself rather than relying solely on the push."""
    notify_to = rec["notify_to"]
    body = dict(payload) if payload else {}
    body.update({"alert_id": rec["id"], "condition": rec["condition"], "detail": detail})
    content = (
        f"live engine alert fired (id={rec['id']})\n"
        f"condition: {json.dumps(rec['condition'])}\n"
        f"{detail}"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            if notify_to.startswith("http://") or notify_to.startswith("https://"):
                resp = await client.post(notify_to, json=body)
            else:
                resp = await client.post(
                    f"{ATC_URL}/messages",
                    json={"to": notify_to, "from": ATC_FROM, "subject": "live engine alert", "content": content},
                )
            resp.raise_for_status()
    except Exception as e:
        logger.error("live_ingest: failed to deliver alert %s to %s: %s", rec["id"], notify_to, e)


def _set_satisfied(rec: dict, satisfied: bool) -> None:
    rec["currently_satisfied"] = satisfied
    conn = _db()
    if satisfied:
        conn.execute(
            "UPDATE live_alerts SET currently_satisfied = 1, last_fired_unix_ns = ?, fire_count = fire_count + 1 "
            "WHERE id = ?",
            (time.time_ns(), rec["id"]),
        )
    else:
        conn.execute("UPDATE live_alerts SET currently_satisfied = 0 WHERE id = ?", (rec["id"],))
    conn.commit()


async def _evaluate_threshold_alerts(key: Tuple[str, str], entry: dict) -> None:
    for rec in _threshold_index.get(key, []):
        condition = rec["condition"]
        value = entry.get(condition["field"])
        if value is None:
            continue
        satisfied = _OPS[condition["op"]](value, condition["value"])
        # Edge-triggered: fire once per crossing, same as atc_remind's own
        # usage-kind trigger — re-arms only after the condition goes false
        # again, never re-fires while it stays true.
        if satisfied and not rec["currently_satisfied"]:
            await _fire_alert(
                rec,
                f"{condition['field']} {condition['op']} {condition['value']} on "
                f"{condition['spec_id']}/{condition['ticker']} (now {value})",
            )
            _set_satisfied(rec, True)
        elif not satisfied and rec["currently_satisfied"]:
            _set_satisfied(rec, False)


async def _disable_pathological_trigger(rec: dict, reason: str) -> None:
    logger.warning("live_ingest: auto-disabling trigger %s: %s", rec["id"], reason)
    conn = _db()
    conn.execute("UPDATE live_alerts SET cancelled = 1, disabled_reason = ? WHERE id = ?", (reason, rec["id"]))
    conn.commit()
    _load_alerts_into_index()
    await _fire_alert(
        rec,
        f"This trigger has been AUTO-DISABLED and will not fire again — re-register if you fix the expr. "
        f"Reason: {reason}. It stays visible in list_my_live_alerts with state='disabled' so you can "
        f"read the condition back to re-register it.",
    )


async def _record_slow_eval(rec: dict, elapsed: float, extra: str = "") -> bool:
    """Track consecutive over-budget evaluations for one trigger; only disable
    after _TRIGGER_MAX_CONSECUTIVE_SLOW in a row, never on one (2026-09-28 —
    see the constant's comment for the live incident this fixes). Returns
    True if the trigger was just disabled, so the caller stops using `rec`."""
    if elapsed <= _TRIGGER_EVAL_MAX_SECONDS:
        _trigger_slow_counts[rec["id"]] = 0
        return False
    count = _trigger_slow_counts.get(rec["id"], 0) + 1
    _trigger_slow_counts[rec["id"]] = count
    if count < _TRIGGER_MAX_CONSECUTIVE_SLOW:
        return False
    reason = (
        f"expr took {elapsed * 1000:.1f}ms on {count} consecutive evaluations "
        f"(over the {_TRIGGER_EVAL_MAX_SECONDS * 1000:.0f}ms budget){extra}"
    )
    await _disable_pathological_trigger(rec, reason)
    _trigger_slow_counts.pop(rec["id"], None)
    _trigger_error_counts.pop(rec["id"], None)
    return True


async def _evaluate_trigger_alerts(source: str, ticker: str, event_type: Optional[str], fields: Dict[str, Any]) -> None:
    """The generalized 'trigger' condition kind — evaluates `condition["expr"]`
    (a simpleeval-restricted expression, never eval()/exec()) against exactly
    `fields`, nothing else in scope: no attribute access, no imports, no
    comprehensions/loops, per the plan's safety requirement (this runs inside
    the one shared FastMCP process every seat's tool calls go through).

    A RAISE during evaluation (e.g. `strength > 0.5` on a tape kind that
    doesn't grade, where `strength` is None) is normal and expected for some
    events on a source with kind-dependent fields — it means "this event
    doesn't support this expr," not "this expr is broken." Only
    _TRIGGER_MAX_CONSECUTIVE_ERRORS raises IN A ROW with no successful
    evaluation between them (every event this trigger has ever seen failed
    the same way — a typo'd field name, never a real one) gets auto-disabled.
    A slow evaluation (over _TRIGGER_EVAL_MAX_SECONDS) only disables after
    _TRIGGER_MAX_CONSECUTIVE_SLOW in a row — one slow eval is normal
    contention in this shared process, not evidence THIS expr is slow (see
    that constant's comment for the live incident that changed this)."""
    for rec in _trigger_index.get((source, ticker), []):
        condition = rec["condition"]
        if condition.get("event_type") and condition["event_type"] != event_type:
            continue
        if source == "quantum_bar" and condition.get("spec_id") and fields.get("spec_id") != condition["spec_id"]:
            continue
        # `fields` is the SAME dict object for every rec in this loop (built
        # once by the caller per event) — references are per-OWNER, so they
        # must go in a fresh dict per rec, never merged into the shared
        # `fields` itself, or owner A's reference values would leak into
        # owner B's expr evaluation on the same event.
        refs = _reference_index.get((rec["owner"], ticker))
        eval_fields = {**fields, **refs} if refs else fields
        t0 = time.monotonic()
        try:
            result = EvalWithCompoundTypes(names=eval_fields).eval(condition["expr"])
        except Exception as e:
            elapsed = time.monotonic() - t0
            if await _record_slow_eval(rec, elapsed, extra=f", and raised: {e}"):
                continue
            count = _trigger_error_counts.get(rec["id"], 0) + 1
            _trigger_error_counts[rec["id"]] = count
            if count >= _TRIGGER_MAX_CONSECUTIVE_ERRORS:
                await _disable_pathological_trigger(
                    rec, f"expr raised on {count} consecutive events with zero successful evaluations: {e}"
                )
                _trigger_error_counts.pop(rec["id"], None)
                _trigger_slow_counts.pop(rec["id"], None)
            continue
        elapsed = time.monotonic() - t0
        _trigger_error_counts[rec["id"]] = 0
        if await _record_slow_eval(rec, elapsed):
            continue
        satisfied = bool(result)
        fire_payload = {"source": source, "ticker": ticker, "event_type": event_type, "fields": eval_fields,
                         "fired_unix_ns": time.time_ns()}
        fire_detail = f"{source}/{ticker} matched `{condition['expr']}` — fields={eval_fields}"
        if condition.get("repeat"):
            # 2026-09-28: fires on EVERY matching event, no edge-detection, no
            # _trigger_ticker_state tracking at all — added because edge-
            # triggering cannot express a COUNTING rule ("arm on the 4th
            # same-side tape firing"), which is a semantics gap no delivery
            # mechanism can paper over (see massive-triggers skill). Opt-in
            # only; every existing caller is unaffected since "repeat" absent
            # keeps the original edge-triggered behavior below. Caller's own
            # responsibility to scope `expr`/`tickers` sanely on a
            # high-frequency source (quantum_trade/quantum_quote) — this can
            # fire once per event with no throttling.
            if satisfied:
                await _fire_alert(rec, fire_detail, payload=fire_payload)
            continue
        was_satisfied = _trigger_ticker_state.get((rec["id"], ticker), False)
        if satisfied and not was_satisfied:
            await _fire_alert(rec, fire_detail, payload=fire_payload)
            _set_trigger_ticker_satisfied(rec, ticker, True)
        elif not satisfied and was_satisfied:
            _set_trigger_ticker_satisfied(rec, ticker, False)


async def _evaluate_engine_health_alerts() -> None:
    if not _engine_health_alerts:
        return
    now_ns = time.time_ns()
    for rec in _engine_health_alerts:
        condition = rec["condition"]
        instance_filter = condition.get("instance")
        if instance_filter:
            instances = [_state.engine_instances[instance_filter]] if instance_filter in _state.engine_instances else []
        else:
            instances = list(_state.engine_instances.values())
        if not instances:
            # No self-description seen yet for the instance(s) in question —
            # per the silence-diagnosis doctrine (massive-live skill), this is
            # NOT evidence of a health problem, it's evidence we haven't heard
            # from the engine at all yet. Don't fire on it.
            continue
        max_silence = condition["max_silence_seconds"]
        silent = [i["instance_id"] for i in instances if (now_ns - i["last_seen_unix_ns"]) / 1e9 > max_silence]
        is_silent = len(silent) == len(instances)
        if is_silent and not rec["currently_satisfied"]:
            await _fire_alert(rec, f"no market.meta.engine self-description from {silent} in over {max_silence}s")
            _set_satisfied(rec, True)
        elif not is_silent and rec["currently_satisfied"]:
            _set_satisfied(rec, False)


async def _engine_health_ticker() -> None:
    while True:
        await asyncio.sleep(ENGINE_HEALTH_TICK_SECONDS)
        try:
            await _evaluate_engine_health_alerts()
        except Exception:
            logger.exception("live_ingest: engine_health tick failed")


# 2026-09-29: pure diagnostic, no behavior change. Added while chasing the
# OOM crash-loop incident (ea47026/ebbebab both wrapped blocking calls in
# asyncio.to_thread and both crashed in production despite testing clean in
# isolation) -- system measured that the SAME reverted, unmodified code shows
# wildly different growth depending on real load level (596MB@15min/628MB@39min
# during quiet hours vs 2.57GB@25min during heavy desk-agent activity earlier
# the same evening), meaning growth tracks live request/event volume, not code
# alone. Rather than keep guessing at a synthetic repro that may never match
# real conditions, this logs the actual state sizes + RSS on an interval so the
# NEXT real high-load window gives direct evidence of what's actually growing.
_MEMORY_DIAG_TICK_SECONDS = int(os.environ.get("MASSIVE_LIVE_MEMORY_DIAG_TICK_SECONDS", "60"))


async def _memory_diag_ticker() -> None:
    import resource
    import threading

    while True:
        await asyncio.sleep(_MEMORY_DIAG_TICK_SECONDS)
        try:
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            logger.info(
                "live_ingest: memory_diag rss_mb=%.1f bars=%d history=%d "
                "engine_instances=%d cross_asset_spec_ids=%d threshold_idx=%d "
                "trigger_idx=%d trigger_error_counts=%d trigger_slow_counts=%d "
                "reference_idx=%d trigger_ticker_state=%d threads=%d event_count=%d",
                rss_mb, len(_state.bars), len(_state.history),
                len(_state.engine_instances), len(_state.cross_asset_spec_ids),
                len(_threshold_index), len(_trigger_index),
                len(_trigger_error_counts), len(_trigger_slow_counts),
                len(_reference_index), len(_trigger_ticker_state),
                threading.active_count(), _state.event_count,
            )
        except Exception:
            logger.exception("live_ingest: memory_diag tick failed")


def _entry_from_bar_payload(market: str, spec_id: str, ticker: str, payload: dict, now_ns: int) -> dict:
    # oa/ha are only present in wire_fields when true (the msgpack encoder
    # omits false/default fields) — a missing key means "not absent", not
    # "unknown". Confirmed by direct capture 2026-09-27: a real bar carried
    # neither key at all.
    oc_absent = bool(payload.get("oa", False))
    hl_absent = bool(payload.get("ha", False))
    unreliable = ticker in ROSTER_23 and spec_id not in _state.cross_asset_spec_ids
    return {
        "market": market,
        "spec_id": spec_id,
        "ticker": ticker,
        "open": payload.get("o"),
        "high": payload.get("h"),
        "low": payload.get("l"),
        "close": payload.get("c"),
        "volume": payload.get("v"),
        "trade_count": payload.get("n"),
        "start_ts_ns": payload.get("s"),
        "end_ts_ns": payload.get("e"),
        "sequence": payload.get("sq"),
        "metric": payload.get("m"),
        "quotes": payload.get("nb"),
        "oc_absent": oc_absent,
        "hl_absent": hl_absent,
        "unreliable": unreliable,
        "received_unix_ns": now_ns,
        "raw": payload,
    }


async def _on_bar(msg) -> None:
    parts = msg.subject.split(".", 4)
    if len(parts) != 5:
        logger.warning("live_ingest: unparseable bar subject %r", msg.subject)
        return
    _, market, _, spec_id, ticker = parts
    try:
        payload = msgpack.unpackb(msg.data, raw=False)
    except Exception as e:
        logger.warning("live_ingest: msgpack decode failed on %r: %s", msg.subject, e)
        return
    now_ns = time.time_ns()
    entry = _entry_from_bar_payload(market, spec_id, ticker, payload, now_ns)
    key = (spec_id, ticker)
    _state.bars[key] = entry
    _state.bars.move_to_end(key)
    hist = _state.history.get(key)
    if hist is None:
        hist = deque(maxlen=HISTORY_LIMIT)
        _state.history[key] = hist
    _state.history.move_to_end(key)
    hist.append(entry)
    while len(_state.bars) > MAX_TRACKED_KEYS:
        evicted_key, _ = _state.bars.popitem(last=False)
        _state.history.pop(evicted_key, None)
    await _evaluate_threshold_alerts(key, entry)
    await _evaluate_trigger_alerts("quantum_bar", ticker, None, entry)


def _tape_fields(payload: dict) -> Dict[str, Any]:
    """Resolve TapeEvent's kind-dependent `price` field into an unambiguous
    name BEFORE any expr sees it — quantum-engine's wire audit (2026-09-27):
    Sweep/Block carry a real trade price, Absorption the quote midpoint,
    Iceberg/QueueDepletion/Flicker a quote level. A generic `price` name would
    let a trigger silently mix these; each is exposed as its own field, and
    only the one that applies to this event's `kind` is non-None. `strength`
    stays None (not coerced to 0) when the detector doesn't grade — see
    _evaluate_trigger_alerts' docstring for why a raised comparison on that is
    normal, not a broken expr. `direction`, when present, is always the
    AGGRESSOR's side, uniformly across every kind that carries one."""
    kind = payload.get("kind")
    price = payload.get("price")
    trade_price = price if kind in ("sweep", "block") else None
    quote_midpoint = price if kind == "absorption" else None
    quote_level = price if kind in ("iceberg", "queue_depletion", "flicker") else None
    return {
        "kind": kind,
        "trade_price": trade_price,
        "quote_midpoint": quote_midpoint,
        "quote_level": quote_level,
        "size": payload.get("size"),
        "strength": payload.get("strength"),
        "direction": payload.get("direction"),
        "timestamp_ns": payload.get("timestamp_ns"),
    }


async def _on_event(msg) -> None:
    _state.event_count += 1
    _state.last_event_unix_ns = time.time_ns()
    parts = msg.subject.split(".", 4)
    if len(parts) != 5:
        return
    _, market, _, source_token, ticker = parts
    try:
        payload = msgpack.unpackb(msg.data, raw=False)
    except Exception as e:
        logger.warning("live_ingest: msgpack decode failed on event subject %r: %s", msg.subject, e)
        return
    if source_token == "trade":
        fields = {
            "ticker": ticker,
            "trade_price": payload.get("price"),
            "size": payload.get("size"),
            "conditions": payload.get("conditions"),
            "exchange": payload.get("exchange"),
            "timestamp_ns": payload.get("timestamp_ns"),
        }
        await _evaluate_trigger_alerts("quantum_trade", ticker, None, fields)
    elif source_token == "quote":
        bid, ask = payload.get("bid_price"), payload.get("ask_price")
        fields = {
            "ticker": ticker,
            "bid_price": bid,
            "ask_price": ask,
            "bid_size": payload.get("bid_size"),
            "ask_size": payload.get("ask_size"),
            "mid_price": (bid + ask) / 2 if bid is not None and ask is not None else None,
            "timestamp_ns": payload.get("timestamp_ns"),
        }
        await _evaluate_trigger_alerts("quantum_quote", ticker, None, fields)
    elif source_token == "tape":
        fields = _tape_fields(payload)
        fields["ticker"] = ticker
        await _evaluate_trigger_alerts("quantum_tape", ticker, fields["kind"], fields)
    # agg/index tokens exist on the wire's taxonomy but nothing is currently
    # configured to publish them (quantum-engine, 2026-09-27) — no handler
    # needed until that changes.


async def _on_indicator(msg) -> None:
    _state.indicator_count += 1
    _state.last_indicator_unix_ns = time.time_ns()


async def _on_meta(msg) -> None:
    try:
        payload = msgpack.unpackb(msg.data, raw=False)
    except Exception as e:
        logger.warning("live_ingest: msgpack decode failed on meta subject %r: %s", msg.subject, e)
        return
    now_ns = time.time_ns()
    instance = payload.get("instance") or {}
    instance_id = instance.get("id") or msg.subject
    prev = _state.engine_instances.get(instance_id)
    schema_id = payload.get("schema_id")
    if prev is not None and prev.get("schema_id") != schema_id:
        logger.warning(
            "live_ingest: schema_id changed for %s: %s -> %s — wire field set changed, re-check assumptions",
            instance_id, prev.get("schema_id"), schema_id,
        )
    specs = payload.get("specs") or {}
    _state.cross_asset_spec_ids.update(specs.get("stage1_cross_asset") or [])

    started_unix_ns = instance.get("started_unix_ns")
    restarted = False
    if prev is not None:
        prev_started = (prev.get("payload") or {}).get("instance", {}).get("started_unix_ns")
        # >1s tolerance, not equality — measured ~10ms jitter every heartbeat
        # with zero real restarts (massive-live skill, 2026-09-22): the field
        # is derived fresh from /proc/uptime rather than cached upstream.
        if prev_started is not None and started_unix_ns is not None:
            if abs(started_unix_ns - prev_started) > 1_000_000_000:
                restarted = True

    _state.engine_instances[instance_id] = {
        "instance_id": instance_id,
        "schema_id": schema_id,
        "last_seen_unix_ns": now_ns,
        "restarted_since_last_seen": restarted,
        "payload": payload,
    }


async def _subscribe_all(nc: "nats.aio.client.Client") -> None:
    await nc.subscribe("market.*.bar.>", cb=_on_bar)
    await nc.subscribe("market.*.event.>", cb=_on_event)
    await nc.subscribe("market.*.indicator.>", cb=_on_indicator)
    await nc.subscribe("market.meta.engine.>", cb=_on_meta)


async def _on_nats_error(e: Exception) -> None:
    logger.warning("live_ingest: NATS error: %s", e)


async def _on_nats_disconnected() -> None:
    logger.warning("live_ingest: NATS disconnected, reconnecting")


async def _on_nats_reconnected() -> None:
    logger.info("live_ingest: NATS reconnected")


async def run_forever() -> None:
    """Entry point for the background task: connect, subscribe, and hold the
    connection open for the life of the process, reconnecting on disconnect.
    Meant to be started once via asyncio.create_task alongside the ASGI
    server, not per-MCP-session (a FastMCP session-scoped lifespan would
    start/stop this every time a client connects/disconnects, which is not
    what a fleet-shared feed needs)."""
    _load_alerts_into_index()
    _load_references_into_index()
    _load_trigger_ticker_state()
    asyncio.create_task(_engine_health_ticker())
    asyncio.create_task(_memory_diag_ticker())
    while True:
        try:
            nc = await nats.connect(
                NATS_URL,
                connect_timeout=5,
                reconnect_time_wait=2,
                max_reconnect_attempts=-1,
                error_cb=_on_nats_error,
                disconnected_cb=_on_nats_disconnected,
                reconnected_cb=_on_nats_reconnected,
            )
            await _subscribe_all(nc)
            logger.info("live_ingest: connected to %s", NATS_URL)
            while nc.is_connected or nc.is_reconnecting:
                await asyncio.sleep(5)
            logger.warning("live_ingest: connection loop exited (closed), reconnecting from scratch")
        except Exception:
            logger.exception("live_ingest: connection attempt failed, retrying in 5s")
            await asyncio.sleep(5)


async def evaluate_trigger(source: str, ticker: str, event_type: Optional[str], fields: Dict[str, Any]) -> None:
    """Public entry point for another ingestion module (mind_ingest.py) to run
    its own normalized events through this same trigger-evaluation/alert-
    registry machinery, without duplicating it. `source` should be "mind_sse"
    — anything registered against a quantum_* source will simply never match
    mind-sourced events, since the index key is (source, ticker)."""
    await _evaluate_trigger_alerts(source, ticker, event_type, fields)


# --- Read accessors for the MCP tools in server.py ---

def get_bar(spec_id: str, ticker: str) -> Dict[str, Any]:
    entry = _state.bars.get((spec_id, ticker))
    if entry is None:
        return {"found": False, "spec_id": spec_id, "ticker": ticker}
    now_ns = time.time_ns()
    return {
        "found": True,
        **{k: v for k, v in entry.items() if k != "raw"},
        "raw": entry["raw"],
        "age_seconds": (now_ns - entry["received_unix_ns"]) / 1e9,
        "evaluable": not entry["oc_absent"],
    }


def get_history(spec_id: str, ticker: str, limit: int = 100) -> Dict[str, Any]:
    hist = _state.history.get((spec_id, ticker))
    if not hist:
        return {"found": False, "spec_id": spec_id, "ticker": ticker, "bars": []}
    limit = max(1, min(limit, HISTORY_LIMIT))
    bars = list(hist)[-limit:]
    return {"found": True, "spec_id": spec_id, "ticker": ticker, "count": len(bars), "bars": bars}


def get_specs_snapshot() -> Dict[str, Any]:
    now_ns = time.time_ns()
    out = {}
    for instance_id, rec in _state.engine_instances.items():
        payload = rec["payload"]
        specs = payload.get("specs") or {}
        placed = (
            list(specs.get("stage1_disjoint", []))
            + list(specs.get("stage1_cross_asset", []))
            + list(specs.get("stage2", []))
        )
        out[instance_id] = {
            "schema_id": rec["schema_id"],
            "last_seen_unix_ns": rec["last_seen_unix_ns"],
            "age_seconds": (now_ns - rec["last_seen_unix_ns"]) / 1e9,
            "restarted_since_last_seen": rec["restarted_since_last_seen"],
            "heartbeat_secs": payload.get("heartbeat_secs"),
            "mode": payload.get("mode"),
            "instance": payload.get("instance"),
            "specs": specs,
            "placed_count": len(placed),
            "universe": payload.get("universe"),
            "requests": payload.get("requests"),
        }
    return {
        "instances": out,
        "event_count_seen": _state.event_count,
        "indicator_count_seen": _state.indicator_count,
        "last_event_unix_ns": _state.last_event_unix_ns,
        "last_indicator_unix_ns": _state.last_indicator_unix_ns,
    }
