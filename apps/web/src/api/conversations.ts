import { apiRequest } from "./client";

export interface ConversationListItem {
  id: number;
  owner_user_id: number;
  agent_instance_id: number;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationDetail {
  id: number;
  owner_user_id: number;
  agent_instance_id: number;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationPage {
  items: ConversationListItem[];
  next_before_id: number | null;
}

export interface ConversationMessage {
  id: number;
  turn_id: number;
  role: "user" | "assistant";
  content: string;
  source_run_id: number | null;
  created_at: string;
}

export interface ConversationTurn {
  id: number;
  conversation_id: number;
  sequence: number;
  state: "pending" | "running" | "succeeded" | "failed" | "cancelled" | "ambiguous";
  client_message_id: string;
  authoritative_run_id: number | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  user_message: ConversationMessage;
  assistant_message: ConversationMessage | null;
  latest_run_id: number | null;
  latest_run_status: string | null;
  is_retryable: boolean;
}

export interface ConversationTurnPage {
  items: ConversationTurn[];
  next_before_sequence: number | null;
}

export interface ConversationCreate {
  agent_instance_id: number;
  title?: string;
}

export interface SendMessage {
  client_message_id: string;
  content: string;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null;

export const isConversation = (v: unknown): v is ConversationDetail =>
  isObject(v) &&
  typeof v.id === "number" &&
  typeof v.owner_user_id === "number" &&
  typeof v.agent_instance_id === "number";

export const isConversationPage = (v: unknown): v is ConversationPage =>
  isObject(v) && Array.isArray(v.items);

export const isTurn = (v: unknown): v is ConversationTurn =>
  isObject(v) &&
  typeof v.id === "number" &&
  typeof v.sequence === "number" &&
  typeof v.state === "string" &&
  isObject(v.user_message);

export const isTurnPage = (v: unknown): v is ConversationTurnPage =>
  isObject(v) && Array.isArray(v.items);

export const listConversations = (params?: {
  limit?: number;
  before_id?: number;
  agent_instance_id?: number;
}) => {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.before_id) query.set("before_id", String(params.before_id));
  if (params?.agent_instance_id)
    query.set("agent_instance_id", String(params.agent_instance_id));
  const qs = query.toString() ? `?${query.toString()}` : "";
  return apiRequest(`/conversations${qs}`, isConversationPage);
};

export const getConversation = (id: number) =>
  apiRequest(`/conversations/${id}`, isConversation);

export const createConversation = (body: ConversationCreate) =>
  apiRequest(`/conversations`, isConversation, { method: "POST", body });

export const sendMessage = (conversationId: number, body: SendMessage) =>
  apiRequest(`/conversations/${conversationId}/messages`, isTurn, {
    method: "POST",
    body,
  });

export const listTurns = (
  conversationId: number,
  params?: { limit?: number; before_sequence?: number }
) => {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.before_sequence)
    query.set("before_sequence", String(params.before_sequence));
  const qs = query.toString() ? `?${query.toString()}` : "";
  return apiRequest(`/conversations/${conversationId}/turns${qs}`, isTurnPage);
};

export const retryTurn = (conversationId: number, turnId: number) =>
  apiRequest(`/conversations/${conversationId}/turns/${turnId}/retry`, isTurn, {
    method: "POST",
  });
