"""
ClaudeRunner - AgentRunner implementation using Claude SDK.

Uses ClaudeSDKClient for persistent sessions and multi-turn conversations.
Tool specs live in tools/specs.py, implementations in tools/implementations.py.
This module wires them together via SdkMcpTool for the Claude SDK.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Set initialize timeout for Claude Agent SDK in the parent process.
# The SDK reads this from os.environ (not from ClaudeAgentOptions.env, which only
# propagates to the child subprocess). Without this, the default 60s timeout causes
# spurious "Control request timeout: initialize" crashes under concurrent load.
os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = "3600000"

from anthropic import APIStatusError, InternalServerError, RateLimitError
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookInput,
    HookMatcher,
    ResultMessage,
    SdkMcpTool,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from ..auth import ResolvedAuth
from ..state.registry import Registry
from ..state.task_store import TaskStore
from ..state.transcript import NoOpTranscriptLogger, TranscriptLoggerProtocol
from ..state.wire_store import WireStore
from ..tools import BUNDLES, FRAMEWORK, Bundle, ToolResponse
from ..tools.implementations import create_tool_implementations
from ..tools.mcp import create_agent_proxy, get_tool_names, resolve_mcp_tools
from .agent_runner import (
    ERROR_API_ERROR,
    ERROR_API_OVERLOADED,
    ERROR_RATE_LIMITED,
    AgentRunner,
    AgentRunResult,
)

logger = logging.getLogger(__name__)


# =============================================================================
# MCP Server Factory
# =============================================================================


def _to_mcp_response(result: ToolResponse) -> dict[str, Any]:
    """Convert a ToolResponse to MCP response format."""
    resp: dict[str, Any] = {"content": [{"type": "text", "text": result.text}]}
    if result.is_error:
        resp["is_error"] = True
    return resp


def _extract_tool_result_text(content: Any) -> str:
    """Extract readable text from a ToolResultBlock's content."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text", "") if isinstance(item, dict) else str(item) for item in content if item is not None]
        return "\n".join(parts)
    return str(content) if content is not None else ""


def create_framework_tools_server(
    task_store: TaskStore,
    registry: Registry,
    agent_id: str,
    role_tools: list[str],
    wire_store: WireStore,
) -> Any:
    """
    Create MCP server with framework tools for a specific agent.

    Wires ToolSpec metadata (from tools/specs.py) with implementation closures
    (from tools/implementations.py) into SdkMcpTool instances for the Claude SDK.

    Only registers tools that the agent's role has access to, so the model
    never sees tool definitions it cannot use (which causes retry loops).
    """
    impls = create_tool_implementations(task_store, registry, agent_id, wire_store=wire_store)

    role_tool_set = set(role_tools)
    sdk_tools = []
    for name, spec in FRAMEWORK.items():
        if name in role_tool_set:
            handler = impls[name]

            async def wrapped(args: Any, _handler=handler) -> dict[str, Any]:
                return _to_mcp_response(await _handler(args))

            sdk_tools.append(SdkMcpTool(spec.name, spec.description, spec.parameters, wrapped))

    return create_sdk_mcp_server(
        name="magelab",
        version="1.0.0",
        tools=sdk_tools,
    )


# =============================================================================
# Tool name resolution
# =============================================================================


def _build_post_tool_hooks(
    agent_id: str, hooks: list, transcript_logger: Optional[TranscriptLoggerProtocol] = None
) -> dict:
    """Convert framework post-tool hooks into SDK PostToolUse hook config for an agent."""
    matchers = []
    for fn in hooks:

        async def _hook(input: HookInput, tool_use_id: str | None, context: HookContext, _fn=fn) -> dict:
            text = _fn(agent_id)
            if not text:
                return {}
            if transcript_logger:
                transcript_logger.log_hook_output(agent_id, text)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": text,
                }
            }

        matchers.append(HookMatcher(hooks=[_hook]))

    return {"PostToolUse": matchers}


def build_allowed_tools(role_tools: list[str], framework_logger: Optional[logging.Logger] = None) -> list[str]:
    """
    Map role tool names to MCP-prefixed tool names for Claude SDK.

    - Framework tools (tasks_create, etc.) -> mcp__magelab__{name}
    - Claude native tools (Read, Write, etc.) -> pass through
    - Custom MCP tools (mcp__*) -> pass through
    """
    framework_logger = framework_logger or logging.getLogger(__name__)
    allowed_tools: list[str] = []

    for tool_name in role_tools:
        if tool_name in FRAMEWORK:
            allowed_tools.append(f"mcp__magelab__{tool_name}")
        elif tool_name in BUNDLES[Bundle.CLAUDE]:
            allowed_tools.append(tool_name)
        elif tool_name.startswith("mcp__"):
            allowed_tools.append(tool_name)
        else:
            framework_logger.warning(f"Unknown tool: {tool_name}")

    return allowed_tools


def build_disallowed_tools(role_tools: list[str]) -> list[str]:
    """
    Return native Claude tools that are NOT in the role's tool list.

    The SDK's allowed_tools doesn't restrict native tools, so we explicitly disallow the ones the role shouldn't have.
    """
    return [t for t in BUNDLES[Bundle.CLAUDE] if t not in role_tools]


# =============================================================================
# MCP proxy helpers
# =============================================================================


def _build_agent_mcp_proxies(
    agent_id: str,
    resolved_tools: list[str],
    mcp_servers: dict[str, Any],
) -> dict[str, Any]:
    """Create per-agent MCP proxy servers for in-process FastMCP servers.

    For each server, checks if the agent has any tools from it (by matching
    resolved tool names). If yes, creates a proxy that injects agent_id.

    Returns:
        Dict of server_name → SDK MCP server config. Empty if no proxies needed.
    """
    proxies: dict[str, Any] = {}
    for server_name, fastmcp_srv in mcp_servers.items():
        has_tools = any(t.startswith(f"mcp__{server_name}__") for t in resolved_tools)
        if has_tools:
            proxy = create_agent_proxy(server_name, fastmcp_srv, agent_id)
            proxies[server_name] = proxy.server
    return proxies


# =============================================================================
# Claude Runner
# =============================================================================


@dataclass
class _AgentRunConfig:
    """Per-agent configuration: MCP server, allowed tools, and limits."""

    mcp_server: Any
    allowed_tools: list[str]
    disallowed_tools: list[str]
    max_turns: int
    model: str
    hooks: Optional[dict] = None
    agent_mcp_servers: Optional[dict[str, Any]] = None


class ClaudeRunner(AgentRunner):
    """
    AgentRunner implementation using Claude SDK.

    Constructor takes task_store, registry, and optional FastMCP servers to build
    per-agent configs (MCP servers, allowed tools, proxies). Stores only the
    pre-built configs and sessions — not the stores themselves.
    """

    def __init__(
        self,
        task_store: TaskStore,
        registry: Registry,
        permission_mode: str,
        working_directory: str,
        agent_timeout_seconds: float,
        wire_store: WireStore,
        mcp_servers: Optional[dict[str, Any]] = None,
        transcript_logger: Optional[TranscriptLoggerProtocol] = None,
        framework_logger: Optional[logging.Logger] = None,
        post_tool_hooks: Optional[list] = None,
        auth: Optional[ResolvedAuth] = None,
    ) -> None:
        super().__init__(post_tool_hooks)
        self._permission_mode = permission_mode
        self._working_directory = working_directory
        self._agent_timeout_seconds = agent_timeout_seconds
        self.transcript_logger = transcript_logger or NoOpTranscriptLogger()
        self._framework_logger = framework_logger or logging.getLogger(__name__)
        self._sessions: dict[str, str] = {}
        self._active_clients: dict[str, ClaudeSDKClient] = {}

        self._api_key = auth.api_key if auth and auth.api_key else ""
        mcp_servers = mcp_servers or {}

        # Derive tool names from FastMCP servers for resolving mcp__<server> refs
        mcp_tool_names: dict[str, list[str]] = {name: get_tool_names(srv) for name, srv in mcp_servers.items()}

        # Build per-agent configs (stores are used here, then discarded)
        self._agent_configs: dict[str, _AgentRunConfig] = {}
        for agent_id in registry.list_agent_ids():
            agent = registry.get_agent_snapshot(agent_id)
            resolved_tools = resolve_mcp_tools(agent.tools, mcp_tool_names, self._framework_logger)
            agent_mcp_proxies = _build_agent_mcp_proxies(agent_id, resolved_tools, mcp_servers)

            mcp_server = create_framework_tools_server(
                task_store, registry, agent_id, resolved_tools, wire_store=wire_store
            )
            allowed_tools = build_allowed_tools(resolved_tools, self._framework_logger)
            disallowed_tools = build_disallowed_tools(resolved_tools)
            max_turns = registry.get_agent_max_turns(agent_id)
            hooks = (
                _build_post_tool_hooks(agent_id, self.post_tool_hooks, self.transcript_logger)
                if self.post_tool_hooks
                else None
            )

            self._agent_configs[agent_id] = _AgentRunConfig(
                mcp_server,
                allowed_tools,
                disallowed_tools,
                max_turns,
                agent.model,
                hooks,
                agent_mcp_proxies or None,
            )

    def get_session(self, agent_id: str) -> Optional[str]:
        """Return the current session ID for an agent, or None."""
        return self._sessions.get(agent_id)

    def restore_session(self, agent_id: str, session_id: str) -> None:
        """Restore a session ID for an agent (used during resume)."""
        self._sessions[agent_id] = session_id

    async def run_agent(
        self,
        agent_id: str,
        system_prompt: str,
        prompt: str,
    ) -> AgentRunResult:
        """
        Run an agent with a given system_prompt and prompt.

        Returns AgentRunResult with error (if any), cost, turns, and timing.
        """
        # Build options
        session_id = self._sessions.get(agent_id)
        config = self._agent_configs[agent_id]
        mcp_servers = {"magelab": config.mcp_server}
        if config.agent_mcp_servers:
            mcp_servers.update(config.agent_mcp_servers)
        options = ClaudeAgentOptions(
            model=config.model,
            system_prompt=system_prompt,
            mcp_servers=mcp_servers,
            allowed_tools=config.allowed_tools,
            disallowed_tools=config.disallowed_tools,
            permission_mode=self._permission_mode,
            max_turns=config.max_turns,
            cwd=self._working_directory,
            resume=session_id,
            hooks=config.hooks,
            env={
                "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT": "3600000",
                "CLAUDE_CONFIG_DIR": str(Path(self._working_directory).parent / ".sessions" / agent_id),
                **({"ANTHROPIC_API_KEY": self._api_key} if self._api_key else {}),
            },
        )

        result = AgentRunResult()

        # Log system prompt on first run only, then the event prompt
        if session_id is None:
            self.transcript_logger.log_system_prompt(agent_id, system_prompt)
        self.transcript_logger.log_prompt(agent_id, prompt)

        try:
            async with ClaudeSDKClient(options=options) as client:
                # Track active client for interrupt support
                self._active_clients[agent_id] = client

                try:
                    await client.query(prompt)
                    result = await asyncio.wait_for(
                        self._process_response(client, agent_id, config),
                        timeout=self._agent_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    result.error = f"Agent timed out after {self._agent_timeout_seconds}s"
                    result.timed_out = True
                    self._framework_logger.warning(f"Agent {agent_id} timed out")
                    self.transcript_logger.log_run_complete(agent_id, 0, None, result.error)
                finally:
                    # Always remove from active clients when done
                    self._active_clients.pop(agent_id, None)

        except asyncio.CancelledError:
            self.transcript_logger.log_run_complete(agent_id, 0, None, "Agent force-cancelled")
            raise
        except RateLimitError as e:
            self._framework_logger.error(f"Agent {agent_id} rate limited (429): {e.message}")
            result.error = f"{ERROR_RATE_LIMITED} (429): {e.message}"
            self.transcript_logger.log_run_complete(agent_id, 0, None, result.error)
        except InternalServerError as e:
            self._framework_logger.error(f"Agent {agent_id} API overloaded ({e.status_code}): {e.message}")
            result.error = f"{ERROR_API_OVERLOADED} ({e.status_code}): {e.message}"
            self.transcript_logger.log_run_complete(agent_id, 0, None, result.error)
        except APIStatusError as e:
            self._framework_logger.error(f"Agent {agent_id} API error ({e.status_code}): {e.message}")
            result.error = f"{ERROR_API_ERROR} ({e.status_code}): {e.message}"
            self.transcript_logger.log_run_complete(agent_id, 0, None, result.error)
        except Exception as e:
            self._framework_logger.exception(f"Agent {agent_id} run failed with unexpected error")
            result.error = str(e)
            self.transcript_logger.log_run_complete(agent_id, 0, None, result.error)

        return result

    async def _process_response(
        self,
        client: ClaudeSDKClient,
        agent_id: str,
        config: _AgentRunConfig,
    ) -> AgentRunResult:
        """
        Process the SDK response stream: log messages and build result.

        Returns AgentRunResult with error (if any), cost, turns, and timing.
        """
        result = AgentRunResult()

        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        self.transcript_logger.log_assistant_text(agent_id, block.text)
                        self._framework_logger.debug(f"[{agent_id}] {block.text[:200]}...")
                    elif isinstance(block, ToolUseBlock):
                        self.transcript_logger.log_tool_call(
                            agent_id,
                            block.name,
                            block.input if isinstance(block.input, dict) else {},
                        )

            elif isinstance(message, UserMessage):
                for block in message.content:
                    if isinstance(block, ToolResultBlock):
                        self.transcript_logger.log_tool_result(
                            agent_id,
                            _extract_tool_result_text(block.content),
                            is_error=block.is_error or False,
                        )

            elif isinstance(message, ResultMessage):
                error = None
                if message.is_error:
                    error = message.result
                elif message.num_turns >= config.max_turns:
                    error = f"Agent exhausted max_turns ({config.max_turns})"

                result = AgentRunResult(
                    error=error,
                    num_turns=message.num_turns,
                    cost_usd=message.total_cost_usd,
                    duration_ms=message.duration_ms,
                    session_id=message.session_id,
                )

                self._framework_logger.info(
                    f"Agent {agent_id} finished: turns={message.num_turns}, cost=${message.total_cost_usd or 0:.4f}"
                )
                self.transcript_logger.log_run_complete(
                    agent_id,
                    message.num_turns,
                    message.total_cost_usd,
                    error,
                )
                if message.session_id:
                    self._sessions[agent_id] = message.session_id

        return result

    async def interrupt_agent(self, agent_id: str) -> None:
        """
        Interrupt a running agent by sending an interrupt signal to its active client.

        Safe to call even if the agent is not currently running - will be a no-op in that case.

        Args:
            agent_id: The agent to interrupt
        """
        client = self._active_clients.get(agent_id)
        if client:
            try:
                await client.interrupt()
                self._framework_logger.info(f"Sent interrupt signal to agent {agent_id}")
            except Exception as e:
                self._framework_logger.warning(f"Failed to interrupt agent {agent_id}: {e}")
        else:
            self._framework_logger.debug(f"Agent {agent_id} not running, no interrupt needed")

    def shutdown(self) -> None:
        """Close transcript logger file handles."""
        self.transcript_logger.close()
