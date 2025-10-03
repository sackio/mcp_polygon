#!/usr/bin/env python3
"""Run the MCP Polygon server with SSE transport on 0.0.0.0:24400."""
import os
import sys
import uvicorn

# Add src to path
sys.path.insert(0, '/app/src')

from mcp_polygon import server

if __name__ == "__main__":
    polygon_api_key = os.environ.get("POLYGON_API_KEY", "")
    if not polygon_api_key:
        print("Warning: POLYGON_API_KEY environment variable not set.")
    else:
        print("Starting Polygon MCP server with API key configured on 0.0.0.0:24400")

    # Get the ASGI app and run with uvicorn
    app = server.get_asgi_app("sse")
    uvicorn.run(app, host="0.0.0.0", port=24400)
