import {
  createInitialAdmin,
  getCurrentUser,
  getSetupStatus,
  login,
  logout,
} from "./auth";
import {
  createAgentInstance,
  createRun,
  getAgentInstance,
  listAgentInstances,
  listRuns,
  updateAgentInstance,
  type RunPage,
} from "./agentInstances";
import type { Credentials, SetupStatus, User } from "./types";
import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

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
 * Execute one Run. The returned value is the Run the server persisted and committed, so it is
 * the only thing rendered as a result — there is never a fabricated assistant answer.
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
