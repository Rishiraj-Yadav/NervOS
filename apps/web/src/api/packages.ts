import { apiRequest } from "./client";
import { type AgentInstance } from "./agentInstances";

export type PackageStatus = "installing" | "installed" | "active" | "failed" | "pending_removal" | "removed";

export interface PackageInspection {
  package_id: string;
  package_version: string;
  display_name: string;
  manifest_version: string;
  signer_fingerprint: string;
  content_digest: string;
  archive_digest: string;
  min_nervos_version: string;
  max_nervos_version: string | null;
  is_compatible: boolean;
  entrypoint_module: string;
  entrypoint_object: string;
  tools_required: string[];
  tools_optional: string[];
  memory_declarations: string[];
  trigger_declarations: string[];
  config_schema: Record<string, unknown>;
  resource_limits: Record<string, number>;
}

export interface PackageVersionSummary {
  package_id: string;
  package_version: string;
  display_name: string;
  status: PackageStatus;
  signer_fingerprint: string;
  content_digest: string;
  archive_digest: string;
  bound_instances_count: number;
  installed_at: string | null;
  activated_at: string | null;
  failed_at: string | null;
  removed_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
}

export interface PackageVersionDetail {
  package_id: string;
  package_version: string;
  display_name: string;
  status: PackageStatus;
  signer_fingerprint: string;
  content_digest: string;
  archive_digest: string;
  manifest_version: string;
  min_nervos_version: string;
  max_nervos_version: string | null;
  is_compatible: boolean;
  entrypoint_module: string;
  entrypoint_object: string;
  tools_required: string[];
  tools_optional: string[];
  memory_declarations: string[];
  trigger_declarations: string[];
  config_schema: Record<string, unknown>;
  resource_limits: Record<string, number>;
  bound_instances_count: number;
  environment_id: number | null;
  environment_status: string | null;
  installed_at: string | null;
  activated_at: string | null;
  failed_at: string | null;
  removed_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
}

export interface PackageVersionPage {
  items: PackageVersionSummary[];
}

export interface PackageRemovalPlan {
  package_id: string;
  package_version: string;
  status: PackageStatus;
  bound_instance_ids: number[];
  bound_instances_count: number;
  nonterminal_run_ids: number[];
  nonterminal_runs_count: number;
  is_environment_shared: boolean;
  can_remove_immediately: boolean;
  can_begin_removal: boolean;
  blocking_reasons: string[];
}

export interface PackageRemovalResponse {
  package_id: string;
  package_version: string;
  outcome: string;
}

export interface InstallAuthorizationInput {
  package_id: string;
  package_version: string;
  content_digest: string;
  signer_fingerprint: string;
  archive_digest?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isPackageInspection(value: unknown): value is PackageInspection {
  return isRecord(value) && typeof value.package_id === "string" && typeof value.package_version === "string";
}

export function isPackageVersionSummary(value: unknown): value is PackageVersionSummary {
  return isRecord(value) && typeof value.package_id === "string" && typeof value.package_version === "string";
}

export function isPackageVersionPage(value: unknown): value is PackageVersionPage {
  return isRecord(value) && Array.isArray(value.items);
}

export function isPackageVersionDetail(value: unknown): value is PackageVersionDetail {
  return isRecord(value) && typeof value.package_id === "string" && typeof value.package_version === "string";
}

export function isPackageRemovalPlan(value: unknown): value is PackageRemovalPlan {
  return isRecord(value) && typeof value.package_id === "string" && Array.isArray(value.blocking_reasons);
}

export function isPackageRemovalResponse(value: unknown): value is PackageRemovalResponse {
  return isRecord(value) && typeof value.outcome === "string";
}

export function isAgentInstance(value: unknown): value is AgentInstance {
  return isRecord(value) && typeof value.id === "number" && typeof value.agent_key === "string";
}

export async function inspectPackage(file: File): Promise<PackageInspection> {
  const formData = new FormData();
  formData.append("file", file);
  return apiRequest("/packages/inspect", isPackageInspection, {
    method: "POST",
    formData,
  });
}

export async function installPackage(
  file: File,
  auth: InstallAuthorizationInput
): Promise<PackageVersionSummary> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("package_id", auth.package_id);
  formData.append("package_version", auth.package_version);
  formData.append("content_digest", auth.content_digest);
  formData.append("signer_fingerprint", auth.signer_fingerprint);
  if (auth.archive_digest) {
    formData.append("archive_digest", auth.archive_digest);
  }
  return apiRequest("/packages/install", isPackageVersionSummary, {
    method: "POST",
    formData,
  });
}

export async function listPackages(status?: string): Promise<PackageVersionPage> {
  const path = status ? `/packages?status=${encodeURIComponent(status)}` : "/packages";
  return apiRequest(path, isPackageVersionPage);
}

export async function getPackageDetail(
  packageId: string,
  version: string
): Promise<PackageVersionDetail> {
  return apiRequest(
    `/packages/${encodeURIComponent(packageId)}/versions/${encodeURIComponent(version)}`,
    isPackageVersionDetail
  );
}

export async function getRemovalPlan(
  packageId: string,
  version: string
): Promise<PackageRemovalPlan> {
  return apiRequest(
    `/packages/${encodeURIComponent(packageId)}/versions/${encodeURIComponent(version)}/removal-plan`,
    isPackageRemovalPlan
  );
}

export async function uninstallPackage(
  packageId: string,
  version: string
): Promise<PackageRemovalResponse> {
  return apiRequest(
    `/packages/${encodeURIComponent(packageId)}/versions/${encodeURIComponent(version)}`,
    isPackageRemovalResponse,
    { method: "DELETE" }
  );
}

export async function patchInstanceConfig(
  agentInstanceId: number,
  config: Record<string, unknown>,
  expectedConfigRevision: number
): Promise<AgentInstance> {
  return apiRequest(
    `/agent-instances/${agentInstanceId}/config`,
    isAgentInstance,
    {
      method: "PATCH",
      body: {
        config,
        expected_config_revision: expectedConfigRevision,
      },
    }
  );
}

export async function rebindInstance(
  agentInstanceId: number,
  targetPackageVersion: string,
  config: Record<string, unknown> | null,
  expectedConfigRevision: number
): Promise<AgentInstance> {
  return apiRequest(
    `/agent-instances/${agentInstanceId}/rebind`,
    isAgentInstance,
    {
      method: "POST",
      body: {
        target_package_version: targetPackageVersion,
        config: config ?? undefined,
        expected_config_revision: expectedConfigRevision,
      },
    }
  );
}
