import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  type Node,
  type Edge,
  type NodeProps,
  Handle,
  Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import ELK from "elkjs/lib/elk.bundled.js";
import { Badge } from "@/components/ui/badge";
import { useStore } from "../store";
import { roleColor, roleBgColor, stateColor, stateLabel } from "../lib/theme";
import type { AgentSnapshot } from "../types";

const elk = new ELK();

// Estimated node dimensions for ELK layout
const NODE_WIDTH = 180;
const NODE_HEIGHT = 70;

// Custom node component — generous card-sized for easy clicking
function AgentNode({ data }: NodeProps) {
  const agent = data.agent as AgentSnapshot;
  const color = stateColor(agent.state);

  return (
    <div
      className="px-4 py-3 rounded-lg border border-border/50 bg-card/90 backdrop-blur-sm min-w-[160px] cursor-pointer card-glow transition-all"
      style={{
        borderLeftWidth: 3,
        borderLeftColor: color,
      }}
    >
      <Handle type="target" position={Position.Left} className="!bg-border !w-2 !h-2" />
      <Handle type="source" position={Position.Right} className="!bg-border !w-2 !h-2" />

      <div className="flex items-center gap-2 mb-1.5">
        <span
          className={`inline-block w-2 h-2 rounded-full shrink-0 ${
            agent.state === "working"
              ? "pulse-working"
              : agent.state === "reviewing"
                ? "pulse-reviewing"
                : ""
          }`}
          style={{ backgroundColor: color }}
        />
        <span className="font-mono text-xs font-medium text-foreground">
          {agent.agent_id}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <Badge
          variant="secondary"
          className="text-[9px] font-medium border-0 py-0 h-4"
          style={{
            color: roleColor(agent.role),
            backgroundColor: roleBgColor(agent.role),
          }}
        >
          {agent.role}
        </Badge>
        <span
          className="text-[9px] font-medium uppercase tracking-wider"
          style={{ color }}
        >
          {stateLabel(agent.state)}
        </span>
      </div>
    </div>
  );
}

const nodeTypes = { agent: AgentNode };

export function NetworkGraph() {
  const agents = useStore((s) => s.agents);
  const network = useStore((s) => s.network);
  const wires = useStore((s) => s.wires);
  const setSelectedAgent = useStore((s) => s.setSelectedAgent);
  const setActiveTab = useStore((s) => s.setActiveTab);
  const [showWireWeight, setShowWireWeight] = useState(false);

  const agentList = useMemo(() => Object.values(agents), [agents]);
  const agentIds = useMemo(() => agentList.map((a) => a.agent_id).sort().join(","), [agentList]);

  // Compute message counts between each pair of agents (across all wires)
  const pairMessageCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const wire of Object.values(wires)) {
      // Count messages per sender in this wire
      for (const msg of wire.messages) {
        // Each message from a sender counts toward edges between sender and all other participants
        for (const other of wire.participants) {
          if (other !== msg.sender) {
            const key = [msg.sender, other].sort().join("↔");
            counts.set(key, (counts.get(key) || 0) + 1);
          }
        }
      }
    }
    return counts;
  }, [wires]);

  const maxMessages = useMemo(() => {
    let max = 0;
    for (const count of pairMessageCounts.values()) {
      if (count > max) max = count;
    }
    return max;
  }, [pairMessageCounts]);

  // Build edges from adjacency list (needed before layout for ELK)
  const edgePairs = useMemo(() => {
    const pairs: { source: string; target: string }[] = [];
    const edgeSet = new Set<string>();
    for (const [source, targets] of Object.entries(network)) {
      for (const target of targets) {
        const key = [source, target].sort().join("->");
        if (edgeSet.has(key)) continue;
        edgeSet.add(key);
        pairs.push({ source, target });
      }
    }
    return pairs;
  }, [network]);

  // Determine if graph is dense (>= 60% of max possible edges → use circular)
  const isDense = useMemo(() => {
    const n = agentList.length;
    if (n <= 2) return true;
    const maxEdges = (n * (n - 1)) / 2;
    return edgePairs.length >= maxEdges * 0.6;
  }, [agentList.length, edgePairs.length]);

  // Circular layout for dense graphs
  const circularPositions = useMemo(() => {
    const positions: Record<string, { x: number; y: number }> = {};
    const count = agentList.length;
    if (count === 0) return positions;
    const RADIUS = Math.max(250, count * 70);
    const CX = RADIUS + 120;
    const CY = RADIUS + 80;
    for (let i = 0; i < count; i++) {
      const angle = (2 * Math.PI * i) / count - Math.PI / 2;
      positions[agentList[i].agent_id] = {
        x: CX + RADIUS * Math.cos(angle),
        y: CY + RADIUS * Math.sin(angle),
      };
    }
    return positions;
  }, [agentList]);

  // ELK layout for sparse graphs — async, stored in state
  const [elkPositions, setElkPositions] = useState<Record<string, { x: number; y: number }>>({});
  const layoutVersionRef = useRef(0);

  useEffect(() => {
    if (!agentIds || isDense) return;

    const version = ++layoutVersionRef.current;
    const ids = agentIds.split(",");

    const elkGraph = {
      id: "root",
      layoutOptions: {
        "elk.algorithm": "stress",
        "elk.stress.desiredEdgeLength": "500",
        "elk.spacing.nodeNode": "300",
        "elk.spacing.componentComponent": "400",
        "elk.padding": "[top=100,left=100,bottom=100,right=100]",
      },
      children: ids.map((id) => ({
        id,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
      })),
      edges: edgePairs.map((e, i) => ({
        id: `elk-e-${i}`,
        sources: [e.source],
        targets: [e.target],
      })),
    };

    elk.layout(elkGraph).then((result) => {
      if (version !== layoutVersionRef.current) return;
      const positions: Record<string, { x: number; y: number }> = {};
      for (const child of result.children || []) {
        positions[child.id] = { x: child.x ?? 0, y: child.y ?? 0 };
      }
      setElkPositions(positions);
    });
  }, [agentIds, edgePairs, isDense]);

  // Build nodes: circular for dense graphs, ELK for sparse
  const nodes = useMemo<Node[]>(() => {
    const count = agentList.length;
    if (count === 0) return [];

    const positions = isDense
      ? circularPositions
      : (Object.keys(elkPositions).length === count ? elkPositions : circularPositions);

    return agentList.map((agent) => ({
      id: agent.agent_id,
      type: "agent",
      position: positions[agent.agent_id] || { x: 0, y: 0 },
      data: { agent },
    }));
  }, [agentList, isDense, circularPositions, elkPositions]);

  // Build styled edges, optionally weighted by wire messages
  const edges = useMemo<Edge[]>(() => {
    return edgePairs.map(({ source, target }) => {
      let strokeWidth = 2;
      let strokeColor = "rgba(148, 163, 184, 0.2)";

      if (showWireWeight && maxMessages > 0) {
        const pairKey = [source, target].sort().join("↔");
        const count = pairMessageCounts.get(pairKey) || 0;
        const ratio = count / maxMessages;
        strokeWidth = 1 + ratio * 8;
        const opacity = 0.1 + ratio * 0.6;
        strokeColor = count > 0
          ? `rgba(96, 165, 250, ${opacity})`
          : "rgba(148, 163, 184, 0.08)";
      }

      return {
        id: `e-${source}-${target}`,
        source,
        target,
        style: { stroke: strokeColor, strokeWidth, pointerEvents: "none" as const },
        type: "default",
        focusable: false,
        selectable: false,
      };
    });
  }, [edgePairs, showWireWeight, pairMessageCounts, maxMessages]);

  const onNodeClick = useCallback(
    (_: React.MouseEvent, node: Node) => {
      setSelectedAgent(node.id);
      setActiveTab("dashboard");
    },
    [setSelectedAgent, setActiveTab],
  );

  if (agentList.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        Waiting for network data...
      </div>
    );
  }

  return (
    <div className="h-full w-full relative">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodeClick={onNodeClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        proOptions={{ hideAttribution: true }}
        minZoom={0.3}
        maxZoom={2}
        nodesDraggable
        nodesConnectable={false}
        edgesFocusable={false}
        elementsSelectable={false}
      >
        <Background color="rgba(148, 163, 184, 0.05)" gap={24} />
        <Controls showInteractive={false} />
      </ReactFlow>

      {/* Wire weight toggle */}
      {Object.keys(wires).length > 0 && (
        <button
          onClick={() => setShowWireWeight(!showWireWeight)}
          className={`absolute top-3 right-3 z-10 px-3 py-1.5 rounded-md text-xs font-medium transition-colors border ${
            showWireWeight
              ? "bg-primary/20 border-primary/40 text-primary"
              : "bg-card/80 border-border/50 text-muted-foreground hover:text-foreground"
          }`}
        >
          {showWireWeight ? "Wire Activity: ON" : "Wire Activity"}
        </button>
      )}
    </div>
  );
}
