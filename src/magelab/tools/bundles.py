"""
Tool bundles for YAML config convenience.

Bundles group tools by role, allowing YAML configs to use
bundle names instead of listing individual tools.

    worker:                submit for review + discover reviewers + mark finished
    management:            create, assign, query, finish tasks + discover agents
    claude_reviewer:       conduct reviews with code reading + execution tools
    passive_claude_reviewer: conduct reviews with code reading tools (no execution)
    coordination:          sleep
    claude_basic:          core Claude Code tools (read, write, edit, bash, etc.)
    claude:                all Claude Code built-in tools (used for disallow list)
"""

from enum import Enum

from . import specs


class Bundle(str, Enum):
    """Tool bundles/aliases usable in YAML tool configs.

    YAML example:
        tools: [management, claude_basic]
        tools: [worker, claude_reviewer, claude_basic]
        tools: [passive_claude_reviewer]
    """

    WORKER = "worker"
    MANAGEMENT = "management"
    MANAGEMENT_NOBATCH = "management_nobatch"
    CLAUDE_REVIEWER = "claude_reviewer"
    PASSIVE_CLAUDE_REVIEWER = "passive_claude_reviewer"
    COORDINATION = "coordination"
    CLAUDE_BASIC = "claude_basic"
    CLAUDE = "claude"
    COMMUNICATION = "communication"


BUNDLES: dict[str, list[str]] = {
    Bundle.WORKER: [
        specs.tasks_submit_for_review.name,
        specs.tasks_mark_finished.name,
        specs.get_available_reviewers.name,
    ],
    Bundle.MANAGEMENT: [
        specs.tasks_create_batch.name,
        specs.tasks_assign.name,
        specs.tasks_get.name,
        specs.tasks_list.name,
        specs.tasks_mark_finished.name,
        specs.connections_list.name,
    ],
    Bundle.MANAGEMENT_NOBATCH: [
        specs.tasks_create.name,
        specs.tasks_assign.name,
        specs.tasks_get.name,
        specs.tasks_list.name,
        specs.tasks_mark_finished.name,
        specs.connections_list.name,
    ],
    Bundle.CLAUDE_REVIEWER: [
        specs.tasks_submit_review.name,
        "Read",
        "Grep",
        "Glob",
        "Bash",
    ],
    Bundle.PASSIVE_CLAUDE_REVIEWER: [
        specs.tasks_submit_review.name,
        "Read",
        "Grep",
        "Glob",
    ],
    Bundle.COORDINATION: [specs.sleep.name],
    Bundle.COMMUNICATION: [
        specs.connections_list.name,
        specs.send_message.name,
        specs.read_messages.name,
        specs.batch_read_messages.name,
        specs.conversations_list.name,
    ],
    Bundle.CLAUDE_BASIC: [
        "Agent",
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "NotebookEdit",
        "TodoWrite",
    ],
    # Complete list of Claude Code built-in tools.
    # build_disallowed_tools blocks any tool in this list that a role doesn't
    # explicitly include. If a tool is missing from here it leaks through to
    # every agent unchecked.
    # Reference: https://code.claude.com/docs/en/tools-reference
    Bundle.CLAUDE: [
        "Agent",
        "AskUserQuestion",
        "Bash",
        "CronCreate",
        "CronDelete",
        "CronList",
        "Edit",
        "EnterPlanMode",
        "EnterWorktree",
        "ExitPlanMode",
        "ExitWorktree",
        "Glob",
        "Grep",
        "ListMcpResourcesTool",
        "LSP",
        "NotebookEdit",
        "Read",
        "ReadMcpResourceTool",
        "Skill",
        "TaskCreate",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "TaskStop",
        "TaskUpdate",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
        "Write",
    ],
}


def expand(tool_list: list[str], *, strict: bool = True) -> list[str]:
    """Expand bundle names into individual tool names. Deduplicates, preserving order.

    Args:
        tool_list: List of bundle names and/or individual tool names.
        strict: If True (default), raise ValueError for names that are neither
                a known bundle nor a known tool (framework or Claude).
    """
    expanded: list[str] = []
    seen: set[str] = set()
    for tool in tool_list:
        if tool in BUNDLES:
            names = BUNDLES[tool]
        else:
            if strict and tool not in _KNOWN_TOOLS and not tool.startswith("mcp__"):
                raise ValueError(f"Unknown tool or bundle '{tool}'. Known bundles: {sorted(b.value for b in Bundle)}. ")
            names = [tool]
        for name in names:
            if name not in seen:
                seen.add(name)
                expanded.append(name)
    return expanded


# All known individual tool names (framework + Claude) for strict validation
_KNOWN_TOOLS: set[str] = set()
for _bundle_tools in BUNDLES.values():
    _KNOWN_TOOLS.update(_bundle_tools)
_KNOWN_TOOLS.update(specs.FRAMEWORK.keys())
