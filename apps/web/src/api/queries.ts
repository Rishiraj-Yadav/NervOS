import {
  createInitialAdmin,
  getCurrentUser,
  getSetupStatus,
  login,
  logout,
} from "./auth";
import type { Credentials, SetupStatus, User } from "./types";
import { queryOptions, useMutation, useQueryClient } from "@tanstack/react-query";

export const queryKeys = {
  setup: ["setup-status"] as const,
  currentUser: ["auth-session"] as const,
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
