"""
MCP server management — module loading, per-agent proxy creation, and tool resolution.

In-process MCP servers are declared as mcp_modules in the org config. Each module
exposes a standard FastMCP server instance. The framework introspects its tools,
creates per-agent proxies that inject agent_id, and manages the lifecycle.

External MCP servers (Slack, GitHub, etc.) are configured in per-role settings
files and handled entirely by the LLM backend.

Convention: tools that declare an ``agent_id`` parameter in their schema get it
auto-injected by the framework — the agent never sees or provides it. Tools
without ``agent_id`` pass through unchanged.

Concurrency: multiple agents call the same FastMCP server concurrently. Tool
handlers must be async-safe (no shared mutable state without locks).
"""

import copy
import importlib
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from mcp.server.fastmcp import FastMCP

from ..events import BaseEvent
from ..state.database import Database

_logger = logging.getLogger(__name__)


# =============================================================================
# Module loading
# =============================================================================


@dataclass(frozen=True)
class LoadedMCPModule:
    """Result of loading an MCP module — the server and its source module."""

    server: FastMCP
    module: Any  # the imported Python module


def load_mcp_module(module_path: str) -> LoadedMCPModule:
    """Import a Python module and return its FastMCP server and module.

    Convention: the module must expose a ``server`` attribute that is a
    FastMCP instance. May optionally expose an ``init(context: MCPContext)``
    function for persistence and event emission.

    Args:
        module_path: Dotted Python module path (e.g. "experiments.voting.server").

    Raises:
        ValueError: If the module doesn't expose a ``server`` attribute or it's
            not a FastMCP instance.
        ImportError: If the module can't be imported.
    """
    mod = importlib.import_module(module_path)
    if not hasattr(mod, "server"):
        raise ValueError(f"MCP module '{module_path}' must expose a 'server' attribute (a FastMCP instance).")
    srv = mod.server
    if not isinstance(srv, FastMCP):
        raise ValueError(
            f"MCP module '{module_path}' has a 'server' attribute but it's "
            f"a {type(srv).__name__}, not a FastMCP instance."
        )
    return LoadedMCPModule(server=srv, module=mod)


# =============================================================================
# MCP server lifecycle
# =============================================================================


@dataclass(frozen=True)
class MCPContext:
    """Context passed to MCP server ``init()`` functions.

    Provides access to the run's database (for schema registration and
    persistence) and an event emitter (for proactively notifying agents).

    Args:
        db: The run's Database instance. Use ``db.register_schema(ddl)``
            to create tables, then ``db.execute/fetchone/fetchall`` for
            reads and writes.
        emit_event: Callback that feeds events into the orchestrator's
            dispatch pipeline. Events are enqueued to the target agent
            and logged to the DB. Note: if the target agent is currently
            running, the event queues until that turn ends — it is not
            a synchronous interrupt.
    """

    db: Database
    emit_event: Callable[[BaseEvent], None]


def init_mcp_servers(
    loaded_modules: dict[str, LoadedMCPModule],
    context: MCPContext,
    framework_logger: Optional[logging.Logger] = None,
) -> None:
    """Call ``init(context)`` on MCP modules that define it.

    Modules that define an ``init`` callable receive the MCPContext for
    DB access and event emission. Modules without ``init`` are pure
    stateless tool providers and are left as-is.

    Args:
        loaded_modules: Server name → LoadedMCPModule from load_mcp_module().
        context: The MCPContext to pass to each init function.
        framework_logger: Logger for diagnostics.

    Raises:
        RuntimeError: If any init() call fails.
    """
    log = framework_logger or _logger

    for server_name, loaded in loaded_modules.items():
        init_fn = getattr(loaded.module, "init", None)
        if init_fn is None:
            continue

        if not callable(init_fn):
            raise RuntimeError(f"MCP module '{server_name}' has an 'init' attribute but it's not callable")
        if inspect.iscoroutinefunction(init_fn):
            raise RuntimeError(
                f"MCP module '{server_name}' defines 'init' as async. init(context) must be a synchronous function."
            )

        try:
            init_fn(context)
            log.info(f"Initialized MCP module '{server_name}'")
        except Exception as e:
            raise RuntimeError(f"Failed to initialize MCP module '{server_name}': {e}") from e


# =============================================================================
# Per-agent proxy with agent_id injection
# =============================================================================


def _has_agent_id_param(schema: dict) -> bool:
    """Check if a tool's schema declares an agent_id property."""
    return "agent_id" in schema.get("properties", {})


def _strip_agent_id(schema: dict) -> dict:
    """Return a copy of the schema with agent_id removed from properties and required."""
    schema = copy.deepcopy(schema)
    schema.get("properties", {}).pop("agent_id", None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r != "agent_id"]
    return schema


def get_tool_names(fastmcp_server: FastMCP) -> list[str]:
    """Get tool names from a FastMCP server (sync, no event loop needed).

    Uses _tool_manager.list_tools() — the top-level list_tools() is async
    but the tool manager's version is sync. The _tool_manager access is
    an internal path; verify after upgrading fastmcp.
    """
    return [t.name for t in fastmcp_server._tool_manager.list_tools()]


@dataclass
class AgentProxy:
    """Result of create_agent_proxy — the SDK server config plus tool metadata."""

    server: Any
    """SDK MCP server dict for passing to mcp_servers in agent options."""

    tools: list[SdkMcpTool]
    """The proxy SdkMcpTool instances (useful for testing and introspection)."""


def create_agent_proxy(
    server_name: str,
    fastmcp_server: FastMCP,
    agent_id: str,
) -> AgentProxy:
    """Create a per-agent MCP proxy server from a FastMCP server.

    Introspects the FastMCP server's tool registry, strips ``agent_id`` from
    each tool's schema (so the agent never sees it), and creates wrapper
    handlers that inject the real agent_id on every call.

    Args:
        server_name: Name for the proxy server (used in MCP tool prefixing).
        fastmcp_server: The FastMCP server to proxy.
        agent_id: The agent ID to inject into every tool call.

    Returns:
        AgentProxy with the SDK server config and tool metadata.
    """
    sdk_tools = []

    # Uses _tool_manager.list_tools() — see get_tool_names() for rationale.
    for tool in fastmcp_server._tool_manager.list_tools():
        name = tool.name
        schema = tool.parameters
        inject = _has_agent_id_param(schema)
        proxy_schema = _strip_agent_id(schema) if inject else copy.deepcopy(schema)

        async def proxy_handler(
            args: dict,
            _name=name,
            _agent_id=agent_id,
            _server=fastmcp_server,
            _inject=inject,
        ) -> dict:
            call_args = {"agent_id": _agent_id, **args} if _inject else args
            result = await _server.call_tool(_name, call_args)
            # call_tool returns (content_list, extras) — extract text
            content_list = result[0] if isinstance(result, tuple) else result
            text_parts = [c.text for c in content_list if hasattr(c, "text")]
            return {"content": [{"type": "text", "text": "\n".join(text_parts)}]}

        sdk_tools.append(SdkMcpTool(name, tool.description or "", proxy_schema, proxy_handler))

    server = create_sdk_mcp_server(
        name=server_name,
        version="1.0.0",
        tools=sdk_tools,
    )
    return AgentProxy(server=server, tools=sdk_tools)


# =============================================================================
# Tool reference resolution
# =============================================================================


def resolve_mcp_tools(
    tools: list[str],
    available_tools: dict[str, list[str]],
    framework_logger: Optional[logging.Logger] = None,
) -> list[str]:
    """Expand server-level MCP references to concrete tool names.

    Handles two forms:
      - ``mcp__<server>`` — expands to all tools from that server (in-process only)
      - ``mcp__<server>__<tool>`` — passes through as-is

    Server-level references to unknown servers (e.g. external servers configured
    in settings) pass through as-is.

    Args:
        tools: Role's tool list (may contain mcp__<server> and mcp__<server>__<tool>).
        available_tools: Mapping of server_name → list of tool names. Built from
            in-process FastMCP servers loaded from mcp_modules.
        framework_logger: Logger for diagnostics.

    Returns:
        New tool list with known server-level references expanded.
    """
    log = framework_logger or _logger
    resolved: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name not in seen:
            seen.add(name)
            resolved.append(name)

    for tool in tools:
        if not tool.startswith("mcp__"):
            _add(tool)
            continue

        parts = tool.split("__")
        server_name = parts[1]

        if len(parts) == 2:
            if server_name in available_tools:
                for tool_name in available_tools[server_name]:
                    _add(f"mcp__{server_name}__{tool_name}")
            else:
                log.debug(f"MCP server-level ref '{tool}' not in available tools, passing through")
                _add(tool)
        else:
            _add(tool)

    return resolved
