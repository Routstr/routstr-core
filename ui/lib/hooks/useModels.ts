import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ModelService } from '../api/services/models';

// Query keys for models
const modelKeys = {
  all: ['models'] as const,
  list: () => [...modelKeys.all, 'list'] as const,
  detail: (id: string) => [...modelKeys.all, 'detail', id] as const,
  test: (id: string) => [...modelKeys.all, 'test', id] as const,
};

/**
 * Hook to fetch the list of available models
 */
export function useModels(options?: {
  team_id?: string;
  return_wildcard_routes?: boolean;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: modelKeys.list(),
    queryFn: () => ModelService.listModels(options),
    enabled: options?.enabled ?? true,
  });
}

/**
 * Hook to fetch detailed information about a specific model
 */
export function useModelInfo(modelId: string, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: modelKeys.detail(modelId),
    queryFn: () => ModelService.getModelInfo(modelId),
    enabled: !!modelId && (options?.enabled ?? true),
  });
}

export function usePrefetchModelInfo() {
  // This is only used for this function, not needed in the larger scope
  const queryClient = useQueryClient();

  return async (modelId: string) => {
    await queryClient.prefetchQuery({
      queryKey: modelKeys.detail(modelId),
      queryFn: () => ModelService.getModelInfo(modelId),
    });
  };
}
