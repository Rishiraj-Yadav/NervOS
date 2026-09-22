import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ConversationCreate,
  SendMessage,
  createConversation,
  getConversation,
  listConversations,
  listTurns,
  retryTurn,
  sendMessage,
} from "./conversations";

export const conversationKeys = {
  all: ["conversations"] as const,
  list: (agentInstanceId?: number) =>
    [...conversationKeys.all, "list", agentInstanceId ?? "all"] as const,
  detail: (id: number) => [...conversationKeys.all, "detail", id] as const,
  turns: (id: number) => [...conversationKeys.all, "turns", id] as const,
};

export const conversationsListQuery = (agentInstanceId?: number) =>
  queryOptions({
    queryKey: conversationKeys.list(agentInstanceId),
    queryFn: () => listConversations({ agent_instance_id: agentInstanceId }),
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
    sendMessage: sendMessageMutation,
    retryTurn: retryTurnMutation,
  };
};
