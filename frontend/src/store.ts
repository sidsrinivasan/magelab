import { create } from "zustand";
import type {
  AgentSnapshot,
  AgentState,
  InitialTask,
  QueueEvent,
  RoleInfo,
  RunOutcome,
  RunStatus,
  TabId,
  Task,
  TranscriptEntry,
  Wire,
} from "./types";

interface AppState {
  // Connection
  connected: boolean;
  setConnected: (connected: boolean) => void;

  // Core data
  agents: Record<string, AgentSnapshot>;
  tasks: Record<string, Task>;
  wires: Record<string, Wire>;

  // Topology (adjacency list: agentId -> connectedIds)
  network: Record<string, string[]>;
  queues: Record<string, QueueEvent[]>;
  orgName: string;

  // Config info
  roles: Record<string, RoleInfo>;
  initialTasks: InitialTask[];

  // Transcripts
  transcripts: Record<string, TranscriptEntry[]>;

  // Run state
  runStatus: RunStatus;
  elapsedSeconds: number;
  totalCostUsd: number;
  finalDurationSeconds: number | null;

  // UI state
  selectedAgentId: string | null;
  selectedWireId: string | null;
  inspectedRoleId: string | null;
  activeTab: TabId;

  // Actions
  setInit: (data: {
    agents: Record<string, AgentSnapshot>;
    tasks: Record<string, Task>;
    wires: Record<string, Wire>;
    network: Record<string, string[]>;
    queues: Record<string, QueueEvent[]>;
    orgName: string;
    roles: Record<string, RoleInfo>;
    initialTasks: InitialTask[];
  }) => void;

  updateAgentState: (agentId: string, state: AgentState, currentTaskId: string | null) => void;
  updateTask: (task: Task) => void;
  addQueueEvent: (agentId: string, event: QueueEvent) => void;
  removeQueueEvent: (agentId: string, eventId: string) => void;
  addTranscriptEntry: (agentId: string, entry: TranscriptEntry) => void;
  updateWireMessage: (wireId: string, sender: string, body: string, timestamp: string, participants: string[]) => void;
  setRunFinished: (outcome: RunOutcome, durationSeconds: number, totalCostUsd: number) => void;
  setActiveTab: (tab: TabId) => void;
  setSelectedAgent: (agentId: string | null) => void;
  setSelectedWire: (wireId: string | null) => void;
  setInspectedRole: (roleId: string | null) => void;
  tickElapsed: () => void;
}

export const useStore = create<AppState>((set) => ({
  // Initial state
  connected: false,
  agents: {},
  tasks: {},
  wires: {},
  network: {},
  queues: {},
  orgName: "",
  roles: {},
  initialTasks: [],
  transcripts: {},
  runStatus: "connecting",
  elapsedSeconds: 0,
  totalCostUsd: 0,
  finalDurationSeconds: null,
  selectedAgentId: null,
  selectedWireId: null,
  inspectedRoleId: null,
  activeTab: "dashboard",

  // Actions
  setConnected: (connected) =>
    set((s) => {
      const isTerminal = s.runStatus !== "connecting" && s.runStatus !== "running";
      if (isTerminal) return { connected };
      return { connected, runStatus: connected ? "running" : "connecting" };
    }),

  setInit: ({ agents, tasks, wires, network, queues, orgName, roles, initialTasks }) =>
    set((s) => {
      const isTerminal = s.runStatus !== "connecting" && s.runStatus !== "running";
      if (isTerminal) {
        // Reconnect after run finished — update live data but preserve transcripts and status
        return { agents, tasks, wires, network, queues, orgName, roles, initialTasks, connected: true };
      }
      return {
        agents, tasks, wires, network, queues, orgName, roles, initialTasks, transcripts: {},
        connected: true,
        runStatus: "running",
      };
    }),

  updateAgentState: (agentId, state, currentTaskId) =>
    set((s) => {
      const existing = s.agents[agentId];
      if (!existing) return s;
      return {
        agents: {
          ...s.agents,
          [agentId]: { ...existing, state, current_task_id: currentTaskId },
        },
      };
    }),

  updateTask: (task) =>
    set((s) => ({
      tasks: { ...s.tasks, [task.id]: task },
    })),

  addQueueEvent: (agentId, event) =>
    set((s) => {
      const existing = s.queues[agentId] || [];
      if (existing.some((e) => e.event_id === event.event_id)) return s;
      return { queues: { ...s.queues, [agentId]: [...existing, event] } };
    }),

  removeQueueEvent: (agentId, eventId) =>
    set((s) => {
      const existing = s.queues[agentId];
      if (!existing) return s;
      return { queues: { ...s.queues, [agentId]: existing.filter((e) => e.event_id !== eventId) } };
    }),

  addTranscriptEntry: (agentId, entry) =>
    set((s) => ({
      transcripts: {
        ...s.transcripts,
        [agentId]: [...(s.transcripts[agentId] || []), entry],
      },
    })),

  updateWireMessage: (wireId, sender, body, timestamp, participants) =>
    set((s) => {
      const wire = s.wires[wireId] || { wire_id: wireId, participants, messages: [] };
      return {
        wires: {
          ...s.wires,
          [wireId]: {
            ...wire,
            participants,
            messages: [...wire.messages, { sender, body, timestamp }],
          },
        },
      };
    }),

  setRunFinished: (outcome, durationSeconds, totalCostUsd) =>
    set({ runStatus: outcome, finalDurationSeconds: durationSeconds, totalCostUsd }),

  setActiveTab: (tab) => set({ activeTab: tab }),
  setSelectedAgent: (agentId) => set({ selectedAgentId: agentId }),
  setSelectedWire: (wireId) => set({ selectedWireId: wireId }),
  setInspectedRole: (roleId) => set({ inspectedRoleId: roleId }),
  tickElapsed: () => set((s) => ({ elapsedSeconds: s.elapsedSeconds + 1 })),
}));
