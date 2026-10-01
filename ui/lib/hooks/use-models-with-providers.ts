'use client';

import { useQuery } from '@tanstack/react-query';
import { AdminService } from '@/lib/api/services/admin';

export const modelsWithProvidersQueryKey = ['models-with-providers'] as const;

/**
 * Shared catalog read for every models view, so the page shell and the
 * selector panel share one request instead of each fanning out to providers.
 */
export function useModelsWithProviders() {
  const query = useQuery({
    queryKey: modelsWithProvidersQueryKey,
    queryFn: () => AdminService.getModelsWithProviders(),
    refetchOnWindowFocus: false,
  });

  return {
    models: query.data?.models ?? [],
    groups: query.data?.groups ?? [],
    isLoading: query.isLoading,
    error: query.error,
    refetch: query.refetch,
  };
}
