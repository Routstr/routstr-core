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
  shouldContinue?: () => boolean;
  onProgress?: (progress: CertificationProgress | null) => void;
  onResults?: (results: ModelCertificationResult[]) => void;
}

export async function runProviderCertification({
  providerId,
  modelRuns,
  includeCache,
  shouldContinue = () => true,
  onProgress,
  onResults,
}: RunProviderCertificationOptions): Promise<ModelCertificationResult[]> {
  const completed: ModelCertificationResult[] = [];

  for (const [index, run] of modelRuns.entries()) {
    if (!shouldContinue()) break;
    onProgress?.({
      modelId: run.modelId,
      modelIndex: index + 1,
      modelTotal: modelRuns.length,
      pathCount: run.targets.length,
    });
    // Sequential on purpose: each run spends real upstream credits, and
    // parallel paths multiply that spend and the admin request load.
    const batch: ModelCertificationResult[] = [];
    for (const target of run.targets) {
      if (!shouldContinue()) break;
      const resultKey = `${providerId}::${run.modelId}::${target.path ?? 'default'}`;
      try {
        const report = await AdminService.certifyProvider(providerId, {
          model_id: run.modelId,
          model_path: target.path,
          check_cache: includeCache,
        });
        batch.push({
          resultKey,
          providerId,
          modelId: run.modelId,
          pathLabel: target.label,
          report,
        });
      } catch (error) {
        batch.push({
          resultKey,
          providerId,
          modelId: run.modelId,
          pathLabel: target.label,
          error: getErrorMessage(error),
        });
      }
    }
    if (!shouldContinue()) break;
    completed.push(...batch);
    onResults?.([...completed]);
  }

  if (shouldContinue()) onProgress?.(null);
  return completed;
}

export function useProviderCertificationRunner(providerId: number) {
  const [results, setResults] = useState<ModelCertificationResult[]>([]);
  const [progress, setProgress] = useState<CertificationProgress | null>(null);
  const [isPending, setIsPending] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(true);
  const active = useRef(false);

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
    // Already dispatched probes may still spend credit; keep them pending.
    setIsPending(active.current);
  }, []);

  const run = useCallback(
    async (modelRuns: ModelRun[], includeCache: boolean) => {
      if (active.current || !mounted.current) return [];
      active.current = true;
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
          shouldContinue: () =>
            mounted.current && generation.current === runGeneration,
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
        active.current = false;
        if (mounted.current) {
          setProgress(null);
          setIsPending(false);
        }
      }
    },
    [providerId]
  );

  return { results, progress, isPending, run, reset };
}
