import { useCallback, useEffect, useMemo, useState } from "react";
import { Highlight, themes, type Language } from "prism-react-renderer";
import Markdown from "react-markdown";
import { ScrollArea } from "@/components/ui/scroll-area";

// ── Types ──────────────────────────────────────────────────────────────

interface FileEntry {
  name: string;
  type: "file";
  size: number;
}

interface DirEntry {
  name: string;
  type: "dir";
  children: TreeEntry[];
}

type TreeEntry = FileEntry | DirEntry;

// ── File tree sidebar ──────────────────────────────────────────────────

function FileTreeNode({
  entry,
  path,
  depth,
  selectedPath,
  onSelect,
}: {
  entry: TreeEntry;
  path: string;
  depth: number;
  selectedPath: string | null;
  onSelect: (path: string) => void;
}) {
  const [expanded, setExpanded] = useState(depth < 2);
  const fullPath = path ? `${path}/${entry.name}` : entry.name;
  const isSelected = selectedPath === fullPath;

  if (entry.type === "dir") {
    return (
      <div>
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full text-left flex items-center gap-1.5 py-1 px-2 rounded hover:bg-secondary/40 text-xs text-muted-foreground"
          style={{ paddingLeft: `${depth * 14 + 8}px` }}
        >
          <span className="text-[10px] w-3 text-center shrink-0 opacity-60">
            {expanded ? "▾" : "▸"}
          </span>
          <span className="text-[10px] shrink-0">📁</span>
          <span className="truncate">{entry.name}</span>
        </button>
        {expanded &&
          entry.children.map((child) => (
            <FileTreeNode
              key={child.name}
              entry={child}
              path={fullPath}
              depth={depth + 1}
              selectedPath={selectedPath}
              onSelect={onSelect}
            />
          ))}
      </div>
    );
  }

  return (
    <button
      onClick={() => onSelect(fullPath)}
      className={`w-full text-left flex items-center gap-1.5 py-1 px-2 rounded text-xs transition-colors ${
        isSelected
          ? "bg-primary/10 text-foreground"
          : "text-muted-foreground hover:bg-secondary/40"
      }`}
      style={{ paddingLeft: `${depth * 14 + 8}px` }}
    >
      <span className="text-[10px] w-3 text-center shrink-0" />
      <span className="text-[10px] shrink-0">{fileIcon(entry.name)}</span>
      <span className="truncate">{entry.name}</span>
      <span className="ml-auto text-[9px] text-muted-foreground/40 shrink-0">
        {formatSize(entry.size)}
      </span>
    </button>
  );
}

function fileIcon(name: string): string {
  if (name.endsWith(".py")) return "🐍";
  if (name.endsWith(".json") || name.endsWith(".yaml") || name.endsWith(".yml")) return "📋";
  if (name.endsWith(".csv")) return "📊";
  if (name.endsWith(".md") || name.endsWith(".txt")) return "📄";
  return "📄";
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`;
}

// ── Language detection ─────────────────────────────────────────────────

function detectLanguage(filename: string): Language {
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, Language> = {
    py: "python",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    md: "markdown",
    mdx: "markdown",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "tsx",
    css: "css",
    html: "markup",
    xml: "markup",
    svg: "markup",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    sql: "sql",
    txt: "plain" as Language,
    csv: "plain" as Language,
    log: "plain" as Language,
  };
  return map[ext] ?? ("plain" as Language);
}

// ── File viewer ────────────────────────────────────────────────────────

function FileViewer({
  path,
  content,
  loading,
  error,
}: {
  path: string;
  content: string | null;
  loading: boolean;
  error: string | null;
}) {
  const language = useMemo(() => detectLanguage(path), [path]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/50 text-sm">
        Loading...
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-full text-state-terminated/70 text-sm">
        {error}
      </div>
    );
  }

  if (content === null) return null;

  const isMarkdown = /\.mdx?$/.test(path);
  const lineCount = content.split("\n").length;
  const gutterWidth = String(lineCount).length;

  return (
    <div className="h-full flex flex-col min-h-0">
      {/* File path header */}
      <div className="shrink-0 px-4 py-2 border-b border-border/30 flex items-center gap-2">
        <span className="font-mono text-xs text-muted-foreground">{path}</span>
        <span className="text-[9px] text-muted-foreground/40 ml-auto">
          {isMarkdown ? "rendered" : `${lineCount} lines`}
        </span>
      </div>

      {isMarkdown ? (
        /* Rendered markdown */
        <ScrollArea className="flex-1 min-h-0">
          <div className="prose prose-invert prose-sm max-w-none p-6
            [&_h1]:text-foreground [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:border-b [&_h1]:border-border/30 [&_h1]:pb-2 [&_h1]:mb-4
            [&_h2]:text-foreground [&_h2]:text-base [&_h2]:font-semibold [&_h2]:mt-6 [&_h2]:mb-3
            [&_h3]:text-foreground [&_h3]:text-sm [&_h3]:font-semibold [&_h3]:mt-4 [&_h3]:mb-2
            [&_p]:text-foreground/80 [&_p]:text-sm [&_p]:leading-relaxed [&_p]:mb-3
            [&_ul]:text-foreground/80 [&_ul]:text-sm [&_ul]:mb-3 [&_ul]:pl-5 [&_ul]:list-disc
            [&_ol]:text-foreground/80 [&_ol]:text-sm [&_ol]:mb-3 [&_ol]:pl-5 [&_ol]:list-decimal
            [&_li]:mb-1
            [&_code]:text-[12px] [&_code]:bg-secondary/60 [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:rounded
            [&_pre]:bg-secondary/40 [&_pre]:rounded-md [&_pre]:p-3 [&_pre]:mb-3 [&_pre]:overflow-x-auto
            [&_pre_code]:bg-transparent [&_pre_code]:p-0
            [&_blockquote]:border-l-2 [&_blockquote]:border-muted-foreground/30 [&_blockquote]:pl-4 [&_blockquote]:text-muted-foreground/70 [&_blockquote]:italic
            [&_a]:text-primary [&_a]:underline
            [&_table]:text-sm [&_table]:w-full [&_table]:mb-3
            [&_th]:text-left [&_th]:font-semibold [&_th]:border-b [&_th]:border-border/30 [&_th]:pb-1.5 [&_th]:pr-4
            [&_td]:border-b [&_td]:border-border/20 [&_td]:py-1.5 [&_td]:pr-4
            [&_hr]:border-border/30 [&_hr]:my-4
            [&_strong]:text-foreground [&_strong]:font-semibold
          ">
            <Markdown>{content}</Markdown>
          </div>
        </ScrollArea>
      ) : (
        /* Syntax-highlighted code */
        <ScrollArea className="flex-1 min-h-0">
          <Highlight theme={themes.vsDark} code={content} language={language}>
            {({ tokens, getLineProps, getTokenProps }) => (
              <pre className="text-[12px] leading-[1.6] font-mono p-4" style={{ background: "transparent", wordBreak: "break-word" }}>
                {tokens.map((line, i) => {
                  const lineProps = getLineProps({ line, key: i });
                  return (
                    <div
                      {...lineProps}
                      key={i}
                      className="flex hover:bg-secondary/20"
                      style={undefined}
                    >
                      <span
                        className="shrink-0 text-right text-muted-foreground/30 select-none pr-4"
                        style={{ width: `${gutterWidth + 2}ch` }}
                      >
                        {i + 1}
                      </span>
                      <span className="whitespace-pre-wrap break-words min-w-0">
                        {line.map((token, j) => (
                          <span {...getTokenProps({ token, key: j })} key={j} />
                        ))}
                      </span>
                    </div>
                  );
                })}
              </pre>
            )}
          </Highlight>
        </ScrollArea>
      )}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────

export function WorkspaceTab() {
  const [tree, setTree] = useState<TreeEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [treeLoading, setTreeLoading] = useState(true);

  // Fetch file tree
  const fetchTree = useCallback(async () => {
    try {
      const res = await fetch("/api/workspace/tree");
      if (!res.ok) return;
      const data = await res.json();
      setTree(data.tree || []);
    } catch {
      // Server might not support workspace API
    } finally {
      setTreeLoading(false);
    }
  }, []);

  // Poll tree every 5 seconds for live updates as agents create files
  useEffect(() => {
    fetchTree();
    const interval = setInterval(fetchTree, 5000);
    return () => clearInterval(interval);
  }, [fetchTree]);

  // Fetch file content when selection changes
  useEffect(() => {
    if (!selectedPath) {
      setFileContent(null);
      setFileError(null);
      return;
    }

    let cancelled = false;
    setFileLoading(true);
    setFileError(null);

    fetch(`/api/workspace/file?path=${encodeURIComponent(selectedPath)}`)
      .then((res) => res.json())
      .then((data) => {
        if (cancelled) return;
        if (data.error) {
          setFileError(data.error);
          setFileContent(null);
        } else {
          setFileContent(data.content);
          setFileError(null);
        }
      })
      .catch(() => {
        if (!cancelled) setFileError("Failed to load file");
      })
      .finally(() => {
        if (!cancelled) setFileLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedPath]);

  if (treeLoading) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/50 text-sm">
        Loading workspace...
      </div>
    );
  }

  if (tree.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground/50 text-sm">
        Workspace is empty
      </div>
    );
  }

  return (
    <div className="flex h-full">
      {/* File tree sidebar */}
      <div className="w-64 border-r border-border/30 flex flex-col shrink-0 min-h-0">
        <div className="shrink-0 px-3 py-2 border-b border-border/30">
          <span className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground/60">
            workspace
          </span>
        </div>
        <div
          className="flex-1 min-h-0 overflow-y-auto py-1"
          style={{
            scrollbarWidth: "thin",
            scrollbarColor: "hsl(var(--border)) transparent",
          }}
        >
          {tree.map((entry) => (
            <FileTreeNode
              key={entry.name}
              entry={entry}
              path=""
              depth={0}
              selectedPath={selectedPath}
              onSelect={setSelectedPath}
            />
          ))}
        </div>
      </div>

      {/* File viewer */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0 overflow-hidden">
        {selectedPath ? (
          <FileViewer
            path={selectedPath}
            content={fileContent}
            loading={fileLoading}
            error={fileError}
          />
        ) : (
          <div className="flex items-center justify-center h-full text-muted-foreground/50 text-sm">
            Select a file to view
          </div>
        )}
      </div>
    </div>
  );
}
