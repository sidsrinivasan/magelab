import { useMemo, useState } from "react";
import { useStore } from "../store";
import Markdown from "react-markdown";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import type { Task, TaskStatus, ReviewRecord } from "../types";

// ── Status column config ──────────────────────────────────────────────

interface ColumnDef {
  statuses: TaskStatus[];
  label: string;
  color: string;
}

const COLUMNS: ColumnDef[] = [
  { statuses: ["created"], label: "Created", color: "#64748b" },
  { statuses: ["assigned"], label: "Assigned", color: "#818cf8" },
  { statuses: ["in_progress"], label: "In Progress", color: "#60a5fa" },
  { statuses: ["under_review"], label: "Under Review", color: "#fbbf24" },
  { statuses: ["review_failed"], label: "Review Failed", color: "#f87171" },
  { statuses: ["changes_requested"], label: "Changes Requested", color: "#fb923c" },
  { statuses: ["approved"], label: "Approved", color: "#34d399" },
  { statuses: ["succeeded"], label: "Succeeded", color: "#22c55e" },
  { statuses: ["failed"], label: "Failed", color: "#ef4444" },
];

// ── Helpers ───────────────────────────────────────────────────────────

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ago`;
}

function reviewDecisionColor(decision: string): string {
  switch (decision) {
    case "approved":
      return "#34d399";
    case "changes_requested":
      return "#fbbf24";
    case "failed":
      return "#f87171";
    default:
      return "#64748b";
  }
}

function reviewDecisionIcon(decision: string): string {
  switch (decision) {
    case "approved":
      return "✓";
    case "changes_requested":
      return "⟳";
    case "failed":
      return "✗";
    default:
      return "?";
  }
}

// ── Review history dialog ─────────────────────────────────────────────

function ReviewHistoryDialog({ task }: { task: Task }) {
  const allRecords: ReviewRecord[] = [
    ...(task.review_history || []),
    ...Object.values(task.active_reviews || {}),
  ];

  if (allRecords.length === 0) {
    return null;
  }

  // Group by round
  const rounds = new Map<number, ReviewRecord[]>();
  for (const r of allRecords) {
    const list = rounds.get(r.round_number) || [];
    list.push(r);
    rounds.set(r.round_number, list);
  }
  const sortedRounds = Array.from(rounds.entries()).sort((a, b) => b[0] - a[0]);

  return (
    <Dialog>
      <DialogTrigger asChild>
        <button className="text-[9px] text-muted-foreground/60 hover:text-foreground/80 transition-colors underline decoration-dotted underline-offset-2">
          {task.current_review_round} review{task.current_review_round !== 1 ? "s" : ""}
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-lg max-h-[70vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="text-sm font-medium">
            Review History — {task.title}
          </DialogTitle>
        </DialogHeader>
        <ScrollArea className="flex-1 min-h-0 pr-2">
          <div className="space-y-4 pb-2">
            {sortedRounds.map(([round, records]) => (
              <div key={round}>
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60">
                    Round {round}
                  </span>
                  <div className="flex-1 h-px bg-border/30" />
                </div>
                <div className="space-y-2">
                  {records.map((record) => (
                    <div
                      key={`${record.reviewer_id}-${record.round_number}`}
                      className="rounded-md border border-border/30 bg-secondary/20 p-3"
                    >
                      {/* Reviewer + decision header */}
                      <div className="flex items-center gap-2 mb-1">
                        <span className="font-mono text-xs text-foreground/90">
                          {record.reviewer_id}
                        </span>
                        {record.review ? (
                          <span
                            className="text-[10px] font-medium flex items-center gap-1"
                            style={{ color: reviewDecisionColor(record.review.decision) }}
                          >
                            <span>{reviewDecisionIcon(record.review.decision)}</span>
                            {statusLabel(record.review.decision)}
                          </span>
                        ) : (
                          <span className="text-[10px] text-muted-foreground/50 italic">
                            pending...
                          </span>
                        )}
                        <span className="ml-auto text-[9px] text-muted-foreground/40">
                          {relativeTime(record.created_at)}
                        </span>
                      </div>
                      {/* Request message */}
                      {record.request_message && (
                        <div className="text-[11px] text-muted-foreground/60 mb-1.5 italic">
                          Request: {record.request_message}
                        </div>
                      )}
                      {/* Review comment */}
                      {record.review?.comment && (
                        <div className="text-[11px] text-foreground/70 whitespace-pre-wrap border-l-2 pl-2 mt-1.5"
                          style={{ borderColor: reviewDecisionColor(record.review.decision) }}
                        >
                          {record.review.comment}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {sortedRounds.length === 0 && (
              <div className="text-sm text-muted-foreground/50 text-center py-4">
                No reviews yet
              </div>
            )}
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}

// ── Collapsible section ──────────────────────────────────────────────

function CollapsibleSection({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-border/30 pt-3">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 text-sm font-medium text-foreground/80 hover:text-foreground transition-colors w-full text-left"
      >
        <span className={`text-[10px] transition-transform ${open ? "rotate-90" : ""}`}>&#9654;</span>
        {title}{count !== undefined ? ` (${count})` : ""}
      </button>
      {open && <div className="mt-2 ml-4">{children}</div>}
    </div>
  );
}

// ── Task detail dialog ───────────────────────────────────────────────

/** Get the column color for a task status (reuses COLUMNS definitions). */
function statusBadgeColor(status: string): string {
  const col = COLUMNS.find((c) => c.statuses.includes(status as TaskStatus));
  return col?.color || "#64748b";
}

function TaskDetailDialog({ task, open, onClose }: { task: Task; open: boolean; onClose: () => void }) {
  const badgeColor = statusBadgeColor(task.status);

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto p-0">
        <div className="p-6 pr-10">
          {/* Header */}
          <div className="space-y-3 mb-4">
            <div className="flex items-center gap-3 pr-4">
              <h2 className="text-lg font-semibold flex-1">{task.title}</h2>
              <span
                className="text-[11px] font-medium px-2 py-0.5 rounded"
                style={{ backgroundColor: `${badgeColor}20`, color: badgeColor }}
              >
                {statusLabel(task.status)}
              </span>
            </div>
            <div className="flex items-center gap-3 text-sm text-muted-foreground">
              {task.assigned_to && (
                <span>Assigned to: <span className="text-foreground font-mono text-xs">{task.assigned_to}</span></span>
              )}
              {task.assigned_by && task.assigned_by !== task.assigned_to && (
                <span>by <span className="text-foreground font-mono text-xs">{task.assigned_by}</span></span>
              )}
            </div>
          </div>

          {/* Description */}
          <div className="mb-4">
            <h3 className="text-xs font-medium uppercase tracking-wider text-muted-foreground/60 mb-1">Description</h3>
            <div className="prose prose-invert prose-sm max-w-none
              [&_h1]:text-foreground [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:border-b [&_h1]:border-border/30 [&_h1]:pb-2 [&_h1]:mb-4
              [&_h2]:text-foreground [&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-6 [&_h2]:mb-3
              [&_h3]:text-foreground [&_h3]:text-sm [&_h3]:font-semibold [&_h3]:mt-4 [&_h3]:mb-2
              [&_p]:text-foreground/80 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:mb-3
              [&_ul]:text-foreground/80 [&_ul]:text-sm [&_ul]:mb-3 [&_ul]:pl-5 [&_ul]:list-disc
              [&_ol]:text-foreground/80 [&_ol]:text-sm [&_ol]:mb-3 [&_ol]:pl-5 [&_ol]:list-decimal
              [&_li]:mb-1
              [&_code]:text-[12px] [&_code]:bg-secondary/60 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:rounded
              [&_pre]:bg-secondary/40 [&_pre]:rounded-md [&_pre]:p-3 [&_pre]:mb-3 [&_pre]:whitespace-pre-wrap [&_pre]:break-words
              [&_pre_code]:bg-transparent [&_pre_code]:p-0
              [&_strong]:text-foreground [&_strong]:font-semibold
            ">
              <Markdown>{task.description}</Markdown>
            </div>
          </div>

          {/* Metadata */}
          <div className="grid grid-cols-2 gap-2 text-[13px] mb-4">
            <div><span className="text-muted-foreground">Created:</span> {task.created_at ? new Date(task.created_at).toLocaleString() : "—"}</div>
            <div><span className="text-muted-foreground">Updated:</span> {task.updated_at ? new Date(task.updated_at).toLocaleString() : "—"}</div>
            {task.finished_at && (
              <div><span className="text-muted-foreground">Finished:</span> {new Date(task.finished_at).toLocaleString()}</div>
            )}
            <div><span className="text-muted-foreground">Review required:</span> {task.review_required ? "Yes" : "No"}</div>
          </div>

          {/* Collapsible: Assignment History */}
          {task.assignment_history.length > 1 && (
            <CollapsibleSection title="Assignment History" count={task.assignment_history.length}>
              <div className="space-y-1">
                {task.assignment_history.map((agent, i) => (
                  <div key={i} className="flex items-center gap-2 text-sm">
                    <span className="text-muted-foreground text-xs w-5">{i + 1}.</span>
                    <span className="text-foreground font-mono text-xs">{agent}</span>
                    {i === 0 && <span className="text-[10px] text-muted-foreground/60">(creator)</span>}
                    {i === task.assignment_history.length - 1 && i > 0 && <span className="text-[10px] text-muted-foreground/60">(current)</span>}
                  </div>
                ))}
              </div>
            </CollapsibleSection>
          )}

          {/* Collapsible: Active Reviews */}
          {task.active_reviews && Object.keys(task.active_reviews).length > 0 && (
            <CollapsibleSection title="Active Reviews" count={Object.keys(task.active_reviews).length}>
              <div className="space-y-2">
                {Object.entries(task.active_reviews).map(([reviewerId, record]) => (
                  <div key={reviewerId} className="text-sm border border-border/20 rounded p-2">
                    <div className="font-mono text-xs font-medium">{record.reviewer_id}</div>
                    {record.request_message && (
                      <div className="text-muted-foreground text-xs mt-1 italic">{record.request_message}</div>
                    )}
                    {record.review ? (
                      <div className="mt-1 text-xs flex items-center gap-1">
                        <span style={{ color: reviewDecisionColor(record.review.decision) }}>
                          {reviewDecisionIcon(record.review.decision)} {statusLabel(record.review.decision)}
                        </span>
                        {record.review.comment && <span className="text-muted-foreground ml-2">{record.review.comment}</span>}
                      </div>
                    ) : (
                      <div className="text-xs text-muted-foreground/60 mt-1 italic">Pending</div>
                    )}
                  </div>
                ))}
              </div>
            </CollapsibleSection>
          )}

          {/* Collapsible: Review History */}
          {task.review_history.length > 0 && (
            <CollapsibleSection title="Review History" count={task.review_history.length}>
              <div className="space-y-2">
                {task.review_history.map((record, i) => (
                  <div key={i} className="text-sm border border-border/20 rounded p-2">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs font-medium">{record.reviewer_id}</span>
                      <span className="text-[10px] text-muted-foreground">Round {record.round_number}</span>
                      {record.review && (
                        <span className="text-xs" style={{ color: reviewDecisionColor(record.review.decision) }}>
                          {reviewDecisionIcon(record.review.decision)} {statusLabel(record.review.decision)}
                        </span>
                      )}
                    </div>
                    {record.request_message && (
                      <div className="text-xs text-muted-foreground mt-1">Request: {record.request_message}</div>
                    )}
                    {record.review?.comment && (
                      <div className="text-xs text-muted-foreground/80 mt-1 border-l-2 pl-2"
                        style={{ borderColor: reviewDecisionColor(record.review.decision) }}
                      >
                        {record.review.comment}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </CollapsibleSection>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ── Task card ─────────────────────────────────────────────────────────

function TaskCard({ task }: { task: Task }) {
  const [detailOpen, setDetailOpen] = useState(false);

  return (
    <>
      <div
        className="rounded-md border border-border/30 bg-card/60 p-3 space-y-2 hover:border-border/50 transition-colors min-w-0 max-w-full cursor-pointer"
        onClick={() => setDetailOpen(true)}
      >
        {/* Title */}
        <div className="text-xs font-medium text-foreground/90 leading-snug break-words">
          {task.title}
        </div>

        {/* Description (truncated) */}
        <p className="text-[11px] text-muted-foreground/60 leading-relaxed line-clamp-2 break-words">
          {task.description}
        </p>

        {/* Metadata row */}
        <div className="flex items-center gap-2 min-w-0">
          {/* Assigned to */}
          {task.assigned_to && (
            <div className="flex items-center gap-1 min-w-0 overflow-hidden">
              <span className="text-[9px] text-muted-foreground/40 shrink-0">→</span>
              <span className="font-mono text-[10px] text-foreground/70 truncate">
                {task.assigned_to}
              </span>
            </div>
          )}

          {/* Review info */}
          {task.review_required && (
            <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
              <span className="text-[9px] text-muted-foreground/40">review</span>
              <ReviewHistoryDialog task={task} />
            </div>
          )}

          {/* Active reviewers indicator */}
          {task.active_reviews && Object.keys(task.active_reviews).length > 0 && (
            <div className="flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
              <span className="text-[9px] text-muted-foreground/50">
                {Object.values(task.active_reviews).filter((r) => r.review === null).length} pending
              </span>
            </div>
          )}

          {/* Timestamp */}
          <span className="text-[9px] text-muted-foreground/30 ml-auto">
            {relativeTime(task.updated_at)}
          </span>
        </div>
      </div>
      <TaskDetailDialog task={task} open={detailOpen} onClose={() => setDetailOpen(false)} />
    </>
  );
}

// ── Kanban column ─────────────────────────────────────────────────────

function KanbanColumn({ column, tasks }: { column: ColumnDef; tasks: Task[] }) {
  return (
    <div className="flex flex-col min-w-[256px] w-[256px] shrink-0">
      {/* Column header */}
      <div className="shrink-0 px-3 py-2 flex items-center gap-2">
        <span
          className="w-2 h-2 rounded-full shrink-0"
          style={{ backgroundColor: column.color }}
        />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/60 truncate">
          {column.label}
        </span>
        <span className="text-[9px] text-muted-foreground/30 ml-auto shrink-0">
          {tasks.length}
        </span>
      </div>

      {/* Cards */}
      <div
        className="flex-1 min-h-0 overflow-y-auto"
        style={{
          scrollbarWidth: "thin",
          scrollbarColor: "hsl(var(--border)) transparent",
        }}
      >
        <div className="space-y-2 pb-4 px-3 min-w-0">
          {tasks.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
          {tasks.length === 0 && (
            <div className="text-[10px] text-muted-foreground/30 text-center py-6">
              No tasks
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────

export function TasksTab() {
  const tasks = useStore((s) => s.tasks);

  // Group tasks by column
  const columnData = useMemo(() => {
    const taskList = Object.values(tasks);
    return COLUMNS.map((col) => ({
      column: col,
      tasks: taskList
        .filter((t) => col.statuses.includes(t.status))
        .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()),
    }));
  }, [tasks]);

  if (Object.keys(tasks).length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/50 text-sm">
        No tasks yet
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* Horizontal scrollable kanban */}
      <div
        className="flex-1 min-h-0 overflow-x-auto overflow-y-hidden flex gap-1 p-4"
        style={{
          scrollbarWidth: "thin",
          scrollbarColor: "hsl(var(--border)) transparent",
        }}
      >
        {columnData.map(({ column, tasks }) => (
          <KanbanColumn key={column.label} column={column} tasks={tasks} />
        ))}
      </div>
    </div>
  );
}
