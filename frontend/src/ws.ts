import { useStore } from "./store";
import type { ServerMessage } from "./types";

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Connect to the magelab WebSocket server.
 * Automatically reconnects on disconnect with exponential backoff.
 */
export function connectWebSocket(url?: string) {
  const wsUrl = url || `ws://${window.location.host}/ws`;

  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  const store = useStore.getState();
  store.setConnected(false);

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("[ws] connected");
    useStore.getState().setConnected(true);
  };

  ws.onmessage = (event) => {
    try {
      const msg: ServerMessage = JSON.parse(event.data);
      handleMessage(msg);
    } catch (e) {
      console.error("[ws] failed to parse message:", e);
    }
  };

  ws.onclose = () => {
    console.log("[ws] disconnected, reconnecting in 2s...");
    useStore.getState().setConnected(false);
    ws = null;
    scheduleReconnect(wsUrl);
  };

  ws.onerror = (err) => {
    console.error("[ws] error:", err);
    ws?.close();
  };
}

function scheduleReconnect(url: string) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => connectWebSocket(url), 2000);
}

function handleMessage(msg: ServerMessage) {
  const store = useStore.getState();

  switch (msg.type) {
    case "init":
      store.setInit({
        agents: msg.agents,
        tasks: msg.tasks || {},
        wires: msg.wires || {},
        network: msg.network || {},
        queues: msg.queues || {},
        orgName: msg.org_name,
        roles: msg.roles || {},
        initialTasks: msg.initial_tasks || [],
      });
      break;

    case "agent_state_changed":
      store.updateAgentState(msg.agent_id, msg.state, msg.current_task_id);
      break;

    case "task_changed":
      if (msg.task) store.updateTask(msg.task);
      break;

    case "transcript_entry":
      store.addTranscriptEntry(msg.agent_id, {
        entry_type: msg.entry_type,
        content: msg.content,
        timestamp: Date.now(),
      });
      break;

    case "event_dispatched":
      // Events are informational — agent state/task changes
      // come through their own dedicated messages
      break;

    case "wire_message":
      store.updateWireMessage(
        msg.wire_id,
        msg.sender,
        msg.body,
        msg.timestamp,
        msg.participants,
      );
      break;

    case "run_finished":
      store.setRunFinished(msg.outcome, msg.duration_seconds, msg.total_cost_usd);
      break;

    case "queue_event_added":
      store.addQueueEvent(msg.agent_id, msg.event);
      break;

    case "queue_event_removed":
      store.removeQueueEvent(msg.agent_id, msg.event_id);
      break;
  }
}

export function disconnectWebSocket() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (ws) ws.close();
  ws = null;
}
