import { useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { useStore } from "../store";
import { roleColor, roleBgColor } from "../lib/theme";
import type { RoleInfo } from "../types";

interface RolePopoverProps {
  roleName: string;
}

export function RolePopover({ roleName }: RolePopoverProps) {
  const roles = useStore((s) => s.roles);
  const [open, setOpen] = useState(false);
  const badgeRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ top: 0, left: 0, maxH: 400 });

  const role: RoleInfo | undefined = roles[roleName];

  const handleOpen = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!open && badgeRef.current) {
      const rect = badgeRef.current.getBoundingClientRect();
      const left = Math.min(rect.left, window.innerWidth - 340);
      const spaceBelow = window.innerHeight - rect.bottom - 16;
      const spaceAbove = rect.top - 16;

      if (spaceBelow >= 300 || spaceBelow >= spaceAbove) {
        // Show below
        setPos({ top: rect.bottom + 8, left, maxH: Math.min(spaceBelow, 500) });
      } else {
        // Show above (flip)
        const maxH = Math.min(spaceAbove, 500);
        setPos({ top: rect.top - 8 - maxH, left, maxH });
      }
    }
    setOpen(!open);
  };

  return (
    <div className="relative inline-block" ref={badgeRef}>
      <Badge
        variant="secondary"
        className="text-[10px] font-medium border-0 cursor-pointer hover:ring-1 hover:ring-primary/30 transition-all"
        style={{
          color: roleColor(roleName),
          backgroundColor: roleBgColor(roleName),
        }}
        onClick={handleOpen}
      >
        {roleName}
      </Badge>

      {open && role && (
        <>
          {/* Backdrop */}
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />

          {/* Popover — fixed positioning to avoid clipping */}
          <div
            className="fixed z-50 w-80 rounded-lg border border-border/50 bg-card shadow-xl animate-fade-up flex flex-col"
            style={{ top: pos.top, left: pos.left, maxHeight: pos.maxH }}
          >
            <div className="p-4 space-y-3 shrink-0">
              <div className="flex items-center justify-between">
                <h4 className="font-mono text-sm font-semibold text-foreground">
                  {role.name}
                </h4>
                <span className="text-[10px] text-muted-foreground font-mono">{role.model}</span>
              </div>

              {/* Tools */}
              <div>
                <span className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Tools
                </span>
                <div className="flex flex-wrap gap-1 mt-1">
                  {role.tools.map((tool) => (
                    <span
                      key={tool}
                      className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-secondary/50 text-muted-foreground"
                    >
                      {tool}
                    </span>
                  ))}
                </div>
              </div>
            </div>

            {/* Role prompt — scrollable, takes remaining space */}
            <div className="px-4 pb-4 min-h-0 flex-1 flex flex-col overflow-hidden">
              <span className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground shrink-0">
                Role Prompt
              </span>
              <div className="mt-1 min-h-0 flex-1 overflow-y-auto">
                <pre className="text-[11px] font-mono text-foreground/70 leading-relaxed whitespace-pre-wrap break-words pr-2 pb-1">
                  {role.role_prompt}
                </pre>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
