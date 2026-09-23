import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  MemoryCreate,
  MemoryEdit,
  createMemory,
  deleteMemory,
  editMemory,
  getMemory,
  listMemories,
  listMemoryVersions,
} from "./memories";

export const memoryKeys = {
  all: ["memories"] as const,
  list: (scope?: "user" | "agent", agentInstanceId?: number) =>
    [
      ...memoryKeys.all,
      "list",
      scope ?? "all",
      agentInstanceId ?? "all",
    ] as const,
  detail: (id: number) => [...memoryKeys.all, "detail", id] as const,
  versions: (id: number) => [...memoryKeys.all, "versions", id] as const,
};

export const memoriesListQuery = (
  scope?: "user" | "agent",
  agentInstanceId?: number
) =>
  queryOptions({
    queryKey: memoryKeys.list(scope, agentInstanceId),
    queryFn: () =>
      listMemories({
        scope,
        agent_instance_id: agentInstanceId,
      }),
  });

export const memoryDetailQuery = (id: number) =>
  queryOptions({
    queryKey: memoryKeys.detail(id),
    queryFn: () => getMemory(id),
  });

export const memoryVersionsQuery = (id: number) =>
  queryOptions({
    queryKey: memoryKeys.versions(id),
    queryFn: () => listMemoryVersions(id),
  });

export const useMemoryActions = () => {
  const queryClient = useQueryClient();

  const createMutation = useMutation({
    mutationFn: (body: MemoryCreate) => createMemory(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
    },
  });

  const editMutation = useMutation({
    mutationFn: ({ id, body }: { id: number; body: MemoryEdit }) =>
      editMemory(id, body),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
      queryClient.setQueryData(memoryKeys.detail(data.id), data);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: ({ id, expectedVersion }: { id: number; expectedVersion?: number }) =>
      deleteMemory(id, expectedVersion),
    onSuccess: (_, { id }) => {
      queryClient.invalidateQueries({ queryKey: memoryKeys.all });
      queryClient.removeQueries({ queryKey: memoryKeys.detail(id) });
      queryClient.removeQueries({ queryKey: memoryKeys.versions(id) });
    },
  });

  return {
    createMemory: createMutation,
    editMemory: editMutation,
    deleteMemory: deleteMutation,
  };
};
