#!/usr/bin/env python3
"""Run the MCP Massive server with streamable-http transport on 0.0.0.0:24400."""
import asyncio
import os
import sys
import uvicorn

# Add src to path - works for both Docker and host
current_dir = os.path.dirname(os.path.abspath(__file__))
src_path = os.path.join(current_dir, 'src')
# Also try /app/src for Docker compatibility
if os.path.exists('/app/src'):
    sys.path.insert(0, '/app/src')
elif os.path.exists(src_path):
    sys.path.insert(0, src_path)
else:
    # Fallback to current directory
    sys.path.insert(0, current_dir)

from mcp_massive import server, live_ingest, mind_ingest, earnings_today, refdata_refresh


async def _main() -> None:
    # live_ingest's NATS subscriber and mind_ingest's SSE/jsonl consumers are
    # started here (once, for the process's life) rather than via FastMCP's
    # own lifespan hook — that hook is per-MCP-session, and these are
    # fleet-shared feeds with exactly one subscriber each, not one per
    # connecting client. The two are independent background tasks (separate
    # reconnect/backoff loops) sharing only the trigger-evaluation layer in
    # live_ingest.py.
    asyncio.create_task(live_ingest.run_forever())
    asyncio.create_task(mind_ingest.run_forever())
    asyncio.create_task(earnings_today.run_forever())
    asyncio.create_task(refdata_refresh.run_forever())

    app = server.get_asgi_app("streamable-http")
    config = uvicorn.Config(app, host="0.0.0.0", port=24400)
    uvicorn_server = uvicorn.Server(config)
    await uvicorn_server.serve()


if __name__ == "__main__":
    api_key = os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        print("Warning: MASSIVE_API_KEY (or legacy POLYGON_API_KEY) environment variable not set.")
    else:
        print("Starting Massive MCP server with API key configured on 0.0.0.0:24400")

    asyncio.run(_main())
