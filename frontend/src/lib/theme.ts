import type { AgentState } from "../types";

/**
 * Derive a stable HSL hue from a string (role name, agent ID, etc.).
 * Uses a simple hash to spread values across the color wheel.
 */
function hashToHue(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
    hash = hash & hash; // Convert to 32-bit int
  }
  return Math.abs(hash) % 360;
}

/**
 * Get a vibrant color for a role name.
 * Returns an HSL color string with fixed saturation and lightness
 * for consistent readability on dark backgrounds.
 */
export function roleColor(role: string): string {
  const hue = hashToHue(role);
  return `hsl(${hue}, 70%, 65%)`;
}

/**
 * Get a softer background tint for a role (for badges, etc.).
 */
export function roleBgColor(role: string): string {
  const hue = hashToHue(role);
  return `hsla(${hue}, 70%, 65%, 0.12)`;
}

/**
 * Get the color associated with an agent state.
 */
export function stateColor(state: AgentState): string {
  switch (state) {
    case "working":
      return "#34d399";
    case "reviewing":
      return "#fbbf24";
    case "idle":
      return "#64748b";
    case "terminated":
      return "#f87171";
  }
}

/**
 * Get a human-readable label for an agent state.
 */
export function stateLabel(state: AgentState): string {
  switch (state) {
    case "working":
      return "Working";
    case "reviewing":
      return "Reviewing";
    case "idle":
      return "Idle";
    case "terminated":
      return "Terminated";
  }
}

/**
 * Get the color associated with a task status.
 */
export function taskStatusColor(status: string): string {
  switch (status) {
    case "succeeded":
      return "#34d399";
    case "failed":
      return "#f87171";
    case "in_progress":
      return "#60a5fa";
    case "under_review":
      return "#fbbf24";
    default:
      return "#64748b";
  }
}

/**
 * Get a color for a sender in wire chat (based on agent ID hash).
 */
export function senderColor(agentId: string): string {
  const hue = hashToHue(agentId);
  return `hsl(${hue}, 65%, 70%)`;
}

/**
 * Format elapsed seconds into a readable string.
 */
export function formatElapsed(seconds: number | null): string {
  if (seconds == null) return "--:--";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/**
 * Format USD cost.
 */
export function formatCost(usd: number): string {
  if (usd < 0.01) return "$0.00";
  return `$${usd.toFixed(2)}`;
}
