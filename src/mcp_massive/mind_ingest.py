"""Background consumer for mind's live event feeds — a second, independent
data source alongside quantum-engine's NATS feed (live_ingest.py), feeding
the same generalized trigger-evaluation machinery there via
live_ingest.evaluate_trigger(). Deliberately does not share reconnect state
with the NATS consumer or with itself between the two sources below (per the
plan: "a second, parallel background task").

Two sources, both from `mind` (server5 / 192.168.1.42:8117):

1. SSE push (`GET /api/events/sse`) — mind's own taxonomy-typed event stream
   (analyst_action, guidance, m_and_a, price_move, legal_regulatory,
   earnings_result, ...). Confirmed live 2026-09-27: the documented protocol
   (`.claude/skills/massive-mind/SKILL.md` -> mind's own `mind-events` skill)
   matches real traffic exactly — a leading `: connected at <cursor>` comment
   frame, then `id:`/`event:`/`data:` frames, msgpack nowhere in sight, this
   is plain SSE/JSON. Also saw two real frame types not named in mind-events'
   taxonomy list: `unverifiable` and `freshness` (data-quality/lane-health
   diagnostics, not market events) — passed through like any other event_type
   rather than filtered out, so an operator CAN register a trigger on feed
   health if they want one.
2. mind's EXISTING earnings-announce-push sink, tailed as a plain file
   (/mnt/nas/data/code/tradedesk/projects/earnings/data/announcements.jsonl)
   rather than intercepting mind's ATC DMs to tradedesk — the file already IS
   the durable, ordered record of that feed, and tailing it is simpler and
   more robust from a background asyncio task than trying to observe another
   seat's ATC traffic.

⛔ tradedesk-earnings' own pipeline (mind -> ATC DM -> announcements.jsonl ->
thread-tradedesk-12 trading directly off it) is NOT touched, intercepted, or
duplicated by this module — this is a second, independent READER of the same
file, purely additive. See massive-triggers skill for why tradedesk-earnings
itself should keep using its existing bespoke path rather than switch to a
registered trigger here.
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from . import live_ingest

logger = logging.getLogger("mcp_massive.mind_ingest")

MIND_SSE_URL = os.environ.get("MASSIVE_LIVE_MIND_SSE_URL", "http://192.168.1.42:8117/api/events/sse")
ANNOUNCEMENTS_JSONL_PATH = os.environ.get(
    "MASSIVE_LIVE_EARNINGS_JSONL",
    "/mnt/nas/data/code/tradedesk/projects/earnings/data/announcements.jsonl",
)
JSONL_POLL_SECONDS = 2.0


def _sanitize_ticker(ticker: str) -> str:
    """Same normalization as live_ingest._sanitize_ticker (kept local rather
    than importing a private name cross-module) — a trigger registered
    against "BRK.B" must match mind events tagged the same way the quantum
    wire sanitizes symbols."""
    return ticker.replace(".", "-")


def _parse_sse_frame(lines: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """One SSE frame's accumulated lines (between blank-line boundaries) ->
    (event_id, event_type, data). The leading `: connected at ...` comment
    frame has none of the three and is silently skipped by the caller."""
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


async def _handle_sse_frame(event_type: Optional[str], data: Optional[str]) -> None:
    if event_type is None or data is None:
        return
    try:
        payload = json.loads(data)
    except Exception as e:
        logger.warning("mind_ingest: SSE data not JSON for event_type=%r: %s", event_type, e)
        return
    ticker = payload.get("ticker") or payload.get("subject")
    if not ticker:
        # Diagnostic frames (unverifiable, freshness) and any taxonomy event
        # extraction failed to resolve a ticker for carry no ticker at all —
        # exposed under a fixed pseudo-ticker rather than dropped, so a
        # data-quality trigger is still possible to register.
        ticker = "_FEED"
    fields = dict(payload)
    fields["received_unix_ns"] = time.time_ns()
    await live_ingest.evaluate_trigger("mind_sse", _sanitize_ticker(str(ticker)), event_type, fields)


async def _sse_forever() -> None:
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
                    logger.info("mind_ingest: connected to %s (Last-Event-ID=%s)", MIND_SSE_URL, last_event_id)
                    buf: List[str] = []
                    async for raw_line in resp.aiter_lines():
                        if raw_line == "":
                            if buf:
                                event_id, event_type, data = _parse_sse_frame(buf)
                                buf = []
                                if event_id:
                                    last_event_id = event_id
                                await _handle_sse_frame(event_type, data)
                        else:
                            buf.append(raw_line)
        except Exception:
            logger.exception("mind_ingest: SSE connection failed, retrying in 5s")
            await asyncio.sleep(5)


async def _handle_announcement_line(line: str) -> None:
    try:
        row = json.loads(line)
    except Exception as e:
        logger.warning("mind_ingest: announcements.jsonl line not JSON: %s (%s)", line[:200], e)
        return
    ticker = row.get("ticker")
    if not ticker:
        return
    fields = dict(row)
    fields["received_unix_ns"] = time.time_ns()
    await live_ingest.evaluate_trigger("mind_earnings_push", _sanitize_ticker(str(ticker)), None, fields)


async def _jsonl_tail_forever() -> None:
    path = Path(ANNOUNCEMENTS_JSONL_PATH)
    # Start at the CURRENT end of file — this is a live-events feed, not a
    # backfill tool. Rows already in the file were ingested before this
    # consumer existed and are readable directly from the file by anyone who
    # wants the history.
    last_size = path.stat().st_size if path.exists() else 0
    while True:
        try:
            if path.exists():
                size = path.stat().st_size
                if size < last_size:
                    logger.warning("mind_ingest: %s shrank (rotated/truncated) — restarting from the top", path)
                    last_size = 0
                if size > last_size:
                    with path.open("r") as f:
                        f.seek(last_size)
                        new_data = f.read()
                    last_size = size
                    for line in new_data.splitlines():
                        line = line.strip()
                        if line:
                            await _handle_announcement_line(line)
        except Exception:
            logger.exception("mind_ingest: jsonl tail failed on %s", path)
        await asyncio.sleep(JSONL_POLL_SECONDS)


async def run_forever() -> None:
    """Entry point: two independent background loops, started once alongside
    live_ingest.run_forever() in run_server_host.py."""
    await asyncio.gather(_sse_forever(), _jsonl_tail_forever())
