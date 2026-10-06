import { apiRequest, apiRequestNoContent } from "./client";

export const securityKey = ["security"] as const;

const record = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const rows = (v: unknown, check: (row: unknown) => boolean): boolean => Array.isArray(v) && v.every(check);
const isTime = (v: unknown) => typeof v === "string" && !Number.isNaN(Date.parse(v));

export interface Secret {
  id: number;
  name: string;
  provider_hint: string | null;
  status: string;
  key_version: number;
  rotation_count: number;
  created_at: string;
  updated_at: string;
}

export interface AccountConnection {
  id: number;
  provider: string;
  display_name: string;
  secret_id: number;
  state: string;
  scopes: string[];
  expires_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ActionApproval {
  id: number;
  agent_instance_id: number;
  run_id: number;
  tool_sequence: number;
  upstream_name: string;
  fingerprint: string;
  input_digest: string;
  preview: Record<string, unknown>;
  state: string;
  requested_at: string;
  expires_at: string;
  decided_at: string | null;
  consumed_at: string | null;
}

export interface PublisherTrust {
  signer_fingerprint: string;
  state: string;
  reason: string | null;
  source: string;
  created_at: string;
  updated_at: string;
}

export interface SecretKeyVersion {
  key_version: number;
  active: boolean;
  created_at: string;
}

const isSecret = (v: unknown): v is Secret =>
  record(v) &&
  typeof v.id === "number" &&
  typeof v.name === "string" &&
  (v.provider_hint === null || typeof v.provider_hint === "string") &&
  typeof v.status === "string" &&
  typeof v.key_version === "number" &&
  typeof v.rotation_count === "number" &&
  isTime(v.created_at) &&
  isTime(v.updated_at);

const isAccountConnection = (v: unknown): v is AccountConnection =>
  record(v) &&
  typeof v.id === "number" &&
  typeof v.provider === "string" &&
  typeof v.display_name === "string" &&
  typeof v.secret_id === "number" &&
  typeof v.state === "string" &&
  rows(v.scopes, (s) => typeof s === "string") &&
  (v.expires_at === null || isTime(v.expires_at)) &&
  isTime(v.created_at) &&
  isTime(v.updated_at);

const isApproval = (v: unknown): v is ActionApproval =>
  record(v) &&
  typeof v.id === "number" &&
  typeof v.agent_instance_id === "number" &&
  typeof v.run_id === "number" &&
  typeof v.tool_sequence === "number" &&
  typeof v.upstream_name === "string" &&
  typeof v.fingerprint === "string" &&
  typeof v.input_digest === "string" &&
  record(v.preview) &&
  typeof v.state === "string" &&
  isTime(v.requested_at) &&
  isTime(v.expires_at) &&
  (v.decided_at === null || isTime(v.decided_at)) &&
  (v.consumed_at === null || isTime(v.consumed_at));

const isTrust = (v: unknown): v is PublisherTrust =>
  record(v) &&
  typeof v.signer_fingerprint === "string" &&
  typeof v.state === "string" &&
  (v.reason === null || typeof v.reason === "string") &&
  typeof v.source === "string" &&
  isTime(v.created_at) &&
  isTime(v.updated_at);

export const getSecrets = () =>
  apiRequest<Secret[]>("/secrets", (v): v is Secret[] => rows(v, isSecret));
export const createSecret = (body: { name: string; value: string; provider_hint?: string }) =>
  apiRequest<Secret>("/secrets", isSecret, { method: "POST", body });
export const replaceSecret = (id: number, value: string) =>
  apiRequest<Secret>(`/secrets/${id}/value`, isSecret, { method: "PUT", body: { value } });
export const setSecretStatus = (id: number, status: Secret["status"]) =>
  apiRequest<Secret>(`/secrets/${id}/status`, isSecret, { method: "PUT", body: { status } });
export const deleteSecret = (id: number) => apiRequestNoContent(`/secrets/${id}`, { method: "DELETE" });
export const getSecretKeys = () =>
  apiRequest<SecretKeyVersion[]>(
    "/secrets/keys",
    (v): v is SecretKeyVersion[] =>
      rows(v, (k) => record(k) && typeof k.key_version === "number" && typeof k.active === "boolean"),
  );

export const getAccountConnections = () =>
  apiRequest<AccountConnection[]>("/account-connections", (v): v is AccountConnection[] =>
    rows(v, isAccountConnection),
  );
export const createAccountConnection = (body: {
  provider: string;
  display_name: string;
  secret_id: number;
  scopes: string[];
}) => apiRequest<AccountConnection>("/account-connections", isAccountConnection, { method: "POST", body });
export const disconnectAccountConnection = (id: number) =>
  apiRequestNoContent(`/account-connections/${id}/disconnect`, { method: "POST" });

export interface AccountProvider { id: string; scopes: string[] }
export const getAccountProviders = () => apiRequest<AccountProvider[]>(
  "/account-oauth/providers",
  (v): v is AccountProvider[] => rows(v, (p) => record(p) && typeof p.id === "string" &&
    rows(p.scopes, (s) => typeof s === "string")),
);
export const startAccountAuthorization = (body: {
  provider: string; display_name: string; scopes: string[];
}) => apiRequest<{ authorization_url: string }>("/account-oauth/start",
  (v): v is { authorization_url: string } => {
    if (!record(v) || typeof v.authorization_url !== "string") return false;
    try {
      const url = new URL(v.authorization_url);
      return url.protocol === "https:" && !url.username && !url.password;
    } catch { return false; }
  }, { method: "POST", body },
);

export const getPendingApprovals = () =>
  apiRequest<ActionApproval[]>("/action-approvals", (v): v is ActionApproval[] => rows(v, isApproval));
export const decideApproval = (id: number, approve: boolean) =>
  apiRequest<ActionApproval>(`/action-approvals/${id}/decision`, isApproval, {
    method: "POST",
    body: { approve },
  });
export const cancelApproval = (id: number) =>
  apiRequest<ActionApproval>(`/action-approvals/${id}/cancel`, isApproval, { method: "POST" });

export const getPublisherTrust = () =>
  apiRequest<PublisherTrust[]>("/publisher-trust", (v): v is PublisherTrust[] => rows(v, isTrust));
export const setPublisherTrust = (
  signerFingerprint: string,
  body: { state: PublisherTrust["state"]; reason?: string },
) =>
  apiRequest<PublisherTrust>(
    `/publisher-trust/${encodeURIComponent(signerFingerprint)}`,
    isTrust,
    { method: "PUT", body },
  );
