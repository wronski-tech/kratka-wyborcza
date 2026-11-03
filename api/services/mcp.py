"""Minimal MCP server for agents: JSON-RPC over Streamable HTTP (``POST /mcp``), JSON responses only.

The tools reuse the API's own paths, so agents get the same guards as the UI: ``ask`` is ``POST /ask``,
``query`` runs agent-written SQL through ``validate_mart_sql``, the cost limit and the read-only role,
and ``schema`` returns the ``COMMENT ON`` descriptions the model is prompted with.
"""

from __future__ import annotations

import json
from typing import Any, Callable

# Newest first; an unknown requested version gets the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Polish election results (PKW/KBW) in PostgreSQL. Use `ask` for a question in Polish or English; "
    "use `schema` and then `query` to run your own read-only SQL over the `mart` schema. "
    "Report only numbers that the tools returned."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "ask",
        "description": "Answer a question about Polish elections like the Kratka Wyborcza UI: the question is turned into SQL "
                       "over the mart, run read-only, and the rows are described in Polish. Returns the answer, "
                       "the rows, the SQL and a clarification when the question is ambiguous.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question, e.g. 'Frekwencja w wyborach do Sejmu 2023'"},
                "previous": {"type": "object", "description": "Previous turn {question, sql} for a follow-up"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "schema",
        "description": "Loaded elections and the mart relations with their column descriptions. With a question, "
                       "only the relations relevant to it; without one, every relation in short form.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "Optional question to narrow the relations"}},
        },
    },
    {
        "name": "query",
        "description": "Run one read-only SELECT over the mart schema (mart.* relations only, no functions with side "
                       "effects, row limit, cost limit, statement timeout). Returns the checked SQL and its rows.",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT over mart relations"}},
            "required": ["sql"],
        },
    },
]


class ToolError(Exception):
    """A tool failure the agent should read (an ``isError`` result), such as a rejected query."""


def _error(message_id: Any, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}}


def handle(message: Any, tools: dict[str, Callable[[dict], dict]], version: str) -> dict | None:
    """JSON-RPC response to one MCP message; None for a notification (no ``id``)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
    if "id" not in message:
        return None
    message_id, method, params = message["id"], message["method"], message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        result: dict[str, Any] = {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "kratka-wyborcza", "version": version},
            "instructions": INSTRUCTIONS,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        tool = tools.get(str(params.get("name")))
        arguments = params.get("arguments") or {}
        if tool is None or not isinstance(arguments, dict):
            return _error(message_id, -32602, f"Unknown tool or bad arguments: {params.get('name')}")
        try:
            value = tool(arguments)
        except ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
            result = {"content": [{"type": "text", "text": text}], "structuredContent": value, "isError": False}
    else:
        return _error(message_id, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": message_id, "result": result}
