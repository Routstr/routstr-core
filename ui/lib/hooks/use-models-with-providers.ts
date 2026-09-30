'use client';

import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AdminService } from '@/lib/api/services/admin';

export const modelsWithProvidersQueryKey = ['models-with-providers'] as const;
const localModelsQueryKey = ['models-with-providers', 'local'] as const;

/**
 * Shared catalog read for every models view.
 *
 * The database rows come back without touching an upstream, so they render
 * first; the listing that needs live provider calls replaces them once it
 * lands. Both queries live under one key prefix, so a single
 * `invalidateQueries(['models-with-providers'])` still refreshes the pair, and
 * every consumer of this hook shares one request instead of fanning out again.
 */
export function useModelsWithProviders() {
  const queryClient = useQueryClient();

  const localQuery = useQuery({
    queryKey: localModelsQueryKey,
    queryFn: () =>
      AdminService.getModelsWithProviders({ includeRemote: false }),
    refetchOnWindowFocus: false,
    staleTime: 30_000,
  });

  const fullQuery = useQuery({
    queryKey: modelsWithProvidersQueryKey,
    queryFn: () => AdminService.getModelsWithProviders(),
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const data = fullQuery.data ?? localQuery.data;

  return {
    models: data?.models ?? [],
    groups: data?.groups ?? [],
    isLoading: !data && (localQuery.isLoading || fullQuery.isLoading),
    isFetchingRemote: fullQuery.isFetching,
    error: data ? null : (fullQuery.error ?? localQuery.error),
    refetch: async () => {
      await queryClient.invalidateQueries({
        queryKey: modelsWithProvidersQueryKey,
      });
    },
  };
}
