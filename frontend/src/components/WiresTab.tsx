import { WireList } from "./WireList";
import { WireChat } from "./WireChat";
import { useStore } from "../store";

export function WiresTab() {
  const wires = useStore((s) => s.wires);
  const wireCount = Object.keys(wires).length;

  if (wireCount === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/50 text-sm">
        No wire conversations yet
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0">
      {/* Sidebar — wire list */}
      <div className="w-64 border-r border-border/30 shrink-0 min-h-0">
        <WireList />
      </div>

      {/* Main — chat view */}
      <div className="flex-1 min-w-0 min-h-0" style={{ marginLeft: 4 }}>
        <WireChat />
      </div>
    </div>
  );
}
