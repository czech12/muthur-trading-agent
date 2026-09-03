from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

log = logging.getLogger(__name__)


def _parse_json(text: str):
    """Tool results are JSON API responses returned as text, but some servers
    prepend a human-readable line - try a straight parse first, then fall back
    to slicing from the first '{' or '['."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        candidates = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if not candidates:
            raise
        return json.loads(text[min(candidates):])


def _unwrap(parsed):
    """alpaca-mcp-server wraps every tool result in a security envelope:
        {"_alpaca_mcp_security": {...}, "data": <actual API response>}
    Unwrap `.data` before touching the real fields."""
    if isinstance(parsed, dict) and "_alpaca_mcp_security" in parsed:
        return parsed.get("data", parsed)
    return parsed


class AlpacaMcpClient:
    """Thin wrapper around a stdio connection to Alpaca's official MCP server
    (alpacahq/alpaca-mcp-server), spawned as a subprocess. Claude never talks
    to Alpaca's REST API directly - every read and order placement goes
    through this MCP tool surface.

    Tool names aren't hardcoded: alpaca-mcp-server is generated from Alpaca's
    OpenAPI spec, so `find_tool()` matches by keyword against whatever
    list_tools() returns, and every discovered tool is logged at connect time.
    """

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._stack = AsyncExitStack()
        self.session: ClientSession | None = None
        self.tools: list = []

    async def __aenter__(self) -> AlpacaMcpClient:
        params = StdioServerParameters(
            command="uvx",
            # alpaca-mcp-server's own dependency spec (fastmcp>=3.1.0) is
            # unbounded, so an unpinned invocation re-resolves to whatever
            # fastmcp is newest at container-start time - including a future
            # major version alpaca-mcp-server hasn't been updated for. Both
            # are pinned here so every start is deterministic.
            args=["--with", "fastmcp==3.4.7", "alpaca-mcp-server==2.3.0"],
            env={
                **os.environ,
                "ALPACA_API_KEY": self._api_key,
                "ALPACA_SECRET_KEY": self._secret_key,
                "ALPACA_PAPER_TRADE": "true" if self._paper else "false",
            },
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

        tools_response = await self.session.list_tools()
        self.tools = tools_response.tools
        log.info(f"connected to alpaca-mcp-server, {len(self.tools)} tools available")
        for tool in self.tools:
            log.info(f"tool available: {tool.name}")
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._stack.aclose()

    def anthropic_tools(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in self.tools
        ]

    def find_tool(self, keywords: list[str], exclude: tuple[str, ...] = ()) -> str | None:
        candidates = [
            tool.name
            for tool in self.tools
            if all(k in tool.name.lower() for k in keywords)
            and not any(x in tool.name.lower() for x in exclude)
        ]
        if not candidates:
            return None
        # Prefer the shortest match - the generic tool name is expected to be
        # shorter than more specific variants, if any exist.
        return min(candidates, key=len)

    async def call_tool(self, name: str, arguments: dict) -> str:
        assert self.session is not None, "call_tool() used outside 'async with AlpacaMcpClient(...)'"
        result = await self.session.call_tool(name, arguments)
        text_parts = [block.text for block in result.content if isinstance(block, TextContent)]
        output = "\n".join(text_parts)
        if getattr(result, "isError", False):
            log.warning(f"tool call errored name={name} arguments={arguments} output={output}")
        return output

    async def get_account(self) -> dict:
        tool_name = self.find_tool(["account"], exclude=("activities", "config"))
        if tool_name is None:
            raise RuntimeError("no account tool found on alpaca-mcp-server")
        return _unwrap(_parse_json(await self.call_tool(tool_name, {})))

    async def get_clock(self) -> dict:
        tool_name = self.find_tool(["clock"])
        if tool_name is None:
            raise RuntimeError("no clock tool found on alpaca-mcp-server")
        return _unwrap(_parse_json(await self.call_tool(tool_name, {})))

    async def get_last_trade_price(self, symbol: str) -> float | None:
        """Best-effort fetch of an underlying's last trade price, for the
        exit-plan invalidation check (position_monitor.py). Never raises -
        returns None on any failure, so a missing price just skips that
        cycle's invalidation check instead of crashing the whole pass."""
        tool_name = self.find_tool(["stock", "latest", "trade"])
        if tool_name is None:
            return None
        try:
            data = _unwrap(_parse_json(await self.call_tool(tool_name, {"symbols": symbol})))
        except Exception:
            log.exception(f"failed to fetch/parse last trade price for {symbol}")
            return None
        trades = data.get("trades", {}) if isinstance(data, dict) else {}
        price = (trades.get(symbol) or {}).get("p")
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    async def get_positions(self) -> list[dict]:
        tool_name = self.find_tool(["position"], exclude=("close",))
        if tool_name is None:
            raise RuntimeError("no positions tool found on alpaca-mcp-server")
        data = _unwrap(_parse_json(await self.call_tool(tool_name, {})))
        # get_all_positions nests the array as {"result": [...]}, not a bare list.
        if isinstance(data, list):
            return data
        return data.get("result", data.get("positions", []))
