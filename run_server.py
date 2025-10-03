#!/usr/bin/env python3
"""Run the MCP Polygon server with SSE transport on 0.0.0.0:8000."""
import os
from mcp_polygon.server import poly_mcp

if __name__ == "__main__":
    polygon_api_key = os.environ.get("POLYGON_API_KEY", "")
    if not polygon_api_key:
        print("Warning: POLYGON_API_KEY environment variable not set.")
    else:
        print("Starting Polygon MCP server with API key configured on 0.0.0.0:8000")

    # Monkey patch the uvicorn config in FastMCP
    import mcp.server.fastmcp
    original_run = mcp.server.fastmcp.FastMCP.run

    def patched_run(self, transport="stdio", mount_path=None):
        if transport in ("sse", "streamable-http"):
            # Patch uvicorn to bind to 0.0.0.0
            import uvicorn
            original_uvicorn_run = uvicorn.run

            def patched_uvicorn_run(app, **kwargs):
                kwargs["host"] = "0.0.0.0"
                kwargs["port"] = 8000
                return original_uvicorn_run(app, **kwargs)

            uvicorn.run = patched_uvicorn_run

        return original_run(self, transport, mount_path)

    mcp.server.fastmcp.FastMCP.run = patched_run

    # Now run the server
    poly_mcp.run("sse")
