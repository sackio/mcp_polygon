#!/usr/bin/env python3
"""Run the MCP Massive server with streamable-http transport on 0.0.0.0:24400."""
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

from mcp_massive import server

if __name__ == "__main__":
    api_key = os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        print("Warning: MASSIVE_API_KEY (or legacy POLYGON_API_KEY) environment variable not set.")
    else:
        print("Starting Massive MCP server with API key configured on 0.0.0.0:24400")

    # Get the ASGI app and run with uvicorn
    # streamable-http transport — SSE doesn't work with Claude Code's MCP client
    app = server.get_asgi_app("streamable-http")
    uvicorn.run(app, host="0.0.0.0", port=24400)
