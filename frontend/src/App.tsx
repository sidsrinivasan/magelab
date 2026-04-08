import { useEffect } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useStore } from "./store";
import { connectWebSocket, disconnectWebSocket } from "./ws";
import { StatusBar } from "./components/StatusBar";
import { DashboardTab } from "./components/DashboardTab";
import { TasksTab } from "./components/TasksTab";
import { NetworkGraph } from "./components/NetworkGraph";
import { WiresTab } from "./components/WiresTab";
import { WorkspaceTab } from "./components/WorkspaceTab";
import type { TabId } from "./types";

const TAB_CLASS =
  "data-[state=active]:bg-secondary/60 data-[state=active]:text-foreground text-muted-foreground text-xs font-medium px-4 h-8 rounded-md";

function App() {
  const activeTab = useStore((s) => s.activeTab);
  const setActiveTab = useStore((s) => s.setActiveTab);
  const connected = useStore((s) => s.connected);

  useEffect(() => {
    connectWebSocket();
    return () => disconnectWebSocket();
  }, []);

  return (
    <TooltipProvider>
      <div className="h-screen flex flex-col noise-bg overflow-hidden">
        <StatusBar />

        <Tabs
          value={activeTab}
          onValueChange={(v) => setActiveTab(v as TabId)}
          className="flex-1 flex flex-col min-h-0"
        >
          <div className="border-b border-border/30 px-5">
            <TabsList className="bg-transparent h-10 gap-1">
              <TabsTrigger value="dashboard" className={TAB_CLASS}>
                Agents
              </TabsTrigger>
              <TabsTrigger value="tasks" className={TAB_CLASS}>
                Tasks
              </TabsTrigger>
              <TabsTrigger value="workspace" className={TAB_CLASS}>
                Workspace
              </TabsTrigger>
              <TabsTrigger value="wires" className={TAB_CLASS}>
                Wires
              </TabsTrigger>
              <TabsTrigger value="network" className={TAB_CLASS}>
                Network
              </TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="dashboard" className="flex-1 m-0 overflow-hidden">
            <DashboardTab />
          </TabsContent>

          <TabsContent value="tasks" className="flex-1 m-0 overflow-hidden">
            <TasksTab />
          </TabsContent>

          <TabsContent value="workspace" className="flex-1 m-0 overflow-hidden">
            <WorkspaceTab />
          </TabsContent>

          <TabsContent value="wires" className="flex-1 m-0 overflow-hidden">
            <WiresTab />
          </TabsContent>

          <TabsContent value="network" className="flex-1 m-0 relative overflow-hidden">
            <div className="absolute inset-0">
              <NetworkGraph />
            </div>
          </TabsContent>
        </Tabs>

        {/* Connection indicator */}
        {!connected && (
          <div className="fixed bottom-4 right-4 flex items-center gap-2 px-3 py-2 rounded-lg bg-card border border-border/50 text-xs text-muted-foreground animate-pulse">
            <span className="w-2 h-2 rounded-full bg-state-reviewing" />
            Connecting to server...
          </div>
        )}
      </div>
    </TooltipProvider>
  );
}

export default App;
