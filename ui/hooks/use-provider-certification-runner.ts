'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { AdminService } from '@/lib/api/services/admin';
import type {
  CertificationProgress,
  ModelCertificationResult,
  ModelRun,
} from '@/lib/provider-certification';
import { getErrorMessage } from '@/lib/provider-certification';

interface RunProviderCertificationOptions {
  providerId: number;
  modelRuns: ModelRun[];
  includeCache: boolean;
  onProgress?: (progress: CertificationProgress | null) => void;
  onResults?: (results: ModelCertificationResult[]) => void;
}

export async function runProviderCertification({
  providerId,
  modelRuns,
  includeCache,
  onProgress,
  onResults,
}: RunProviderCertificationOptions): Promise<ModelCertificationResult[]> {
  const completed: ModelCertificationResult[] = [];

  for (const [index, run] of modelRuns.entries()) {
    onProgress?.({
      modelId: run.modelId,
      modelIndex: index + 1,
      modelTotal: modelRuns.length,
      pathCount: run.targets.length,
    });
    const batch = await Promise.all(
      run.targets.map(async (target): Promise<ModelCertificationResult> => {
        const resultKey = `${providerId}::${run.modelId}::${target.path ?? 'default'}`;
        try {
          const report = await AdminService.certifyProvider(providerId, {
            model_id: run.modelId,
            model_path: target.path,
            check_cache: includeCache,
          });
          return {
            resultKey,
            providerId,
            modelId: run.modelId,
            pathLabel: target.label,
            report,
          };
        } catch (error) {
          return {
            resultKey,
            providerId,
            modelId: run.modelId,
            pathLabel: target.label,
            error: getErrorMessage(error),
          };
        }
      })
    );
    completed.push(...batch);
    onResults?.([...completed]);
  }

  onProgress?.(null);
  return completed;
}

export function useProviderCertificationRunner(providerId: number) {
  const [results, setResults] = useState<ModelCertificationResult[]>([]);
  const [progress, setProgress] = useState<CertificationProgress | null>(null);
  const [isPending, setIsPending] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
    };
  }, []);

  const reset = useCallback(() => {
    generation.current += 1;
    setResults([]);
    setProgress(null);
    setIsPending(false);
  }, []);

  const run = useCallback(
    async (modelRuns: ModelRun[], includeCache: boolean) => {
      const runGeneration = generation.current + 1;
      generation.current = runGeneration;
      setResults([]);
      setProgress(null);
      setIsPending(true);
      try {
        return await runProviderCertification({
          providerId,
          modelRuns,
          includeCache,
          onProgress: (nextProgress) => {
            if (mounted.current && generation.current === runGeneration) {
              setProgress(nextProgress);
            }
          },
          onResults: (nextResults) => {
            if (mounted.current && generation.current === runGeneration) {
              setResults(nextResults);
            }
          },
        });
      } finally {
        if (mounted.current && generation.current === runGeneration) {
          setProgress(null);
          setIsPending(false);
        }
      }
    },
    [providerId]
  );

  return { results, progress, isPending, run, reset };
}
