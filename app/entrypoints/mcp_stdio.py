"""stdio entrypoint for the MCP server — reads MCP_USER_ID env var per ADR-006/015."""

from __future__ import annotations

import asyncio

from mcp.server.stdio import stdio_server

from app.db.identity import resolve_user_id_stdio
from app.mcp.server import create_server
from app.obs.logging import configure_logging

configure_logging("mcp-stdio")


async def main() -> None:
    user_id = resolve_user_id_stdio()
    server = create_server(user_id)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
