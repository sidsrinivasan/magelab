"""
Live-updating console display for pipeline runs.

Reusable across pipeline types and experiment configurations.
"""

import asyncio
import sys
import time
from typing import Optional


def fmt_duration(seconds: float) -> str:
    """Format seconds as '1h 05m', '1m 05s', or '45s'."""
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


PHASE_ICON = {
    "waiting": "○",
    "setup": "◐",
    "running": "●",
    "success": "✓",
    "partial": "◑",
    "failure": "✗",
    "timeout": "⧖",
    "no_work": "⊘",
}

PHASE_STYLE = {
    "waiting": "\033[2m",  # dim
    "setup": "\033[33m",  # yellow
    "running": "\033[36m",  # cyan
    "success": "\033[32m",  # green
    "partial": "\033[33m",  # yellow
    "failure": "\033[31m",  # red
    "timeout": "\033[35m",  # magenta
    "no_work": "\033[2m",  # dim
}

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

# Characters used in outcome strings (e.g. "SSF", "SPT")
_OUTCOME_CHARS = set("SPFTN")


def _is_outcome_string(phase: str) -> bool:
    """Check if a phase string is a compact outcome string like 'SSF'."""
    return len(phase) > 0 and all(c in _OUTCOME_CHARS for c in phase)


def _outcome_icon(phase: str) -> str:
    """Pick an icon for an outcome string based on its worst outcome."""
    if "F" in phase:
        return PHASE_ICON["failure"]
    if "T" in phase:
        return PHASE_ICON["timeout"]
    if "P" in phase:
        return PHASE_ICON["partial"]
    if "N" in phase:
        return PHASE_ICON["no_work"]
    return PHASE_ICON["success"]


def _outcome_style(phase: str) -> str:
    """Pick a color for an outcome string based on its worst outcome."""
    if "F" in phase:
        return PHASE_STYLE["failure"]
    if "T" in phase:
        return PHASE_STYLE["timeout"]
    if "P" in phase:
        return PHASE_STYLE["partial"]
    if "N" in phase:
        return PHASE_STYLE["no_work"]
    return PHASE_STYLE["success"]


class StatusDisplay:
    """
    Live-updating console display for pipeline runs.

    Handles both single and concurrent runs.
    Redraws in-place on each phase change and on a periodic timer.
    """

    def __init__(
        self,
        num_runs: int,
        abort_chars: set[str],
        label: str = "",
        run_labels: Optional[list[str]] = None,
    ):
        self._num_runs = num_runs
        self._label = label
        if run_labels is not None and len(run_labels) != num_runs:
            raise ValueError(f"run_labels length ({len(run_labels)}) must match num_runs ({num_runs})")
        self._run_labels = run_labels or [""] * num_runs
        self._phases: list[str] = ["waiting"] * num_runs
        self._start_times: list[Optional[float]] = [None] * num_runs
        self._end_times: list[Optional[float]] = [None] * num_runs
        self._global_start = time.monotonic()
        self._lines_drawn = 0
        self._refresh_task: Optional[asyncio.Task] = None
        self._abort_chars = abort_chars

    def set_label(self, run_index: int, label: str) -> None:
        """Update the label for a run (e.g. port assignment)."""
        self._run_labels[run_index] = label

    def update(self, run_index: int, phase: str) -> None:
        """Update phase for a run (0-indexed) and redraw."""
        self._phases[run_index] = phase
        if phase not in ("waiting",) and self._start_times[run_index] is None:
            self._start_times[run_index] = time.monotonic()
        if self._is_terminal(phase):
            self._end_times[run_index] = time.monotonic()
        self._draw()

    def _elapsed(self, i: int) -> str:
        if self._start_times[i] is None:
            return "--"
        end = self._end_times[i] or time.monotonic()
        return fmt_duration(end - self._start_times[i])

    def _is_terminal(self, phase: str) -> bool:
        return _is_outcome_string(phase)

    def _is_aborted(self, phase: str) -> bool:
        """Check if an outcome string contains any abort_on outcome character."""
        return self._is_terminal(phase) and any(c in self._abort_chars for c in phase)

    def _render(self) -> str:
        now = time.monotonic()
        total_elapsed = fmt_duration(now - self._global_start)

        completed = sum(1 for p in self._phases if self._is_terminal(p) and not self._is_aborted(p))
        aborted = sum(1 for p in self._phases if self._is_aborted(p))
        active = sum(1 for p in self._phases if not self._is_terminal(p) and p != "waiting")
        waiting = self._num_runs - completed - aborted - active

        parts = []
        if completed:
            parts.append(f"{PHASE_STYLE['success']}{completed} completed{RESET}")
        if aborted:
            parts.append(f"{PHASE_STYLE['failure']}{aborted} aborted{RESET}")
        if active:
            parts.append(f"{PHASE_STYLE['running']}{active} running{RESET}")
        if waiting:
            parts.append(f"{DIM}{waiting} waiting{RESET}")
        status = "  ".join(parts) if parts else "starting"

        lines = []
        lines.append("")
        header = f"  {BOLD}Runs{RESET}"
        if self._label:
            header += f"  {DIM}[{self._label}]{RESET}"
        lines.append(header)
        lines.append(f"  {status}  {DIM}·  {total_elapsed}{RESET}")
        lines.append(f"  {DIM}{'─' * 40}{RESET}")

        for i in range(self._num_runs):
            phase = self._phases[i]
            elapsed = self._elapsed(i)
            rl = self._run_labels[i]
            label_part = f"  {DIM}{rl}{RESET}" if rl else ""

            if _is_outcome_string(phase):
                icon = _outcome_icon(phase)
                style = _outcome_style(phase)
            else:
                icon = PHASE_ICON.get(phase, PHASE_ICON["running"])
                style = PHASE_STYLE.get(phase, PHASE_STYLE["running"])

            lines.append(
                f"  {style}{icon}{RESET}  {DIM}Run{RESET} {i + 1:<3} {style}{phase:<10}{RESET}{label_part} {DIM}{elapsed:>8}{RESET}"
            )

        lines.append("")
        return "\n".join(lines)

    def _draw(self) -> None:
        """Clear previous output and redraw."""
        if self._lines_drawn > 0:
            sys.stdout.write(f"\033[{self._lines_drawn}A\033[J")
        output = self._render()
        sys.stdout.write(output)
        sys.stdout.flush()
        self._lines_drawn = output.count("\n")

    async def start(self, interval: float = 5.0) -> None:
        """Start periodic refresh (updates elapsed times)."""
        self._draw()
        self._refresh_task = asyncio.create_task(self._refresh_loop(interval))

    async def stop(self) -> None:
        """Stop refresh and do a final draw."""
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._draw()

    async def _refresh_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            self._draw()
