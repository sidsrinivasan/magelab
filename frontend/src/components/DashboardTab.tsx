import { useEffect, useMemo, useState } from "react";
import { useStore } from "../store";
import { stateColor, stateLabel, senderColor, taskStatusColor } from "../lib/theme";
import { RolePopover } from "./RolePopover";
import { TranscriptView } from "./TranscriptView";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { AgentSnapshot, QueueEvent, TranscriptEntry, Wire } from "../types";

function AgentRow({
  agent,
  isSelected,
  onClick,
}: {
  agent: AgentSnapshot;
  isSelected: boolean;
  onClick: () => void;
}) {
  const tasks = useStore((s) => s.tasks);
  const transcripts = useStore((s) => s.transcripts);

  const currentTask = agent.current_task_id ? tasks[agent.current_task_id] : null;
  const agentTranscript: TranscriptEntry[] = transcripts[agent.agent_id] || [];
  const lastEntry = agentTranscript[agentTranscript.length - 1];

  const color = stateColor(agent.state);
  const pulseClass =
    agent.state === "working"
      ? "pulse-working"
      : agent.state === "reviewing"
        ? "pulse-reviewing"
        : "";

  return (
    <button
      onClick={onClick}
      className={`w-full text-left p-3 rounded-lg transition-all overflow-hidden ${
        isSelected
          ? "bg-primary/8 border border-primary/20"
          : "hover:bg-secondary/40 border border-transparent"
      }`}
    >
      {/* Row 1: agent ID + state */}
      <div className="flex items-center gap-2 mb-1.5">
        <span
          className={`inline-block w-2 h-2 rounded-full shrink-0 ${pulseClass}`}
          style={{ backgroundColor: color }}
        />
        <span className="font-mono text-xs font-medium text-foreground">
          {agent.agent_id}
        </span>
        <span
          className="text-[9px] font-medium uppercase tracking-wider ml-auto"
          style={{ color }}
        >
          {stateLabel(agent.state)}
        </span>
      </div>

      {/* Row 2: role badge */}
      <div className="flex items-center gap-2 mb-1">
        <RolePopover roleName={agent.role} />
      </div>
      {/* Row 3: model */}
      <div className="mb-1.5">
        <span className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-secondary/50 text-muted-foreground/60">
          {agent.model}
        </span>
      </div>

      {/* Row 3: current task */}
      {currentTask && (
        <div className="text-[11px] text-muted-foreground truncate mb-1">
          <span className="opacity-50">Task:</span>{" "}
          <span className="text-foreground/70">{currentTask.title}</span>
        </div>
      )}

      {/* Row 4: last transcript preview */}
      {lastEntry && (
        <div className="text-[10px] text-muted-foreground/50 truncate font-mono">
          {lastEntry.entry_type === "tool_call" && (
            <span className="text-primary/40">[tool] </span>
          )}
          {lastEntry.content.slice(0, 100)}
        </div>
      )}
    </button>
  );
}

function WireDropdown({ agentWires }: { agentWires: Wire[] }) {
  const [open, setOpen] = useState(false);
  const setSelectedWire = useStore((s) => s.setSelectedWire);
  const setActiveTab = useStore((s) => s.setActiveTab);

  // Sort by most recent message first
  const sorted = useMemo(() =>
    [...agentWires].sort((a, b) => {
      const aLast = a.messages[a.messages.length - 1]?.timestamp || "";
      const bLast = b.messages[b.messages.length - 1]?.timestamp || "";
      return bLast.localeCompare(aLast);
    }),
  [agentWires]);

  return (
    <div className="relative inline-block">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-primary/60 hover:text-primary transition-colors cursor-pointer"
      >
        <span>{agentWires.length} wire{agentWires.length > 1 ? "s" : ""}</span>
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute z-50 top-full left-0 mt-1 w-64 max-h-[320px] rounded-lg border border-border/50 bg-card shadow-xl flex flex-col overflow-hidden">
            <div className="overflow-y-auto py-1">
              {sorted.map((wire) => (
                <button
                  key={wire.wire_id}
                  onClick={() => {
                    setSelectedWire(wire.wire_id);
                    setActiveTab("wires");
                    setOpen(false);
                  }}
                  className="w-full text-left px-3 py-2 hover:bg-secondary/50 transition-colors"
                >
                  <div className="font-mono text-xs font-medium text-foreground">
                    {wire.wire_id}
                  </div>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {wire.participants.map((p) => (
                      <span
                        key={p}
                        className="text-[10px] font-mono px-1.5 py-0.5 rounded-full bg-secondary/60 text-foreground/70"
                        style={{ borderLeft: `2px solid ${senderColor(p)}` }}
                      >
                        {p}
                      </span>
                    ))}
                  </div>
                  <div className="text-[10px] text-muted-foreground/50 mt-1">
                    {wire.messages.length} messages
                  </div>
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function AgentInfoHeader({ agent, queueOpen, onToggleQueue, queueCount }: {
  agent: AgentSnapshot;
  queueOpen: boolean;
  onToggleQueue: () => void;
  queueCount: number;
}) {
  const tasks = useStore((s) => s.tasks);
  const wires = useStore((s) => s.wires);
  const [showAllTools, setShowAllTools] = useState(false);

  const agentTasks = Object.values(tasks).filter(
    (t) => t.assigned_to === agent.agent_id || t.assignment_history?.includes(agent.agent_id),
  );
  const agentWires = Object.values(wires).filter((w) =>
    w.participants.includes(agent.agent_id),
  );

  const color = stateColor(agent.state);
  const TOOL_LIMIT = 6;
  const visibleTools = showAllTools ? agent.tools : agent.tools.slice(0, TOOL_LIMIT);
  const hiddenCount = agent.tools.length - TOOL_LIMIT;

  return (
    <div className="p-4 border-b border-border/30 space-y-3">
      {/* Agent identity */}
      <div className="flex items-center gap-3">
        <span
          className={`inline-block w-3 h-3 rounded-full shrink-0 ${
            agent.state === "working"
              ? "pulse-working"
              : agent.state === "reviewing"
                ? "pulse-reviewing"
                : ""
          }`}
          style={{ backgroundColor: color }}
        />
        <span className="font-mono text-base font-semibold text-foreground">
          {agent.agent_id}
        </span>
        <span
          className="text-[10px] font-medium uppercase tracking-wider"
          style={{ color }}
        >
          {stateLabel(agent.state)}
        </span>
        <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-secondary/50 text-muted-foreground/70">
          {agent.model}
        </span>
      </div>

      {/* Role + tools */}
      <div className="flex items-center gap-2 flex-wrap">
        <RolePopover roleName={agent.role} />
        {visibleTools.map((tool) => (
          <span
            key={tool}
            className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-secondary/40 text-muted-foreground/60"
          >
            {tool}
          </span>
        ))}
        {hiddenCount > 0 && (
          <button
            onClick={() => setShowAllTools(!showAllTools)}
            className="text-[9px] text-primary/60 hover:text-primary transition-colors cursor-pointer"
          >
            {showAllTools ? "show less" : `+${hiddenCount} more`}
          </button>
        )}
      </div>

      {/* Tasks + Wires summary row */}
      <div className="flex gap-4 text-[10px]">
        {agentTasks.length > 0 && (
          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground/50">Tasks</span>
            {agentTasks.map((task) => (
              <span key={task.id} className="flex items-center gap-1">
                <TaskStatusDot status={task.status} />
                <span className="font-mono text-foreground/60">{task.title || task.id}</span>
              </span>
            ))}
          </div>
        )}
        {agentWires.length > 0 && (
          <WireDropdown agentWires={agentWires} />
        )}
        <button
          onClick={onToggleQueue}
          className={`text-[10px] px-2 py-0.5 rounded border transition-colors ml-auto ${
            queueOpen
              ? "border-primary/30 bg-primary/10 text-primary"
              : "border-border/30 hover:bg-secondary/30 text-muted-foreground/60"
          }`}
        >
          Queue ({queueCount})
        </button>
      </div>
    </div>
  );
}

function TaskStatusDot({ status }: { status: string }) {
  return (
    <span
      className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
      style={{ backgroundColor: taskStatusColor(status) }}
    />
  );
}

function InitialTasksBar() {
  const initialTasks = useStore((s) => s.initialTasks);
  const setSelectedAgent = useStore((s) => s.setSelectedAgent);
  const [collapsed, setCollapsed] = useState(false);

  if (initialTasks.length === 0) return null;

  return (
    <div style={{ padding: "8px 12px 8px 10px" }} className="border-b border-border/20 bg-secondary/20">
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="flex items-center gap-2 w-full text-left"
      >
        <span className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground/60">
          Initial Task Assignees
        </span>
        <span className="text-[9px] text-muted-foreground/40">
          {collapsed ? "+" : "\u2212"}
        </span>
      </button>
      {!collapsed && (
        <div className="flex flex-wrap gap-1.5 mt-2">
          {initialTasks.map((t) => (
            <button
              key={t.task_id}
              onClick={() => setSelectedAgent(t.assigned_to)}
              className="text-[11px] font-mono font-medium px-2.5 py-1 rounded-full bg-secondary/50 border border-border/30 text-foreground/80 hover:bg-primary/10 hover:border-primary/20 transition-colors"
              style={{ borderLeft: `2px solid ${senderColor(t.assigned_to)}` }}
              title={t.title}
            >
              {t.assigned_to}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Queue panel ─────────────────────────────────────────────────────

function QueueEventEntry({ event }: { event: QueueEvent }) {
  const [expanded, setExpanded] = useState(false);
  const source = (event.payload.source_id as string) || null;

  return (
    <div
      className="border border-border/20 rounded-lg p-3 cursor-pointer hover:bg-secondary/20 transition-colors"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-center gap-2">
        <span className="text-[11px] font-mono font-bold uppercase tracking-wider text-primary/70">
          {event.event_type.replace(/Event$/, "")}
        </span>
        {source && (
          <span className="text-[11px] text-muted-foreground">from {source}</span>
        )}
        <span className="text-[11px] text-muted-foreground/50 ml-auto font-mono">
          {new Date(event.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
        </span>
      </div>
      {expanded && Object.keys(event.payload).length > 0 && (
        <div className="mt-2 space-y-0.5 border-t border-border/20 pt-2">
          {Object.entries(event.payload).map(([key, val]) => (
            <div key={key} className="flex gap-2 text-[13px]">
              <span className="text-muted-foreground/70 shrink-0">{key}:</span>
              <span className="text-foreground/80 break-all">
                {typeof val === "string" ? val : JSON.stringify(val)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function QueuePanel({ agentId }: { agentId: string }) {
  const queue = useStore((s) => s.queues[agentId] || []);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <div className="px-4 py-2 border-b border-border/30 flex items-center gap-2 shrink-0">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
          Event Queue
        </span>
        <span className="text-[10px] text-muted-foreground/40">{queue.length}</span>
      </div>
      {queue.length === 0 ? (
        <div className="flex items-center justify-center flex-1 text-xs text-muted-foreground/50">
          No pending events
        </div>
      ) : (
        <div className="flex-1 min-h-0">
          <ScrollArea className="h-full">
            <div className="p-3 space-y-2">
              {queue.map((event) => (
                <QueueEventEntry key={event.event_id} event={event} />
              ))}
            </div>
          </ScrollArea>
        </div>
      )}
    </div>
  );
}

export function DashboardTab() {
  const agents = useStore((s) => s.agents);
  const selectedAgentId = useStore((s) => s.selectedAgentId);
  const setSelectedAgent = useStore((s) => s.setSelectedAgent);
  const transcripts = useStore((s) => s.transcripts);
  const [queueOpen, setQueueOpen] = useState(false);

  const agentList = Object.values(agents);
  const selectedAgent = selectedAgentId ? agents[selectedAgentId] : null;
  const selectedTranscript = selectedAgentId ? transcripts[selectedAgentId] || [] : [];
  const queueCount = useStore((s) =>
    selectedAgentId ? (s.queues[selectedAgentId] || []).length : 0
  );

  // Auto-select first agent if none selected
  useEffect(() => {
    if (!selectedAgentId && agentList.length > 0) {
      setSelectedAgent(agentList[0].agent_id);
    }
  }, [selectedAgentId, agentList, setSelectedAgent]);

  if (agentList.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-muted-foreground gap-2">
        <div className="w-8 h-8 border-2 border-muted-foreground/30 border-t-primary rounded-full animate-spin" />
        <span className="text-sm mt-2">Waiting for agents...</span>
      </div>
    );
  }

  return (
    <div className="flex h-full">
      {/* Left sidebar: agent list */}
      <div className="w-72 border-r border-border/30 flex flex-col shrink-0 min-h-0">
        <InitialTasksBar />
        <div className="flex-1 min-h-0 overflow-y-auto" style={{ scrollbarWidth: "thin", scrollbarColor: "hsl(var(--border)) transparent" }}>
          <div className="space-y-0.5" style={{ padding: "8px 12px 8px 10px" }}>
            {agentList.map((agent) => (
              <AgentRow
                key={agent.agent_id}
                agent={agent}
                isSelected={agent.agent_id === selectedAgentId}
                onClick={() => setSelectedAgent(agent.agent_id)}
              />
            ))}
          </div>
        </div>
      </div>

      {/* Right main area: agent info + transcript + optional queue */}
      <div className="flex-1 flex min-w-0 min-h-0 overflow-hidden" style={{ marginLeft: 4 }}>
        {selectedAgent ? (
          <>
            {/* Transcript column */}
            <div className={`flex-1 flex flex-col min-w-0 min-h-0 ${queueOpen ? "border-r border-border/30" : ""}`}>
              <AgentInfoHeader
                agent={selectedAgent}
                queueOpen={queueOpen}
                onToggleQueue={() => setQueueOpen(!queueOpen)}
                queueCount={queueCount}
              />
              <div className="flex-1 min-h-0 relative">
                <TranscriptView entries={selectedTranscript} />
                <div className="absolute bottom-0 left-0 right-0 h-16 bg-gradient-to-t from-background to-transparent pointer-events-none" />
              </div>
            </div>

            {/* Queue panel */}
            {queueOpen && selectedAgentId && (
              <div className="w-[30%] min-w-[280px] flex flex-col min-h-0 overflow-hidden">
                <QueuePanel agentId={selectedAgentId} />
              </div>
            )}
          </>
        ) : (
          <div className="flex items-center justify-center h-full w-full text-muted-foreground/40 text-sm">
            Select an agent to view transcript
          </div>
        )}
      </div>
    </div>
  );
}
