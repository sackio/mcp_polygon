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
import asyncio
import json
import logging
import operator
import os
import sqlite3
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx
import msgpack
import nats

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


class _LiveState:
    def __init__(self) -> None:
        self.bars: Dict[Tuple[str, str], dict] = {}
        self.history: Dict[Tuple[str, str], Deque[dict]] = {}
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
    else:
        raise ValueError(f"unknown condition kind: {kind!r} (expected 'threshold' or 'engine_health')")


def _load_alerts_into_index() -> None:
    global _threshold_index, _engine_health_alerts
    threshold_index: Dict[Tuple[str, str], List[dict]] = {}
    engine_health_alerts: List[dict] = []
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
        else:
            engine_health_alerts.append(rec)
    _threshold_index = threshold_index
    _engine_health_alerts = engine_health_alerts


def register_alert(condition: Dict[str, Any], notify_to: str, owner: Optional[str] = None) -> Dict[str, Any]:
    _validate_condition(condition)
    alert_id = str(uuid.uuid4())
    owner = owner or notify_to
    conn = _db()
    conn.execute(
        "INSERT INTO live_alerts (id, owner, notify_to, condition_json, created_unix_ns) VALUES (?, ?, ?, ?, ?)",
        (alert_id, owner, notify_to, json.dumps(condition), time.time_ns()),
    )
    conn.commit()
    _load_alerts_into_index()
    return {"alert_id": alert_id, "owner": owner, "notify_to": notify_to, "condition": condition}


def list_alerts(owner: str) -> List[Dict[str, Any]]:
    rows = _db().execute(
        "SELECT id, notify_to, condition_json, created_unix_ns, last_fired_unix_ns, fire_count, "
        "currently_satisfied FROM live_alerts WHERE owner = ? AND cancelled = 0",
        (owner,),
    ).fetchall()
    return [
        {
            "alert_id": r[0],
            "notify_to": r[1],
            "condition": json.loads(r[2]),
            "created_unix_ns": r[3],
            "last_fired_unix_ns": r[4],
            "fire_count": r[5],
            "currently_satisfied": bool(r[6]),
        }
        for r in rows
    ]


def cancel_alert(alert_id: str) -> Dict[str, Any]:
    conn = _db()
    cur = conn.execute("UPDATE live_alerts SET cancelled = 1 WHERE id = ? AND cancelled = 0", (alert_id,))
    conn.commit()
    _load_alerts_into_index()
    return {"alert_id": alert_id, "cancelled": cur.rowcount > 0}


async def _fire_alert(rec: dict, detail: str) -> None:
    content = (
        f"live engine alert fired (id={rec['id']})\n"
        f"condition: {json.dumps(rec['condition'])}\n"
        f"{detail}"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{ATC_URL}/messages",
                json={"to": rec["notify_to"], "from": ATC_FROM, "subject": "live engine alert", "content": content},
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error("live_ingest: failed to deliver alert %s to %s: %s", rec["id"], rec["notify_to"], e)


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
    hist = _state.history.get(key)
    if hist is None:
        hist = deque(maxlen=HISTORY_LIMIT)
        _state.history[key] = hist
    hist.append(entry)
    await _evaluate_threshold_alerts(key, entry)


async def _on_event(msg) -> None:
    _state.event_count += 1
    _state.last_event_unix_ns = time.time_ns()


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
    asyncio.create_task(_engine_health_ticker())
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
