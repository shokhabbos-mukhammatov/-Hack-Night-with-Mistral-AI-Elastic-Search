"""Elastic Agent Builder's MCP server, reached through the Mistral SDK's MCP client (streamable HTTP).

Agent Builder exposes our ES|QL tools (scripts/agent_builder.py) at {KIBANA}/api/agent_builder/mcp.
The Mistral SDK turns MCP tools into Mistral FunctionTools, so they plug straight into the Mistral agent.
"""

import asyncio
from contextlib import AsyncExitStack

from highground.config import settings

NAMESPACE = "highground"  # Agent Builder MCP names: "highground.flood_hotspots" -> "highground_flood_hotspots"
PREFIX = NAMESPACE + "_"


def endpoint() -> tuple[str, dict] | None:
    cfg = settings()
    if not cfg.kibana_url:
        return None
    headers = {"Authorization": f"ApiKey {cfg.kibana_api_key}"} if cfg.kibana_api_key else {}
    return f"{cfg.kibana_url.rstrip('/')}/api/agent_builder/mcp?namespace={NAMESPACE}", headers


async def _session(fn, url: str | None = None, headers: dict | None = None):
    from mistralai.extra.mcp.streamable_http import MCPClientStreamableHTTP, StreamableHTTPServerParams

    if url is None:
        ep = endpoint()
        if not ep:
            raise RuntimeError("HG_KIBANA_URL not set")
        url, headers = ep
    client = MCPClientStreamableHTTP(StreamableHTTPServerParams(url=url, headers=headers or {}, timeout=30))
    async with AsyncExitStack() as stack:
        await client.initialize(exit_stack=stack)
        try:
            return await fn(client)
        finally:
            await client.aclose()


def list_tools(url: str | None = None, headers: dict | None = None, only_ours: bool = True) -> list:
    """Mistral FunctionTool objects for the Agent Builder tools (ours only, by default)."""

    async def go(c):
        return await c.get_tools()

    tools = asyncio.run(_session(go, url, headers))
    return [t for t in tools if not only_ours or t.function.name.startswith(PREFIX)]


def call_tool(name: str, args: dict, url: str | None = None, headers: dict | None = None) -> list[str]:
    async def go(c):
        return await c.execute_tool(name, args)

    chunks = asyncio.run(_session(go, url, headers))
    return [c.get("text", "") if isinstance(c, dict) else str(c) for c in chunks]
