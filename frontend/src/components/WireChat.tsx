import { useState } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useStore } from "../store";
import { senderColor } from "../lib/theme";
import { RolePopover } from "./RolePopover";

const DEFAULT_VISIBLE_PARTICIPANTS = 4;

function ParticipantBubble({ agentId }: { agentId: string }) {
  const agents = useStore((s) => s.agents);
  const agent = agents[agentId];

  return (
    <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-secondary/50 border border-border/30">
      <span
        className="w-2 h-2 rounded-full shrink-0"
        style={{ backgroundColor: senderColor(agentId) }}
      />
      <span className="text-xs font-mono font-medium text-foreground/80">
        {agentId}
      </span>
      {agent && <RolePopover roleName={agent.role} />}
    </div>
  );
}

export function WireChat() {
  const wires = useStore((s) => s.wires);
  const selectedWireId = useStore((s) => s.selectedWireId);
  const [showAllParticipants, setShowAllParticipants] = useState(false);

  const wire = selectedWireId ? wires[selectedWireId] : null;

  if (!wire) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/40 text-sm">
        Select a wire to view messages
      </div>
    );
  }

  const hasMany = wire.participants.length > DEFAULT_VISIBLE_PARTICIPANTS;
  const visibleParticipants = showAllParticipants
    ? wire.participants
    : wire.participants.slice(0, DEFAULT_VISIBLE_PARTICIPANTS);
  const hiddenCount = wire.participants.length - DEFAULT_VISIBLE_PARTICIPANTS;

  return (
    <div className="flex flex-col h-full">
      {/* Wire header */}
      <div className="px-4 py-3 border-b border-border/30 shrink-0 overflow-hidden">
        <div className="font-mono text-base font-semibold text-foreground mb-2">
          {wire.wire_id}
        </div>
        <div className="flex flex-wrap gap-1.5 items-center">
          {visibleParticipants.map((p) => (
            <ParticipantBubble key={p} agentId={p} />
          ))}
          {hasMany && (
            <button
              onClick={() => setShowAllParticipants(!showAllParticipants)}
              className="text-[10px] text-primary/60 hover:text-primary transition-colors px-2 py-0.5"
            >
              {showAllParticipants ? "show less" : `+${hiddenCount} more`}
            </button>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 min-h-0 relative">
        <ScrollArea className="h-full">
          <div className="space-y-3" style={{ padding: "16px 44px 16px 36px" }}>
            {wire.messages.map((msg, i) => (
              <div key={i} className="rounded-lg bg-secondary/30 border border-border/20 px-5 py-4">
                <div className="flex items-baseline gap-2 mb-2">
                  <span
                    className="font-mono text-sm font-bold"
                    style={{ color: senderColor(msg.sender) }}
                  >
                    {msg.sender}
                  </span>
                  <span className="text-[11px] text-muted-foreground/50 font-mono">
                    {formatTimestamp(msg.timestamp)}
                  </span>
                </div>
                <div className="text-[13px] text-foreground/85 leading-relaxed whitespace-pre-wrap break-words">
                  {msg.body}
                </div>
              </div>
            ))}
          </div>
        </ScrollArea>
        <div className="absolute bottom-0 left-0 right-0 h-16 bg-gradient-to-t from-background to-transparent pointer-events-none" />
      </div>
    </div>
  );
}

function formatTimestamp(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return ts;
  }
}
