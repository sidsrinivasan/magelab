import { useCallback, useEffect, useRef, useState } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import type { TranscriptEntry } from "../types";

interface TranscriptViewProps {
  entries: TranscriptEntry[];
}

function formatTs(ts: number): string {
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "";
  }
}

const typeBubbleColors: Record<string, string> = {
  assistant_text: "bg-foreground/[0.03] border-foreground/10",
  tool_call: "bg-primary/5 border-primary/15",
  tool_result: "bg-secondary/40 border-border/20",
  run_complete: "bg-state-working/5 border-state-working/15",
  system_prompt: "bg-secondary/30 border-border/15",
  prompt: "bg-secondary/30 border-border/15",
  hook_output: "bg-state-reviewing/5 border-state-reviewing/15",
};

const typeLabels: Record<string, string | null> = {
  assistant_text: "ASSISTANT",
  tool_call: "TOOL",
  tool_result: "RESULT",
  run_complete: "DONE",
  system_prompt: "SYS",
  prompt: "PROMPT",
  hook_output: "HOOK",
};

const typeColors: Record<string, string> = {
  assistant_text: "text-foreground",
  tool_call: "text-primary/80",
  tool_result: "text-muted-foreground",
  run_complete: "text-state-working",
  system_prompt: "text-muted-foreground/50",
  prompt: "text-muted-foreground/60",
  hook_output: "text-state-reviewing",
};

/** Try to parse a string as JSON. Returns the parsed object or null. */
function tryParseJson(s: string): unknown | null {
  try {
    const parsed = JSON.parse(s);
    if (typeof parsed === "object" && parsed !== null) return parsed;
  } catch { /* not JSON */ }
  return null;
}

/** Parse tool call content: "tool_name: {input}" → { name, input } */
function parseToolCall(content: string): { name: string; input: Record<string, unknown> } | null {
  const colonIdx = content.indexOf(": ");
  if (colonIdx === -1) return null;
  const name = content.slice(0, colonIdx).trim();
  const rest = content.slice(colonIdx + 2).trim();
  const parsed = tryParseJson(rest) ?? tryParseJson(rest.replace(/'/g, '"'));
  if (parsed && typeof parsed === "object") return { name, input: parsed as Record<string, unknown> };
  return null;
}

function ToolCallContent({ content }: { content: string }) {
  const parsed = parseToolCall(content);
  if (!parsed) return <span className="text-sm font-mono">{content}</span>;

  return (
    <div className="space-y-1.5">
      <span className="text-primary font-semibold text-sm">{parsed.name}</span>
      <div className="space-y-0.5">
        {Object.entries(parsed.input).map(([key, val]) => (
          <div key={key} className="flex gap-2 text-[13px]">
            <span className="text-muted-foreground/70 shrink-0">{key}:</span>
            <span className="text-foreground/80 break-all">
              {typeof val === "string" ? val : JSON.stringify(val)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ToolResultContent({ content, expanded }: { content: string; expanded: boolean }) {
  const parsed = tryParseJson(content);
  const shouldTruncate = !expanded && content.length > 300;

  if (parsed) {
    const prettyPrinted = JSON.stringify(parsed, null, 2);
    const display = shouldTruncate ? prettyPrinted.slice(0, 300) + "..." : prettyPrinted;
    return (
      <pre className="text-[13px] font-mono leading-relaxed whitespace-pre-wrap break-words text-muted-foreground">
        {display}
      </pre>
    );
  }

  const display = shouldTruncate ? content.slice(0, 300) + "..." : content;
  return (
    <pre className="text-[13px] font-mono leading-relaxed whitespace-pre-wrap break-words text-muted-foreground">
      {display}
    </pre>
  );
}

function EntryRow({ entry }: { entry: TranscriptEntry }) {
  const [expanded, setExpanded] = useState(false);

  const content = entry.content.trim();
  const isToolCall = entry.entry_type === "tool_call";
  const isToolResult = entry.entry_type === "tool_result";
  const isCollapsible = isToolCall
    ? content.length > 400
    : isToolResult
      ? content.length > 300
      : false;

  const label = entry.entry_type in typeLabels
    ? typeLabels[entry.entry_type]
    : entry.entry_type;
  const colorClass = typeColors[entry.entry_type] || "text-foreground";
  const bubbleClass = typeBubbleColors[entry.entry_type] || "bg-secondary/30 border-border/15";
  const timeStr = entry.timestamp ? formatTs(entry.timestamp) : "";

  return (
    <div className="py-1.5">
      <div className={`rounded-lg border p-3.5 ${bubbleClass}`}>
        {/* Header: label + timestamp */}
        <div className="flex items-center gap-2 mb-1.5">
          {label && (
            <span className="text-[10px] font-mono font-bold uppercase tracking-wider opacity-60">
              {label}
            </span>
          )}
          {timeStr && (
            <span className="text-[11px] text-primary/50 font-mono ml-auto">
              {timeStr}
            </span>
          )}
        </div>

        {/* Content */}
        {isToolCall ? (
          <ToolCallContent content={content} />
        ) : isToolResult ? (
          <ToolResultContent content={content} expanded={expanded} />
        ) : (
          <pre
            className={`text-sm font-mono leading-relaxed whitespace-pre-wrap break-words ${colorClass}`}
          >
            {isCollapsible && !expanded
              ? content.slice(0, 200) + "..."
              : content}
          </pre>
        )}

        {isCollapsible && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="mt-1.5 text-[11px] text-primary/60 hover:text-primary transition-colors"
          >
            {expanded ? "collapse" : "expand"}
          </button>
        )}
      </div>
    </div>
  );
}

export function TranscriptView({ entries }: TranscriptViewProps) {
  if (entries.length === 0) {
    return (
      <div className="flex items-center justify-center h-32 text-xs text-muted-foreground/50">
        No transcript entries yet
      </div>
    );
  }

  // Track run_complete positions to insert session boundary dividers.
  // A run_complete followed by more entries means the agent was resumed.
  let sawRunComplete = false;

  const bottomRef = useRef<HTMLDivElement>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const [showScrollBtn, setShowScrollBtn] = useState(false);

  const handleScroll = useCallback(() => {
    const el = viewportRef.current;
    if (!el) return;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollBtn(distFromBottom > 200);
  }, []);

  // Attach scroll listener to the ScrollArea viewport
  const scrollAreaRef = useCallback((node: HTMLDivElement | null) => {
    if (node) {
      const viewport = node.querySelector("[data-radix-scroll-area-viewport]") as HTMLDivElement | null;
      viewportRef.current = viewport;
      viewport?.addEventListener("scroll", handleScroll);
    }
  }, [handleScroll]);

  useEffect(() => {
    return () => {
      viewportRef.current?.removeEventListener("scroll", handleScroll);
    };
  }, [handleScroll]);

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <div className="h-full relative">
      <ScrollArea className="h-full" ref={scrollAreaRef}>
        <div style={{ padding: "16px 56px 16px 48px" }}>
          {entries.map((entry, i) => {
            let divider = null;
            if (sawRunComplete && entry.entry_type !== "run_complete") {
              divider = (
                <div className="flex items-center gap-3 py-4 my-2">
                  <div className="flex-1 border-t border-border/40" />
                  <span className="text-[10px] font-mono uppercase tracking-wider text-muted-foreground/50">
                    session resumed
                  </span>
                  <div className="flex-1 border-t border-border/40" />
                </div>
              );
              sawRunComplete = false;
            }
            if (entry.entry_type === "run_complete") {
              sawRunComplete = true;
            }
            return (
              <div key={i}>
                {divider}
                <EntryRow entry={entry} />
              </div>
            );
          })}
          <div ref={bottomRef} />
        </div>
      </ScrollArea>

      {showScrollBtn && (
        <button
          onClick={scrollToBottom}
          className="absolute bottom-4 right-4 w-8 h-8 rounded-full bg-secondary/80 border border-border/40 flex items-center justify-center hover:bg-secondary transition-colors shadow-lg z-10"
          title="Scroll to bottom"
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" className="text-foreground/70">
            <path d="M7 2v10M3 8l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      )}
    </div>
  );
}
