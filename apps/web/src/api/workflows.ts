import { apiRequest } from "./client";

export type WorkflowStatus =
  | "pending"
  | "runnable"
  | "running"
  | "waiting"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "needs_review";

export type WorkflowWaitKind = "time" | "signal" | "owner_decision";

export interface WorkflowBudgetView {
  steps_used: number;
  steps_allowed: number;
  model_calls_reserved: number;
  model_calls_remaining: number;
  tool_calls_reserved: number;
  tool_calls_remaining: number;
  output_tokens_reserved: number;
  output_tokens_remaining: number;
}

export interface WorkflowSummary {
  id: number;
  workflow_kind: string;
  status: WorkflowStatus;
  paused: boolean;
  step_count: number;
  checkpoint_revision: number;
  wait_kind: WorkflowWaitKind | null;
  signal_key: string | null;
  decision_key: string | null;
  wakeup_at: string | null;
  deadline_at: string | null;
  created_at: string | null;
  finished_at: string | null;
  review_reason: string | null;
  budget: WorkflowBudgetView;
}

export interface WorkflowList {
  workflows: WorkflowSummary[];
  next_before_id: number | null;
}

export interface WorkflowStepView {
  step_number: number;
  run_id: number | null;
  status: string;
  run_status: string | null;
  job_phase: string | null;
  summary: string | null;
  expected_checkpoint_revision: number;
  finished_at: string | null;
}

// A checkpoint's *shape*, never its values. The API withholds application state from the
// dashboard on purpose, so this type has no content field to render by accident.
export interface WorkflowCheckpointShape {
  revision: number;
  step_number: number;
  byte_size: number;
  keys: string[];
  created_at: string | null;
}

export interface WorkflowDecisionView {
  id: number;
  checkpoint_revision: number;
  tool_definition_id: number;
  upstream_name: string;
  arguments_digest: string;
  preview: Record<string, unknown>;
  state: string;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  consumed_at: string | null;
}

export interface WorkflowSignalView {
  id: number;
  signal_key: string;
  expected_revision: number;
  received_at: string;
  accepted_at: string | null;
  outcome: string;
}

export interface WorkflowRecovery {
  needs_attention: boolean;
  summary: string;
  detail?: string | null;
  guidance?: string;
  actions: string[];
}

export interface WorkflowDetail {
  workflow: WorkflowSummary;
  steps: WorkflowStepView[];
  checkpoints: WorkflowCheckpointShape[];
  decisions: WorkflowDecisionView[];
  signals: WorkflowSignalView[];
  recovery: WorkflowRecovery;
}

export interface CreateWorkflowInput {
  agent_instance_id: number;
  submission_key: string;
  input_text: string;
  workflow_kind: string;
}

export interface WorkflowSignalOutcome {
  accepted: boolean;
  outcome: string;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null;

const isBudget = (v: unknown): v is WorkflowBudgetView =>
  isObject(v) &&
  typeof v.steps_used === "number" &&
  typeof v.steps_allowed === "number" &&
  typeof v.model_calls_remaining === "number" &&
  typeof v.tool_calls_remaining === "number" &&
  typeof v.output_tokens_remaining === "number";

export const isWorkflowSummary = (v: unknown): v is WorkflowSummary =>
  isObject(v) &&
  typeof v.id === "number" &&
  typeof v.status === "string" &&
  typeof v.workflow_kind === "string" &&
  typeof v.paused === "boolean" &&
  typeof v.step_count === "number" &&
  typeof v.checkpoint_revision === "number" &&
  isBudget(v.budget);

export const isWorkflowList = (v: unknown): v is WorkflowList =>
  isObject(v) && Array.isArray(v.workflows);

export const isWorkflowDetail = (v: unknown): v is WorkflowDetail =>
  isObject(v) &&
  isWorkflowSummary(v.workflow) &&
  Array.isArray(v.steps) &&
  Array.isArray(v.checkpoints) &&
  Array.isArray(v.decisions) &&
  Array.isArray(v.signals) &&
  isObject(v.recovery) &&
  typeof v.recovery.needs_attention === "boolean" &&
  typeof v.recovery.summary === "string" &&
  Array.isArray(v.recovery.actions);

const isSignalOutcome = (v: unknown): v is WorkflowSignalOutcome =>
  isObject(v) && typeof v.accepted === "boolean" && typeof v.outcome === "string";

export const listWorkflows = (params?: { limit?: number; before_id?: number }) => {
  const query = new URLSearchParams();
  if (params?.limit) query.set("limit", String(params.limit));
  if (params?.before_id) query.set("before_id", String(params.before_id));
  const qs = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<WorkflowList>(`/workflows${qs}`, isWorkflowList);
};

export const getWorkflow = (id: number) =>
  apiRequest<WorkflowDetail>(`/workflows/${id}`, isWorkflowDetail);

export const createWorkflow = (body: CreateWorkflowInput) =>
  apiRequest<WorkflowSummary>("/workflows", isWorkflowSummary, { method: "POST", body });

export const setWorkflowPaused = (id: number, paused: boolean) =>
  apiRequest<WorkflowSummary>(`/workflows/${id}/pause`, isWorkflowSummary, {
    method: "POST",
    body: { paused },
  });

export const cancelWorkflow = (id: number) =>
  apiRequest<WorkflowSummary>(`/workflows/${id}/cancel`, isWorkflowSummary, {
    method: "POST",
    body: {},
  });

export const deliverWorkflowSignal = (
  id: number,
  signalKey: string,
  payload: Record<string, unknown>,
  expectedRevision: number,
) =>
  apiRequest<WorkflowSignalOutcome>(`/workflows/${id}/signals`, isSignalOutcome, {
    method: "POST",
    body: {
      signal_key: signalKey,
      payload,
      expected_revision: expectedRevision,
    },
  });

export const decideWorkflow = (
  id: number,
  decisionId: number,
  approve: boolean,
  expectedRevision: number,
) =>
  apiRequest<Record<string, unknown>>(
    `/workflows/${id}/decisions/${decisionId}`,
    isObject,
    {
      method: "POST",
      body: { approve, expected_revision: expectedRevision },
    },
  );