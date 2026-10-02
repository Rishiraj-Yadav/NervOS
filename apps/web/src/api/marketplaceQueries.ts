import { useMutation, useQuery } from "@tanstack/react-query";
import { createMarketplaceInstallRequest, installMarketplaceRequest, marketplaceRelease, marketplaceVersions, prepareMarketplaceRequest, searchMarketplace } from "./marketplace";

export function marketplaceQuery(query: string) {
  return {
    queryKey: ["marketplace", query],
    queryFn: () => searchMarketplace(query),
  };
}

export function useMarketplace(query: string) {
  return useQuery(marketplaceQuery(query));
}

export function useMarketplaceInstall() {
  return useMutation({
    mutationFn: async ({ packageId, packageVersion, acknowledgement }: { packageId: string; packageVersion: string; acknowledgement?: string }) => {
      const request = await createMarketplaceInstallRequest(packageId, packageVersion, acknowledgement);
      return prepareMarketplaceRequest(request.id);
    },
  });
}

export function useMarketplaceVersions(packageId: string | null) {
  return useQuery({ queryKey: ["marketplace-versions", packageId], enabled: packageId !== null,
    queryFn: () => marketplaceVersions(packageId!), });
}

export function useMarketplaceRelease(packageId: string | null, version: string) {
  return useQuery({ queryKey: ["marketplace-release", packageId, version],
    enabled: packageId !== null && version !== "",
    queryFn: () => marketplaceRelease(packageId!, version), });
}

export function useMarketplaceApproval() {
  return useMutation({ mutationFn: installMarketplaceRequest });
}
