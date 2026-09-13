import { ApiHttpError } from "./api/client";
import { currentUserQuery, setupStatusQuery } from "./api/queries";
import type { User } from "./api/types";
import { ErrorState, LoadingState } from "./components/AsyncState";
import { AgentInstancePage } from "./pages/AgentInstancePage";
import { AgentInstancesPage } from "./pages/AgentInstancesPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { SetupPage } from "./pages/SetupPage";
import { useQuery } from "@tanstack/react-query";
import {
  Navigate,
  Outlet,
  useLocation,
  useOutletContext,
} from "react-router-dom";

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

function SessionView({ page }: { page: "home" | "setup" | "login" | "agents" | "agent" }) {
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
  if (page === "home") {
    return <DashboardPage user={session.user} />;
  }
  if (page === "agents") {
    return <AgentInstancesPage />;
  }
  if (page === "agent") {
    return <AgentInstancePage />;
  }
  return <Navigate to="/" replace />;
}

function isAnonymous(error: unknown): boolean {
  return (
    error instanceof ApiHttpError &&
    error.status === 401 &&
    error.code === "authentication_required"
  );
}
