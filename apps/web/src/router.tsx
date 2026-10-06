import {
  AgentInstanceRoute,
  AgentInstancesRoute,
  AutomationsRoute,
  AutomationRoute,
  NewAutomationRoute,
  ConversationsRoute,
  ConversationRoute,
  HomeRoute,
  LoginRoute,
  MemoriesRoute,
  MarketplaceRoute,
  ConnectionsRoute,
  RuntimeHealthRoute,
  SecurityRoute,
  WorkflowsRoute,
  RunRoute,
  NotFoundRoute,
  PackagesRoute,
  PackageInstallRoute,
  PackageDetailRoute,
  SessionGate,
  SetupRoute,
} from "./routing";
import { createBrowserRouter, type RouteObject } from "react-router-dom";

export const appRoutes: RouteObject[] = [
  {
    element: <SessionGate />,
    children: [
      { path: "/", element: <HomeRoute /> },
      { path: "/setup", element: <SetupRoute /> },
      { path: "/login", element: <LoginRoute /> },
      { path: "/dashboard", element: <HomeRoute /> },
      { path: "/agents", element: <AgentInstancesRoute /> },
      { path: "/agents/:agentInstanceId", element: <AgentInstanceRoute /> },
      { path: "/packages", element: <PackagesRoute /> },
      { path: "/packages/install", element: <PackageInstallRoute /> },
      { path: "/packages/:packageId/:packageVersion", element: <PackageDetailRoute /> },
      { path: "/automations", element: <AutomationsRoute /> },
      { path: "/automations/new", element: <NewAutomationRoute /> },
      { path: "/automations/:automationId", element: <AutomationRoute /> },
      { path: "/conversations", element: <ConversationsRoute /> },
      { path: "/conversations/:conversationId", element: <ConversationRoute /> },
      { path: "/memories", element: <MemoriesRoute /> },
      { path: "/workflows", element: <WorkflowsRoute /> },
      { path: "/runs/:runId", element: <RunRoute /> },
      { path: "/marketplace", element: <MarketplaceRoute /> },
      { path: "/connections", element: <ConnectionsRoute /> },
      { path: "/runtime", element: <RuntimeHealthRoute /> },
      { path: "/security", element: <SecurityRoute /> },
    ],
  },
  { path: "*", element: <NotFoundRoute /> },
];

export function createAppRouter() {
  return createBrowserRouter(appRoutes);
}
