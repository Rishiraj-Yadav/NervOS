import { apiRequest } from "./client";

export type RunStatus = "created" | "running" | "succeeded" | "failed" | "cancelled";

export interface AgentInstance {
  id: number;
  agent_key: string;
  agent_definition_version: string;
  display_name: string;
  enabled: boolean;
  model_provider: string;
  model_name: string;
  created_at: string;
  updated_at: string;
}

export interface AgentInstancePage {
  items: AgentInstance[];
  next_before_id: number | null;
}

export interface RunUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
}

export interface Run {
  id: number;
  agent_instance_id: number;
  agent_key: string;
  agent_definition_version: string;
  model_provider: string;
  model_name: string;
  input_text: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  output_text: string | null;
  finish_reason: string | null;
  error_code: string | null;
  error_message: string | null;
  usage: RunUsage | null;
  elapsed_ms: number | null;
}

export interface RunPage {
  items: Run[];
  next_before_id: number | null;
}

export interface AgentInstanceCreateInput {
  agent_key: string;
  agent_definition_version: string;
  display_name: string;
  model_provider: string;
  model_name: string;
  enabled?: boolean;
}

/** A configuration update always carries all three values and never `enabled`. */
export interface AgentInstanceConfigurationUpdate {
  display_name: string;
  model_provider: string;
  model_name: string;
}

/** An enable-state update carries `enabled` alone and no configuration. */
export interface AgentInstanceEnableUpdate {
  enabled: boolean;
}

const RUN_STATUSES: readonly RunStatus[] = [
  "created",
  "running",
  "succeeded",
  "failed",
  "cancelled",
];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableInteger(value: unknown): value is number | null {
  return value === null || Number.isInteger(value);
}

function isRunStatus(value: unknown): value is RunStatus {
  return typeof value === "string" && (RUN_STATUSES as readonly string[]).includes(value);
}

export function isAgentInstance(value: unknown): value is AgentInstance {
  return (
    isRecord(value) &&
    Number.isInteger(value.id) &&
    typeof value.agent_key === "string" &&
    typeof value.agent_definition_version === "string" &&
    typeof value.display_name === "string" &&
    typeof value.enabled === "boolean" &&
    typeof value.model_provider === "string" &&
    typeof value.model_name === "string" &&
    typeof value.created_at === "string" &&
    typeof value.updated_at === "string"
  );
}

export function isRun(value: unknown): value is Run {
  return (
    isRecord(value) &&
    Number.isInteger(value.id) &&
    Number.isInteger(value.agent_instance_id) &&
    typeof value.agent_key === "string" &&
    typeof value.agent_definition_version === "string" &&
    typeof value.model_provider === "string" &&
    typeof value.model_name === "string" &&
    typeof value.input_text === "string" &&
    isRunStatus(value.status) &&
    typeof value.created_at === "string" &&
    isNullableString(value.started_at) &&
    isNullableString(value.finished_at) &&
    isNullableString(value.output_text) &&
    isNullableString(value.finish_reason) &&
    isNullableString(value.error_code) &&
    isNullableString(value.error_message) &&
    isNullableInteger(value.elapsed_ms) &&
    (value.usage === null || isRunUsage(value.usage))
  );
}

function isRunUsage(value: unknown): value is RunUsage {
  return (
    isRecord(value) &&
    isNullableInteger(value.input_tokens) &&
    isNullableInteger(value.output_tokens) &&
    isNullableInteger(value.total_tokens)
  );
}

export function isAgentInstancePage(value: unknown): value is AgentInstancePage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isAgentInstance) &&
    isNullableInteger(value.next_before_id)
  );
}

export function isRunPage(value: unknown): value is RunPage {
  return (
    isRecord(value) &&
    Array.isArray(value.items) &&
    value.items.every(isRun) &&
    isNullableInteger(value.next_before_id)
  );
}

function pageQuery(beforeId?: number): string {
  return beforeId === undefined ? "" : `?before_id=${beforeId}`;
}

export function listAgentInstances(beforeId?: number): Promise<AgentInstancePage> {
  return apiRequest(`/agent-instances${pageQuery(beforeId)}`, isAgentInstancePage);
}

export function createAgentInstance(input: AgentInstanceCreateInput): Promise<AgentInstance> {
  return apiRequest("/agent-instances", isAgentInstance, { method: "POST", body: input });
}

export function getAgentInstance(agentInstanceId: number): Promise<AgentInstance> {
  return apiRequest(`/agent-instances/${agentInstanceId}`, isAgentInstance);
}

export function updateAgentInstance(
  agentInstanceId: number,
  update: AgentInstanceConfigurationUpdate | AgentInstanceEnableUpdate,
): Promise<AgentInstance> {
  return apiRequest(`/agent-instances/${agentInstanceId}`, isAgentInstance, {
    method: "PATCH",
    body: update,
  });
}

export function createRun(agentInstanceId: number, input: string): Promise<Run> {
  return apiRequest(`/agent-instances/${agentInstanceId}/runs`, isRun, {
    method: "POST",
    body: { input },
  });
}

export function listRuns(agentInstanceId: number, beforeId?: number): Promise<RunPage> {
  return apiRequest(`/agent-instances/${agentInstanceId}/runs${pageQuery(beforeId)}`, isRunPage);
}

export function getRun(runId: number): Promise<Run> {
  return apiRequest(`/runs/${runId}`, isRun);
}

/** Durably cancel one owned Run. Idempotent: cancelling a cancelled Run returns the same Run. */
export function cancelRun(runId: number): Promise<Run> {
  return apiRequest(`/runs/${runId}/cancel`, isRun, { method: "POST" });
}
