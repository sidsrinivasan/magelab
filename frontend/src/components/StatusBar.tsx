import { useEffect, useRef } from "react";
import { useStore } from "../store";
import { formatCost, formatElapsed } from "../lib/theme";
import type { RunStatus } from "../types";

export function StatusBar() {
  const runStatus = useStore((s) => s.runStatus);
  const totalCostUsd = useStore((s) => s.totalCostUsd);
  const finalDurationSeconds = useStore((s) => s.finalDurationSeconds);
  const tasks = useStore((s) => s.tasks);
  const orgName = useStore((s) => s.orgName);
  const elapsedSeconds = useStore((s) => s.elapsedSeconds);
  const tickElapsed = useStore((s) => s.tickElapsed);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Tick elapsed time while running
  useEffect(() => {
    if (runStatus === "running") {
      timerRef.current = setInterval(tickElapsed, 1000);
    } else if (timerRef.current) {
      clearInterval(timerRef.current);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [runStatus, tickElapsed]);

  const taskList = Object.values(tasks);
  const succeeded = taskList.filter((t) => t.status === "succeeded").length;
  const failed = taskList.filter((t) => t.status === "failed").length;
  const total = taskList.length;

  const statusConfig: Record<RunStatus, { label: string; color: string; pulse: boolean }> = {
    connecting: { label: "Connecting", color: "#94a3b8", pulse: true },
    running: { label: "Running", color: "#34d399", pulse: true },
    success: { label: "Success", color: "#60a5fa", pulse: false },
    partial: { label: "Partial", color: "#fbbf24", pulse: false },
    failure: { label: "Failed", color: "#f87171", pulse: false },
    timeout: { label: "Timed Out", color: "#fbbf24", pulse: false },
    no_work: { label: "No Work", color: "#94a3b8", pulse: false },
  };

  const { label, color, pulse } = statusConfig[runStatus];

  // Use final duration from server once available, otherwise client-side elapsed
  const elapsed = finalDurationSeconds ?? elapsedSeconds;

  return (
    <div className="flex items-center justify-between px-5 py-3 border-b border-border bg-card/50 backdrop-blur-sm">
      <div className="flex items-center gap-4">
        <h1 className="text-base font-semibold tracking-tight text-foreground">
          {orgName || "magelab"}
        </h1>
        <div className="flex items-center gap-2">
          <span
            className={`inline-block w-2 h-2 rounded-full ${pulse ? "animate-pulse" : ""}`}
            style={{ backgroundColor: color }}
          />
          <span className="text-xs font-medium" style={{ color }}>
            {label}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-6 text-xs text-muted-foreground font-mono">
        <div className="flex items-center gap-1.5">
          <span className="opacity-60">TIME</span>
          <span className="text-foreground">{formatElapsed(elapsed)}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="opacity-60">COST</span>
          <span className="text-foreground">{formatCost(totalCostUsd)}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="opacity-60">TASKS</span>
          <span className="text-foreground">
            {total > 0 ? (
              <>
                <span className="text-state-working">{succeeded}</span>
                {failed > 0 && (
                  <>
                    <span className="text-muted-foreground">/</span>
                    <span className="text-state-terminated">{failed}</span>
                  </>
                )}
                <span className="text-muted-foreground">/{total}</span>
              </>
            ) : (
              "--"
            )}
          </span>
        </div>
      </div>
    </div>
  );
}
