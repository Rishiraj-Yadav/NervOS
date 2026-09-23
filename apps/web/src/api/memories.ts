import { apiRequest, apiRequestNoContent } from "./client";

export interface MemoryItem {
  id: number;
  owner_user_id: number;
  agent_instance_id: number | null;
  scope: "user" | "agent";
  status: "active" | "deleted";
  current_version: number;
  content: string;
  source_kind: "direct_user" | "promoted_message" | "promoted_run";
  source_id: number | null;
  provenance_type: "user_authored" | "user_approved_inferred";
  created_at: string;
  updated_at: string;
}

export interface MemoryPage {
  items: MemoryItem[];
  next_before_id: number | null;
}

export interface MemoryVersionItem {
  id: number;
  memory_item_id: number;
  version: number;
  content: string;
  content_digest: string;
  source_kind: "direct_user" | "promoted_message" | "promoted_run";
  source_id: number | null;
  provenance_type: "user_authored" | "user_approved_inferred";
  created_by_user_id: number;
  created_at: string;
}

export interface MemoryVersionPage {
  items: MemoryVersionItem[];
  next_before_version: number | null;
}

export interface MemoryCreate {
  scope: "user" | "agent";
  content: string;
  agent_instance_id?: number;
}

export interface MemoryEdit {
  expected_version: number;
  content: string;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null;

export const isMemoryItem = (v: unknown): v is MemoryItem =>
  isObject(v) &&
  typeof v.id === "number" &&
  typeof v.owner_user_id === "number" &&
  typeof v.scope === "string" &&
  typeof v.current_version === "number" &&
  typeof v.content === "string";

export const isMemoryPage = (v: unknown): v is MemoryPage =>
  isObject(v) && Array.isArray(v.items);

export const isMemoryVersionPage = (v: unknown): v is MemoryVersionPage =>
  isObject(v) && Array.isArray(v.items);

export const listMemories = (params?: {
  scope?: "user" | "agent";
  agent_instance_id?: number;
  limit?: number;
  before_id?: number;
}) => {
  const query = new URLSearchParams();
  if (params?.scope) query.set("scope", params.scope);
  if (params?.agent_instance_id)
    query.set("agent_instance_id", String(params.agent_instance_id));
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.before_id) query.set("before_id", String(params.before_id));
  const qs = query.toString() ? `?${query.toString()}` : "";
  return apiRequest(`/memories${qs}`, isMemoryPage);
};

export const getMemory = (id: number) =>
  apiRequest(`/memories/${id}`, isMemoryItem);

export const listMemoryVersions = (
  id: number,
  params?: { limit?: number; before_version?: number }
) => {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.before_version)
    query.set("before_version", String(params.before_version));
  const qs = query.toString() ? `?${query.toString()}` : "";
  return apiRequest(`/memories/${id}/versions${qs}`, isMemoryVersionPage);
};

export const createMemory = (body: MemoryCreate) =>
  apiRequest(`/memories`, isMemoryItem, { method: "POST", body });

export const editMemory = (id: number, body: MemoryEdit) =>
  apiRequest(`/memories/${id}`, isMemoryItem, { method: "PATCH", body });

export const deleteMemory = (id: number, expectedVersion?: number) => {
  const qs = expectedVersion ? `?expected_version=${expectedVersion}` : "";
  return apiRequestNoContent(`/memories/${id}${qs}`, { method: "DELETE" });
};
