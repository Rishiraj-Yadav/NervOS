import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ConversationCreate,
  SendMessage,
  archiveConversation,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  listTurns,
  retryTurn,
  sendMessage,
  unarchiveConversation,
} from "./conversations";

export const conversationKeys = {
  all: ["conversations"] as const,
  list: (agentInstanceId?: number, status?: "active" | "archived") =>
    [
      ...conversationKeys.all,
      "list",
      agentInstanceId ?? "all",
      status ?? "active",
    ] as const,
  detail: (id: number) => [...conversationKeys.all, "detail", id] as const,
  turns: (id: number) => [...conversationKeys.all, "turns", id] as const,
};

export const conversationsListQuery = (
  agentInstanceId?: number,
  status?: "active" | "archived"
) =>
  queryOptions({
    queryKey: conversationKeys.list(agentInstanceId, status),
    queryFn: () =>
      listConversations({
        agent_instance_id: agentInstanceId,
        status: status ?? "active",
      }),
  });

export const conversationDetailQuery = (id: number) =>
  queryOptions({
    queryKey: conversationKeys.detail(id),
    queryFn: () => getConversation(id),
  });

export const conversationTurnsQuery = (id: number, refetchInterval?: number | false) =>
  queryOptions({
    queryKey: conversationKeys.turns(id),
    queryFn: () => listTurns(id),
    refetchInterval,
  });

export const useConversationActions = () => {
  const queryClient = useQueryClient();

  const createMutation = useMutation({
    mutationFn: (body: ConversationCreate) => createConversation(body),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: conversationKeys.all });
      queryClient.setQueryData(conversationKeys.detail(data.id), data);
    },
  });

  const archiveMutation = useMutation({
    mutationFn: (id: number) => archiveConversation(id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: conversationKeys.all });
      queryClient.setQueryData(conversationKeys.detail(data.id), data);
    },
  });

  const unarchiveMutation = useMutation({
    mutationFn: (id: number) => unarchiveConversation(id),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: conversationKeys.all });
      queryClient.setQueryData(conversationKeys.detail(data.id), data);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteConversation(id),
    onSuccess: (_, id) => {
      queryClient.invalidateQueries({ queryKey: conversationKeys.all });
      queryClient.removeQueries({ queryKey: conversationKeys.detail(id) });
      queryClient.removeQueries({ queryKey: conversationKeys.turns(id) });
    },
  });

  const sendMessageMutation = useMutation({
    mutationFn: ({
      conversationId,
      body,
    }: {
      conversationId: number;
      body: SendMessage;
    }) => sendMessage(conversationId, body),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({
        queryKey: conversationKeys.turns(vars.conversationId),
      });
      queryClient.invalidateQueries({ queryKey: conversationKeys.all });
    },
  });

  const retryTurnMutation = useMutation({
    mutationFn: ({
      conversationId,
      turnId,
    }: {
      conversationId: number;
      turnId: number;
    }) => retryTurn(conversationId, turnId),
    retry: false,
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries({
        queryKey: conversationKeys.turns(vars.conversationId),
      });
    },
  });

  return {
    createConversation: createMutation,
    archiveConversation: archiveMutation,
    unarchiveConversation: unarchiveMutation,
    deleteConversation: deleteMutation,
    sendMessage: sendMessageMutation,
    retryTurn: retryTurnMutation,
  };
};
