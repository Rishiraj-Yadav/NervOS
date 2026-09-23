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
  isTerminalStatus,
  listAgentInstances,
  listRunEvents,
  listRuns,
  updateAgentInstance,
  type Run,
  type RunEvent,
  type RunPage,
} from "./agentInstances";
import { ApiProtocolError } from "./client";
import type { Credentials, SetupStatus, User } from "./types";
import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

export const RUN_POLL_INTERVAL_MS = 2000;
export const RUN_POLL_SLOW_INTERVAL_MS = 10000;
// Roughly a minute of fast feedback (30 × 2 s) before settling into the slow cadence.
export const RUN_POLL_FAST_WINDOW_COUNT = 30;
// A drain is bounded by construction -- every page advances the cursor strictly and the stream is
// finite -- so this is a guard against a server that never reports an end, not a coverage cap. It
// fails loudly rather than silently truncating a timeline.
export const MAX_DRAIN_PAGES = 50;

/**
 * Pure two-tier polling cadence for a still-live Run.
 *
 * The first thirty updates keep today's responsiveness; after that the interval widens and never
 * ends on its own. There is deliberately no cap on how long a live Run is observed: an abandoned
 * Run is no longer a risk *because* the timeline is durable and the derived phase is truthful, so
 * a Run that is not progressing now renders as exactly that instead of being hidden by a request
 * budget. Polling still stops entirely once every displayed Run is terminal.
 */
export function runPollCadence(dataUpdateCount: number): number {
  return dataUpdateCount < RUN_POLL_FAST_WINDOW_COUNT
    ? RUN_POLL_INTERVAL_MS
    : RUN_POLL_SLOW_INTERVAL_MS;
}

/**
 * Pure polling predicate for the Run history page.
 *
 * Polls while at least one displayed Run is nonterminal, and stops the moment they are all final.
 */
export function runPollInterval(
  data: RunPage | undefined,
  dataUpdateCount: number = 0,
): number | false {
  if (data === undefined || data.items.length === 0) {
    return false;
  }
  const hasNonterminal = data.items.some((run: Run) => !isTerminalStatus(run.status));
  return hasNonterminal ? runPollCadence(dataUpdateCount) : false;
}

/** The highest sequence applied so far, or 0 for a timeline that has not loaded anything yet. */
export function lastAppliedSequence(events: RunEvent[]): number {
  return events.length === 0 ? 0 : events[events.length - 1].sequence;
}

/**
 * Merge newly received Events into the applied timeline, keyed on `sequence` alone.
 *
 * Never a timestamp and never array position: `sequence` is the Run-local order the server
 * allocated, so it is the only stable identity an Event has. The merge is a union, which is what
 * makes it safe to apply responses in any order -- an older response can only contribute sequences
 * already present, so the applied cursor can never move backwards and no Event is ever dropped.
 * A duplicate delivery therefore renders one row, not two.
 */
export function mergeRunEvents(applied: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  if (incoming.length === 0) {
    return applied;
  }
  const bySequence = new Map<number, RunEvent>();
  for (const event of applied) {
    bySequence.set(event.sequence, event);
  }
  for (const event of incoming) {
    bySequence.set(event.sequence, event);
  }
  return [...bySequence.values()].sort((left, right) => left.sequence - right.sequence);
}

/**
 * Fetch every page that has not been applied yet, in one drain.
 *
 * The cursor comes from the *cache* rather than from the caller, and the merged result is written
 * through the cache's own updater, so the freshest applied timeline is always the base of the
 * merge -- even when a slower earlier request resolves after a newer one.
 */
async function drainRunEvents(
  client: QueryClient,
  queryKey: readonly unknown[],
  runId: number,
): Promise<RunEvent[]> {
  const applied = client.getQueryData<RunEvent[]>(queryKey) ?? [];
  const collected: RunEvent[] = [];
  let cursor = lastAppliedSequence(applied);

  for (let page = 0; ; page += 1) {
    if (page >= MAX_DRAIN_PAGES) {
      throw new ApiProtocolError(
        "NervOS returned an endless run timeline. Please reload and try again.",
      );
    }
    const response = await listRunEvents(runId, cursor);
    collected.push(...response.items);
    if (response.next_after_sequence === null) {
      break;
    }
    cursor = response.next_after_sequence;
  }

  let merged: RunEvent[] = applied;
  client.setQueryData<RunEvent[]>(queryKey, (current) => {
    merged = mergeRunEvents(current ?? [], collected);
    return merged;
  });
  return merged;
}

export const queryKeys = {
  setup: ["setup-status"] as const,
  currentUser: ["auth-session"] as const,
  agentInstances: ["agent-instances"] as const,
  agentInstance: (agentInstanceId: number) => ["agent-instance", agentInstanceId] as const,
  agentRuns: (agentInstanceId: number) => ["agent-runs", agentInstanceId] as const,
  runEvents: (runId: number) => ["run-events", runId] as const,
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
      queryClient.removeQueries({ queryKey: queryKeys.agentInstances });
      queryClient.removeQueries({ queryKey: ["conversations"] });
      queryClient.removeQueries({ queryKey: ["memories"] });
      queryClient.removeQueries({ queryKey: ["automations"] });
      queryClient.removeQueries({ queryKey: ["agent-runs"] });
      queryClient.removeQueries({ queryKey: ["run-events"] });
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

/**
 * One Run's durable Event timeline.
 *
 * Polling follows the Run's own liveness: `isTerminal` is the caller's observed Run status, and a
 * terminal Run stops the interval entirely. That is safe because every fetch drains the timeline
 * to its end, so no Event can be left unread behind a stopped timer.
 */
export const runEventsQuery = (runId: number, isTerminal: boolean) =>
  queryOptions({
    queryKey: queryKeys.runEvents(runId),
    queryFn: ({ client, queryKey }) => drainRunEvents(client, queryKey, runId),
    retry: false,
    refetchInterval: (query) =>
      isTerminal ? false : runPollCadence(query.state.dataUpdateCount),
  });

export function useRunEvents(runId: number, isTerminal: boolean) {
  const query = useQuery(runEventsQuery(runId, isTerminal));
  const { refetch } = query;
  const awaitingTerminalDrain = useRef(!isTerminal);

  useEffect(() => {
    if (!isTerminal) {
      awaitingTerminalDrain.current = true;
      return;
    }
    if (!awaitingTerminalDrain.current) {
      return;
    }
    // The Run just became terminal, so the interval has already stopped. One final fetch is
    // issued anyway, and because a fetch drains every remaining page this single catch-up cycle
    // can span several requests. Without it the terminal Event -- committed in the same
    // transaction that made the Run terminal -- could be missed by a timer that stopped first.
    awaitingTerminalDrain.current = false;
    void refetch();
  }, [isTerminal, refetch]);

  return query;
}

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
