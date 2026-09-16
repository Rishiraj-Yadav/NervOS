import {
  createInitialAdmin,
  getCurrentUser,
  getSetupStatus,
  login,
  logout,
} from "./auth";
import {
  cancelRun,
  createAgentInstance,
  createRun,
  getAgentInstance,
  listAgentInstances,
  listRuns,
  updateAgentInstance,
  type Run,
  type RunPage,
} from "./agentInstances";
import type { Credentials, SetupStatus, User } from "./types";
import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

export const RUN_POLL_INTERVAL_MS = 2000;
// Bounded polling budget: ~300 s of continuous polling across nonterminal runs at 2 s.
// A permanently stranded Run will stop polling rather than spinning forever, honoring the rule
// in `docs/runtime.md` that an abandoned Run must not be presented as actively progressing.
export const MAX_NONTERMINAL_POLL_COUNT = 150;

/**
 * Pure polling predicate for the Run history page.
 *
 * Polls every 2 seconds while at least one displayed Run is nonterminal (`created` or `running`)
 * and the polling budget has not been exhausted. Once every Run is terminal, or the budget is
 * reached, polling stops.
 */
export function runPollInterval(
  data: RunPage | undefined,
  dataUpdateCount: number = 0,
): number | false {
  if (data === undefined || data.items.length === 0) {
    return false;
  }
  if (dataUpdateCount >= MAX_NONTERMINAL_POLL_COUNT) {
    return false;
  }
  const hasNonterminal = data.items.some(
    (run: Run) => run.status === "created" || run.status === "running",
  );
  return hasNonterminal ? RUN_POLL_INTERVAL_MS : false;
}

export const queryKeys = {
  setup: ["setup-status"] as const,
  currentUser: ["auth-session"] as const,
  agentInstances: ["agent-instances"] as const,
  agentInstance: (agentInstanceId: number) => ["agent-instance", agentInstanceId] as const,
  agentRuns: (agentInstanceId: number) => ["agent-runs", agentInstanceId] as const,
};

export const setupStatusQuery = () =>
  queryOptions({
    queryKey: queryKeys.setup,
    queryFn: getSetupStatus,
    staleTime: 30_000,
    retry: false,
  });

export const currentUserQuery = (setupComplete: boolean) =>
  queryOptions({
    queryKey: queryKeys.currentUser,
    queryFn: getCurrentUser,
    enabled: setupComplete,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: login,
    onSuccess: (user) => setAuthenticatedState(queryClient, user),
  });
}

export function useSetup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createInitialAdmin,
    onSuccess: (user) => setAuthenticatedState(queryClient, user),
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      queryClient.setQueryData<User | null>(queryKeys.currentUser, null);
    },
  });
}

export const agentInstancesQuery = () =>
  queryOptions({
    queryKey: queryKeys.agentInstances,
    queryFn: () => listAgentInstances(),
    retry: false,
  });

export const agentInstanceQuery = (agentInstanceId: number) =>
  queryOptions({
    queryKey: queryKeys.agentInstance(agentInstanceId),
    queryFn: () => getAgentInstance(agentInstanceId),
    retry: false,
  });

export const agentRunsQuery = (agentInstanceId: number) =>
  queryOptions({
    queryKey: queryKeys.agentRuns(agentInstanceId),
    queryFn: () => listRuns(agentInstanceId),
    retry: false,
    refetchInterval: (query) =>
      runPollInterval(query.state.data, query.state.dataUpdateCount),
  });

export function useCreateAgentInstance() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: createAgentInstance,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.agentInstances }),
  });
}

export function useUpdateAgentInstance(agentInstanceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (update: Parameters<typeof updateAgentInstance>[1]) =>
      updateAgentInstance(agentInstanceId, update),
    onSuccess: (instance) => {
      queryClient.setQueryData(queryKeys.agentInstance(agentInstanceId), instance);
      void queryClient.invalidateQueries({ queryKey: queryKeys.agentInstances });
    },
  });
}

/**
 * Durably accept one Run. The returned value is the Run the server persisted and committed
 * as `created`; a separately-running Worker executes it and terminal persistence updates it.
 */
export function useCreateRun(agentInstanceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: string) => createRun(agentInstanceId, input),
    onSuccess: (run) => {
      queryClient.setQueryData<RunPage>(queryKeys.agentRuns(agentInstanceId), (previous) =>
        previous === undefined ? previous : { ...previous, items: [run, ...previous.items] },
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns(agentInstanceId) });
    },
  });
}

/**
 * Durably cancel one Run.
 *
 * The cache is updated from the server's own answer rather than from an optimistic guess: a Run
 * that had already succeeded or failed is reported as 409 and stays exactly as it was, so
 * pretending locally that it became `cancelled` would show the user something untrue.
 */
export function useCancelRun(agentInstanceId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: number) => cancelRun(runId),
    onSuccess: (run) => {
      queryClient.setQueryData<RunPage>(queryKeys.agentRuns(agentInstanceId), (previous) =>
        previous === undefined
          ? previous
          : {
              ...previous,
              items: previous.items.map((item) => (item.id === run.id ? run : item)),
            },
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.agentRuns(agentInstanceId) });
    },
  });
}

export async function reconcileSetupComplete(queryClient: ReturnType<typeof useQueryClient>) {
  const status = await queryClient.fetchQuery({
    ...setupStatusQuery(),
    staleTime: 0,
  });
  queryClient.setQueryData<SetupStatus>(queryKeys.setup, status);
  if (status.setup_complete) {
    await queryClient.fetchQuery({ ...currentUserQuery(true), staleTime: 0 });
  }
}

function setAuthenticatedState(
  queryClient: ReturnType<typeof useQueryClient>,
  user: User,
) {
  queryClient.setQueryData<SetupStatus>(queryKeys.setup, { setup_complete: true });
  queryClient.setQueryData(queryKeys.currentUser, user);
}

export type { Credentials };
