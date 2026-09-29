#!/usr/bin/env python3
"""Standalone relay: mind's SSE event feed -> NATS JetStream, unscoped (all
tickers, all event types), with replay-by-sequence for reconnecting
consumers.

Built 2026-09-29 per Ben's directive ("massive should be pumping all this
out... I want massive to be the center of everything") and system's explicit
instruction to keep this OUT of mcp_polygon_server (open OOM investigation on
that process) and run it as its own container instead. Does not touch, import
from, or restart mcp_polygon_server — it independently re-implements the same
SSE parsing mind_ingest.py in that repo already has, so this container has no
runtime dependency on the other one.

Subject scheme: mind.events.<event_type>.<ticker>
  - event_type and ticker are both single NATS tokens (mind's event_type
    values are plain identifiers; ticker is sanitized the same way
    mind_ingest.py does, "BRK.B" -> "BRK-B", so neither ever contains a dot).
  - Frames with no resolvable ticker (diagnostic frames: unverifiable,
    freshness) use the same "_FEED" pseudo-ticker mind_ingest.py already
    uses, for consistency with the in-process consumer.
  - `mind.events.analyst_action.>`  -> one event type, every ticker
  - `mind.events.*.AAPL`            -> every event type, one ticker
  - `mind.events.>`                 -> everything
  fields.rule (and every other payload field) is NOT in the subject — it's
  in the message body. Subject cardinality would explode if rule were a
  token; consumers filter on it client-side after a broader subject
  subscription.

Staleness gate: identical to mind_ingest.py's (observed_at older than
MIND_MAX_STALENESS_HOURS is dropped, fail-open on unparseable timestamps) --
this relay must not reintroduce the backfill-leaking-onto-live incident that
gate was built to fix.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx
import nats
from nats.js.api import StreamConfig, RetentionPolicy, StorageType

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("mind_relay")

MIND_SSE_URL = os.environ.get("MIND_SSE_URL", "http://192.168.1.42:8117/api/events/sse")
NATS_URL = os.environ.get("RELAY_NATS_URL", "nats://127.0.0.1:4223")
STREAM_NAME = "MIND_EVENTS"
SUBJECT_PREFIX = "mind.events"
MAX_STALENESS_HOURS = float(os.environ.get("RELAY_MAX_STALENESS_HOURS", "24"))
STREAM_MAX_AGE_SECONDS = int(os.environ.get("RELAY_STREAM_MAX_AGE_SECONDS", str(7 * 24 * 3600)))


import re

_NATS_UNSAFE = re.compile(r"[\s*>]+")


def _sanitize_ticker(ticker: str) -> str:
    """Same normalization as mind_ingest.py's _sanitize_ticker, PLUS NATS
    subject-token safety. `filing_notice`/`equity_offering_or_issuance`
    frames put a free-text ENTITY NAME (e.g. "JPMORGAN CHASE & CO") in
    `subject` when there's no real ticker -- used verbatim as a subject
    token, that whitespace made `js.publish` raise on every such frame,
    silently dropping it (caught+logged, not retried). Found 2026-09-29 via
    mind's own capture parity check: 5 missing ids, all filing_notice/
    equity_offering_or_issuance with multi-word `subject` values."""
    s = ticker.replace(".", "-")
    s = _NATS_UNSAFE.sub("_", s).strip("_")
    return s or "_FEED"


def _parse_sse_frame(lines: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    event_id = event_type = data = None
    for line in lines:
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            piece = line[5:].strip()
            data = piece if data is None else data + "\n" + piece
    return event_id, event_type, data


async def _ensure_stream(js) -> None:
    try:
        await js.stream_info(STREAM_NAME)
        logger.info("nats: stream %s already exists", STREAM_NAME)
    except Exception:
        await js.add_stream(
            StreamConfig(
                name=STREAM_NAME,
                subjects=[f"{SUBJECT_PREFIX}.>"],
                retention=RetentionPolicy.LIMITS,
                max_age=float(STREAM_MAX_AGE_SECONDS),  # nats-py StreamConfig.max_age is SECONDS, not ns
                storage=StorageType.FILE,
            )
        )
        logger.info("nats: created stream %s (max_age=%ds)", STREAM_NAME, STREAM_MAX_AGE_SECONDS)


async def _handle_frame(js, event_id: Optional[str], event_type: Optional[str], data: Optional[str]) -> None:
    if event_type is None or data is None:
        return
    try:
        payload = json.loads(data)
    except Exception as e:
        logger.warning("relay: SSE data not JSON for event_type=%r: %s", event_type, e)
        return

    observed_at_raw = payload.get("observed_at")
    if observed_at_raw:
        try:
            observed_dt = datetime.fromisoformat(str(observed_at_raw).replace("Z", "+00:00"))
            if observed_dt.tzinfo is None:
                observed_dt = observed_dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - observed_dt).total_seconds() / 3600.0
            if age_hours > MAX_STALENESS_HOURS:
                logger.info(
                    "relay: dropping stale event, event_type=%r id=%s observed_at=%s age=%.1fh",
                    event_type, payload.get("id"), observed_at_raw, age_hours,
                )
                return
        except Exception:
            pass  # unparseable observed_at doesn't block delivery -- fail open

    # `ticker`/`subject` cover event-typed frames (analyst_action, filing_notice,
    # ...). `article` frames instead carry a `tickers` ARRAY (plural, can name
    # more than one company in one headline) -- mind_ingest.py's own singular
    # lookup misses this too (verified live 2026-09-29: every article frame
    # landed under the "_FEED" pseudo-ticker, silently breaking per-ticker
    # filtering for headlines, the exact "fastest leg" data mind called out).
    # Fixed here by publishing once per ticker named in `tickers` when present.
    tickers_field = payload.get("tickers")
    if isinstance(tickers_field, list) and tickers_field:
        tickers = [_sanitize_ticker(str(t)) for t in tickers_field if t]
    else:
        single = payload.get("ticker") or payload.get("subject")
        tickers = [_sanitize_ticker(str(single))] if single else ["_FEED"]

    fields = dict(payload)
    fields["relay_received_unix_ns"] = time.time_ns()
    # mind's own SSE `id:` line, verbatim -- required by mind+Ben spec addition
    # 2026-09-29 11:06 ET so mind can verify 1:1 parity and so a consumer's
    # resume-by-mind-id (distinct from NATS's own replay-by-sequence) has
    # something to search on in the message body.
    fields["mind_event_id"] = event_id
    body = json.dumps(fields).encode("utf-8")

    for ticker in tickers:
        subject = f"{SUBJECT_PREFIX}.{event_type}.{ticker}"
        try:
            await js.publish(subject, body)
        except Exception:
            logger.exception("nats: publish failed for subject=%s", subject)


async def _sse_forever(js) -> None:
    last_event_id: Optional[str] = None
    while True:
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("GET", MIND_SSE_URL, headers=headers) as resp:
                    resp.raise_for_status()
                    logger.info("relay: connected to %s (Last-Event-ID=%s)", MIND_SSE_URL, last_event_id)
                    buf: List[str] = []
                    async for raw_line in resp.aiter_lines():
                        if raw_line == "":
                            if buf:
                                event_id, event_type, data = _parse_sse_frame(buf)
                                buf = []
                                if event_id:
                                    last_event_id = event_id
                                await _handle_frame(js, event_id, event_type, data)
                        else:
                            buf.append(raw_line)
        except Exception:
            logger.exception("relay: SSE connection failed, retrying in 5s")
            await asyncio.sleep(5)


async def main() -> None:
    nc = await nats.connect(NATS_URL, max_reconnect_attempts=-1)
    js = nc.jetstream()
    await _ensure_stream(js)
    logger.info("relay: starting, publishing to %s.> on %s", SUBJECT_PREFIX, NATS_URL)
    await _sse_forever(js)


if __name__ == "__main__":
    asyncio.run(main())
