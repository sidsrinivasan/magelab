import { useMemo, useState } from "react";
import { useStore } from "../store";
import { senderColor } from "../lib/theme";

const DEFAULT_VISIBLE_PARTICIPANTS = 3;

function ParticipantChips({ participants }: { participants: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const hasMany = participants.length > DEFAULT_VISIBLE_PARTICIPANTS;
  const visible = expanded ? participants : participants.slice(0, DEFAULT_VISIBLE_PARTICIPANTS);
  const hiddenCount = participants.length - DEFAULT_VISIBLE_PARTICIPANTS;

  return (
    <div className="flex flex-wrap gap-1 mt-1.5 mb-1 items-center">
      {visible.map((p) => (
        <span
          key={p}
          className="text-[11px] font-mono font-medium px-2 py-0.5 rounded-full bg-secondary/60 text-foreground/80"
          style={{ borderLeft: `2px solid ${senderColor(p)}` }}
        >
          {p}
        </span>
      ))}
      {hasMany && (
        <span
          role="button"
          onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
          className="text-[10px] text-primary/60 hover:text-primary transition-colors px-1.5 py-0.5 cursor-pointer"
        >
          {expanded ? "show less" : `+${hiddenCount} more`}
        </span>
      )}
    </div>
  );
}

export function WireList() {
  const wires = useStore((s) => s.wires);
  const selectedWireId = useStore((s) => s.selectedWireId);
  const setSelectedWire = useStore((s) => s.setSelectedWire);

  const wireList = useMemo(() =>
    Object.values(wires).sort((a, b) => {
      const aLast = a.messages[a.messages.length - 1]?.timestamp || "";
      const bLast = b.messages[b.messages.length - 1]?.timestamp || "";
      return bLast.localeCompare(aLast);
    }),
  [wires]);

  if (wireList.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/50 text-xs">
        No wires active
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto" style={{ scrollbarWidth: "thin", scrollbarColor: "hsl(var(--border)) transparent" }}>
      <div className="space-y-1" style={{ padding: "8px 12px 8px 10px" }}>
        {wireList.map((wire) => {
          const lastMsg = wire.messages[wire.messages.length - 1];
          const isSelected = wire.wire_id === selectedWireId;

          return (
            <button
              key={wire.wire_id}
              onClick={() => setSelectedWire(wire.wire_id)}
              className={`w-full text-left p-3 rounded-md transition-colors ${
                isSelected
                  ? "bg-primary/10 border border-primary/20"
                  : "hover:bg-secondary/50 border border-transparent"
              }`}
            >
              <div className="flex items-center justify-between mb-0.5">
                <span className="font-mono text-sm text-foreground font-semibold">
                  {wire.wire_id}
                </span>
                <span className="text-[10px] text-muted-foreground/50">
                  {wire.messages.length} msgs
                </span>
              </div>
              <ParticipantChips participants={wire.participants} />
              {lastMsg && (
                <div className="text-[11px] text-muted-foreground truncate mt-1">
                  <span style={{ color: senderColor(lastMsg.sender) }} className="font-medium">
                    {lastMsg.sender}:
                  </span>{" "}
                  {lastMsg.body.slice(0, 80)}
                </div>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
