import { http, HttpResponse } from "msw";
import userEvent from "@testing-library/user-event";

import type { ExecutionPhase, RunEventType, RunStatus } from "../api/agentInstances";
import { appRoutes } from "../router";
import { renderWithRouter } from "./render";

export const API_USER = { id: 1, username: "admin", role: "admin", is_active: true };

export interface ApiAgentInstance {
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

export interface ApiRun {
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
  usage: { input_tokens: number | null; output_tokens: number | null; total_tokens: number | null } | null;
  elapsed_ms: number | null;
  execution_phase: ExecutionPhase | null;
  retry_available_at: string | null;
}

export interface ApiRunEvent {
  sequence: number;
  event_type: RunEventType;
  created_at: string;
  attempt_number: number | null;
  code: string | null;
  message: string | null;
  available_at: string | null;
}

const TIMESTAMP = "2026-01-01T00:00:00Z";

export function apiAgentInstance(overrides: Partial<ApiAgentInstance> = {}): ApiAgentInstance {
  return {
    id: 1,
    agent_key: "nervos.chat",
    agent_definition_version: "1",
    display_name: "Chat",
    enabled: true,
    model_provider: "anthropic",
    model_name: "opaque/model",
    created_at: TIMESTAMP,
    updated_at: TIMESTAMP,
    ...overrides,
  };
}

export function apiRun(overrides: Partial<ApiRun> = {}): ApiRun {
  return {
    id: 1,
    agent_instance_id: 1,
    agent_key: "nervos.chat",
    agent_definition_version: "1",
    model_provider: "anthropic",
    model_name: "opaque/model",
    input_text: "hello",
    status: "succeeded",
    created_at: TIMESTAMP,
    started_at: TIMESTAMP,
    finished_at: TIMESTAMP,
    output_text: "deterministic answer",
    finish_reason: "stop",
    error_code: null,
    error_message: null,
    usage: { input_tokens: 11, output_tokens: 3, total_tokens: null },
    elapsed_ms: 12,
    execution_phase: "succeeded",
    retry_available_at: null,
    ...overrides,
  };
}

export function apiRunEvent(overrides: Partial<ApiRunEvent> = {}): ApiRunEvent {
  return {
    sequence: 1,
    event_type: "run.created",
    created_at: TIMESTAMP,
    attempt_number: null,
    code: null,
    message: null,
    available_at: null,
    ...overrides,
  };
}

export function apiError(status: number, code: string, message: string) {
  return HttpResponse.json({ error: { code, message } }, { status });
}

export function setupStatusHandler(complete: boolean) {
  return http.get("/api/v1/setup/status", () => HttpResponse.json({ setup_complete: complete }));
}

export function authenticatedHandler() {
  return http.get("/api/v1/auth/me", () => HttpResponse.json(API_USER));
}

export async function renderRoute(path: string) {
  const result = renderWithRouter(appRoutes, { initialEntries: [path] });
  return { user: userEvent.setup(), ...result };
}
