import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  inspectPackage,
  installPackage,
  listPackages,
  getPackageDetail,
  getRemovalPlan,
  uninstallPackage,
  patchInstanceConfig,
  rebindInstance,
  type InstallAuthorizationInput,
} from "./packages";

export function packagesQuery(status?: string) {
  return {
    queryKey: status ? ["packages", { status }] : ["packages"],
    queryFn: () => listPackages(status),
  };
}

export function packageDetailQuery(packageId: string, version: string) {
  return {
    queryKey: ["package", packageId, version],
    queryFn: () => getPackageDetail(packageId, version),
  };
}

export function packageRemovalPlanQuery(packageId: string, version: string) {
  return {
    queryKey: ["package-removal-plan", packageId, version],
    queryFn: () => getRemovalPlan(packageId, version),
  };
}

export function useInspectPackage() {
  return useMutation({
    mutationFn: (file: File) => inspectPackage(file),
  });
}

export function useInstallPackage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ file, auth }: { file: File; auth: InstallAuthorizationInput }) =>
      installPackage(file, auth),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["packages"] });
    },
  });
}

export function useUninstallPackage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ packageId, version }: { packageId: string; version: string }) =>
      uninstallPackage(packageId, version),
    onSuccess: (_data, { packageId, version }) => {
      void queryClient.invalidateQueries({ queryKey: ["packages"] });
      void queryClient.invalidateQueries({ queryKey: ["package", packageId, version] });
      void queryClient.invalidateQueries({ queryKey: ["package-removal-plan", packageId, version] });
    },
  });
}

export function usePatchInstanceConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentInstanceId,
      config,
      expectedConfigRevision,
    }: {
      agentInstanceId: number;
      config: Record<string, unknown>;
      expectedConfigRevision: number;
    }) => patchInstanceConfig(agentInstanceId, config, expectedConfigRevision),
    onSuccess: (_data, { agentInstanceId }) => {
      void queryClient.invalidateQueries({ queryKey: ["agent-instance", agentInstanceId] });
      void queryClient.invalidateQueries({ queryKey: ["agent-instances"] });
    },
  });
}

export function useRebindInstance() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentInstanceId,
      targetPackageVersion,
      config,
      expectedConfigRevision,
    }: {
      agentInstanceId: number;
      targetPackageVersion: string;
      config: Record<string, unknown> | null;
      expectedConfigRevision: number;
    }) => rebindInstance(agentInstanceId, targetPackageVersion, config, expectedConfigRevision),
    onSuccess: (_data, { agentInstanceId }) => {
      void queryClient.invalidateQueries({ queryKey: ["agent-instance", agentInstanceId] });
      void queryClient.invalidateQueries({ queryKey: ["agent-instances"] });
      void queryClient.invalidateQueries({ queryKey: ["packages"] });
    },
  });
}
