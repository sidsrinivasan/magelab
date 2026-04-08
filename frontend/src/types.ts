// Types mirroring Python schemas in magelab.state

export type AgentState = "idle" | "working" | "reviewing" | "terminated";

export type TaskStatus =
  | "created" | "assigned" | "in_progress"
  | "under_review" | "approved" | "changes_requested" | "review_failed"
  | "succeeded" | "failed";

export type ReviewDecision = "approved" | "changes_requested" | "failed";

export type RunOutcome = "no_work" | "success" | "partial" | "failure" | "timeout";

export interface Review {
  reviewer_id: string;
  decision: ReviewDecision;
  comment: string | null;
  timestamp: string;
}

export interface ReviewRecord {
  reviewer_id: string;
  requester_id: string;
  request_message: string | null;
  round_number: number;
  created_at: string;
  review: Review | null;
}

export interface AgentSnapshot {
  agent_id: string;
  role: string;
  model: string;
  state: AgentState;
  current_task_id: string | null;
  tools: string[];
}

export interface Task {
  id: string;
  title: string;
  description: string;
  status: TaskStatus;
  assignment_history: string[];
  review_required: boolean;
  current_review_round: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  assigned_to?: string | null;
  assigned_by?: string | null;
  review_history: ReviewRecord[];
  active_reviews: Record<string, ReviewRecord> | null;
}

export interface WireMessage {
  sender: string;
  body: string;
  timestamp: string;
}

export interface Wire {
  wire_id: string;
  participants: string[];
  messages: WireMessage[];
}

export interface TranscriptEntry {
  entry_type: string;
  content: string;
  timestamp: number;
}

export interface RoleInfo {
  name: string;
  role_prompt: string;
  tools: string[];
  model: string;
}

export interface InitialTask {
  task_id: string;
  title: string;
  description: string;
  assigned_to: string;
}

export type RunStatus = "connecting" | "running" | RunOutcome;

export type TabId = "dashboard" | "tasks" | "workspace" | "wires" | "network";

// WebSocket message types (server -> client)

export interface InitMessage {
  type: "init";
  org_name: string;
  agents: Record<string, AgentSnapshot>;
  tasks: Record<string, Task>;
  wires: Record<string, Wire>;
  network: Record<string, string[]>;
  queues: Record<string, QueueEvent[]>;
  roles: Record<string, RoleInfo>;
  initial_tasks: InitialTask[];
}

export interface AgentStateChangedMessage {
  type: "agent_state_changed";
  agent_id: string;
  state: AgentState;
  current_task_id: string | null;
}

export interface TaskChangedMessage {
  type: "task_changed";
  task: Task | null;
}

export interface TranscriptEntryMessage {
  type: "transcript_entry";
  agent_id: string;
  entry_type: string;
  content: string;
}

export interface EventDispatchedMessage {
  type: "event_dispatched";
  event_id: string;
  event_type: string;
  target_id: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface WireMessageBroadcast {
  type: "wire_message";
  wire_id: string;
  sender: string;
  body: string;
  timestamp: string;
  participants: string[];
}

export interface RunFinishedMessage {
  type: "run_finished";
  outcome: RunOutcome;
  duration_seconds: number;
  total_cost_usd: number;
}

export interface QueueEvent {
  event_id: string;
  event_type: string;
  target_id: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface QueueEventAddedMessage {
  type: "queue_event_added";
  agent_id: string;
  event: QueueEvent;
}

export interface QueueEventRemovedMessage {
  type: "queue_event_removed";
  agent_id: string;
  event_id: string;
}

export type ServerMessage =
  | InitMessage
  | AgentStateChangedMessage
  | TaskChangedMessage
  | TranscriptEntryMessage
  | EventDispatchedMessage
  | WireMessageBroadcast
  | RunFinishedMessage
  | QueueEventAddedMessage
  | QueueEventRemovedMessage;
