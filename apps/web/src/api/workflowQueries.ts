import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  cancelWorkflow,
  createWorkflow,
  decideWorkflow,
  deliverWorkflowSignal,
  getWorkflow,
  listWorkflows,
  setWorkflowPaused,
  type CreateWorkflowInput,
} from "./workflows";

export const workflowKeys = {
  all: ["workflows"] as const,
  list: (limit: number, beforeId?: number) =>
    [...workflowKeys.all, "list", limit, beforeId ?? "first"] as const,
  detail: (id: number) => [...workflowKeys.all, "detail", id] as const,
};

export function workflowsListQuery(params?: { limit?: number; before_id?: number }) {
  return queryOptions({
    queryKey: workflowKeys.list(params?.limit ?? 50, params?.before_id),
    queryFn: () => listWorkflows(params),
    refetchInterval: 3000,
    refetchIntervalInBackground: false,
  });
}

export function workflowDetailQuery(id: number | null) {
  return queryOptions({
    queryKey: workflowKeys.detail(id ?? 0),
    queryFn: () => getWorkflow(id as number),
    enabled: id !== null,
    refetchInterval: (query) =>
      query.state.data && ["succeeded", "failed", "cancelled", "needs_review"].includes(
        query.state.data.workflow.status,
      ) ? false : 1500,
    refetchIntervalInBackground: false,
  });
}

export function useWorkflowActions() {
  const client = useQueryClient();
  // Every mutation invalidates the list as well as the detail: a workflow's status, budget
  // and recovery guidance all change together, so leaving the list stale would show one
  // screen saying "running" and another saying "needs review".
  const invalidate = (id?: number) => {
    void client.invalidateQueries({ queryKey: workflowKeys.all });
    if (id !== undefined) void client.invalidateQueries({ queryKey: workflowKeys.detail(id) });
  };
  return {
    create: useMutation({
      mutationFn: (input: CreateWorkflowInput) => createWorkflow(input),
      onSuccess: () => invalidate(),
    }),
    setPaused: useMutation({
      mutationFn: ({ id, paused }: { id: number; paused: boolean }) =>
        setWorkflowPaused(id, paused),
      onSuccess: (_result, variables) => invalidate(variables.id),
    }),
    cancel: useMutation({
      mutationFn: (id: number) => cancelWorkflow(id),
      onSuccess: (_result, id) => invalidate(id),
    }),
    signal: useMutation({
      mutationFn: ({
        id,
        signalKey,
        payload,
        expectedRevision,
      }: {
        id: number;
        signalKey: string;
        payload: Record<string, unknown>;
        expectedRevision: number;
      }) => deliverWorkflowSignal(id, signalKey, payload, expectedRevision),
      onSuccess: (_result, variables) => invalidate(variables.id),
    }),
    decide: useMutation({
      mutationFn: ({
        id,
        decisionId,
        approve,
        expectedRevision,
      }: {
        id: number;
        decisionId: number;
        approve: boolean;
        expectedRevision: number;
      }) => decideWorkflow(id, decisionId, approve, expectedRevision),
      onSuccess: (_result, variables) => invalidate(variables.id),
    }),
  };
}
