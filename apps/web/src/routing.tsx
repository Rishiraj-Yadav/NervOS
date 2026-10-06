import { ApiHttpError } from "./api/client";
import { currentUserQuery, setupStatusQuery } from "./api/queries";
import type { User } from "./api/types";
import { ErrorState, LoadingState } from "./components/AsyncState";
import { AgentInstancePage } from "./pages/AgentInstancePage";
import { AutomationsPage } from "./pages/AutomationsPage";
import { AutomationPage, NewAutomationPage } from "./pages/AutomationPage";
import { AgentInstancesPage } from "./pages/AgentInstancesPage";
import { ConversationPage } from "./pages/ConversationPage";
import { ConversationsPage } from "./pages/ConversationsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { MemoriesPage } from "./pages/MemoriesPage";
import { WorkflowsPage } from "./pages/WorkflowsPage";
import { RunPage } from "./pages/RunPage";
import { MarketplacePage } from "./pages/MarketplacePage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { PackagesPage } from "./pages/PackagesPage";
import { PackageInstallPage } from "./pages/PackageInstallPage";
import { PackageDetailPage } from "./pages/PackageDetailPage";
import { SetupPage } from "./pages/SetupPage";
import { AppNavigation } from "./components/AppNavigation";
import { ConnectionsPage } from "./pages/ConnectionsPage";
import { RuntimeHealthPage } from "./pages/RuntimeHealthPage";
import { SecurityPage } from "./pages/SecurityPage";
import { useQuery } from "@tanstack/react-query";
import {
  Navigate,
  Outlet,
  useLocation,
  useOutletContext,
} from "react-router-dom";

export function PackagesRoute() { return <SessionView page="packages" />; }
export function PackageInstallRoute() { return <SessionView page="package-install" />; }
export function PackageDetailRoute() { return <SessionView page="package-detail" />; }
export function AutomationsRoute() { return <SessionView page="automations" />; }
export function AutomationRoute() { return <SessionView page="automation" />; }
export function NewAutomationRoute() { return <SessionView page="new-automation" />; }
export function ConversationsRoute() { return <SessionView page="conversations" />; }
export function ConversationRoute() { return <SessionView page="conversation" />; }
export function MemoriesRoute() { return <SessionView page="memories" />; }
export function WorkflowsRoute() { return <SessionView page="workflows" />; }
export function RunRoute() { return <SessionView page="run" />; }
export function MarketplaceRoute() { return <SessionView page="marketplace" />; }
export function ConnectionsRoute() { return <SessionView page="connections" />; }
export function RuntimeHealthRoute() { return <SessionView page="runtime-health" />; }
export function SecurityRoute() { return <SessionView page="security" />; }

type Session =
  | { kind: "unconfigured" }
  | { kind: "anonymous" }
  | { kind: "authenticated"; user: User };

export function SessionGate() {
  const setup = useQuery(setupStatusQuery());
  const user = useQuery(currentUserQuery(setup.data?.setup_complete === true));

  if (setup.isPending || (setup.data?.setup_complete === true && user.isPending)) {
    return <LoadingState />;
  }
  if (setup.isError) {
    return <ErrorState error={setup.error} onRetry={() => void setup.refetch()} />;
  }
  if (setup.data.setup_complete && user.isError && !isAnonymous(user.error)) {
    return <ErrorState error={user.error} onRetry={() => void user.refetch()} />;
  }

  let session: Session;
  if (!setup.data.setup_complete) {
    session = { kind: "unconfigured" };
  } else if (user.data !== undefined && user.data !== null) {
    session = { kind: "authenticated", user: user.data };
  } else {
    session = { kind: "anonymous" };
  }

  return <Outlet context={session} />;
}

export function HomeRoute() {
  return <SessionView page="home" />;
}

export function SetupRoute() {
  return <SessionView page="setup" />;
}

export function LoginRoute() {
  return <SessionView page="login" />;
}

export function AgentInstancesRoute() {
  return <SessionView page="agents" />;
}

export function AgentInstanceRoute() {
  return <SessionView page="agent" />;
}

export function NotFoundRoute() {
  return <NotFoundPage />;
}

type Page = "home" | "setup" | "login" | "agents" | "agent" | "packages" | "package-install" | "package-detail" | "automations" | "automation" | "new-automation" | "conversations" | "conversation" | "memories" | "workflows" | "run" | "marketplace" | "connections" | "runtime-health" | "security";
function SessionView({ page }: { page: Page }) {
  const session = useOutletContext<Session>();
  const location = useLocation();

  if (session.kind === "unconfigured") {
    return page === "setup" ? <SetupPage /> : <Navigate to="/setup" replace />;
  }
  if (session.kind === "anonymous") {
    return page === "login"
      ? <LoginPage />
      : <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  // Setup and login are entry points, not destinations: once a session exists the gate owns the way
  // out of them. The transition is derived from this render rather than navigated imperatively from
  // the page, because an imperative navigate raced the session write -- the router re-rendered with
  // the new location while the gate still held the previous session, redirected straight back, and
  // bounced the user off the page they had just reached.
  if (page === "setup" || page === "login") {
    return <><AppNavigation /><Navigate to={sessionDestination(location.state)} replace /></>;
  }
  return <><AppNavigation /><AuthenticatedView page={page} user={session.user} /></>;
}

/** Where an authenticated session leaves setup or login for. Anything unrecognised goes home. */
function sessionDestination(state: unknown): string {
  if (typeof state !== "object" || state === null || !("from" in state)) {
    return "/";
  }
  const { from } = state;
  return typeof from === "string" && from.startsWith("/") && from !== "/login" && from !== "/setup"
    ? from
    : "/";
}

function AuthenticatedView({page, user}: {page: Page; user: User}) {
  if (page === "home") {
    return <DashboardPage user={user} />;
  }
  if (page === "agents") {
    return <AgentInstancesPage />;
  }
  if (page === "agent") return <AgentInstancePage />;
  if (page === "packages") return <PackagesPage />;
  if (page === "package-install") return <PackageInstallPage />;
  if (page === "package-detail") return <PackageDetailPage />;
  if (page === "automations") return <AutomationsPage />;
  if (page === "automation") return <AutomationPage />;
  if (page === "new-automation") return <NewAutomationPage />;
  if (page === "conversations") return <ConversationsPage />;
  if (page === "conversation") return <ConversationPage />;
  if (page === "memories") return <MemoriesPage />;
  if (page === "workflows") return <WorkflowsPage />;
  if (page === "run") return <RunPage />;
  if (page === "marketplace") return <MarketplacePage />;
  if (page === "connections") return <ConnectionsPage />;
  if (page === "runtime-health") return <RuntimeHealthPage />;
  if (page === "security") return <SecurityPage />;
  return <Navigate to="/" replace />;
}

function isAnonymous(error: unknown): boolean {
  return (
    error instanceof ApiHttpError &&
    error.status === 401 &&
    error.code === "authentication_required"
  );
}
