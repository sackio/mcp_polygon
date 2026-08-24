#!/usr/bin/env python
import os
from typing import Literal
from mcp_massive import server


def transport() -> Literal["stdio", "sse", "streamable-http"]:
    """
    Determine the transport type for the MCP server.
    Defaults to 'stdio' if not set in environment variables.
    """
    mcp_transport_str = os.environ.get("MCP_TRANSPORT", "stdio")

    # These are currently the only supported transports
    supported_transports: dict[str, Literal["stdio", "sse", "streamable-http"]] = {
        "stdio": "stdio",
        "sse": "sse",
        "streamable-http": "streamable-http",
    }

    return supported_transports.get(mcp_transport_str, "stdio")


# Ensure the server process doesn't exit immediately when run as an MCP server
def start_server():
    api_key = os.environ.get("MASSIVE_API_KEY", "") or os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        print("Warning: MASSIVE_API_KEY (or legacy POLYGON_API_KEY) environment variable not set.")
    else:
        print("Starting Massive MCP server with API key configured.")

    server.run(transport=transport())


if __name__ == "__main__":
    start_server()
