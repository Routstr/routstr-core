import { getApiErrorMessage } from '@/lib/api/errors';
import type {
  CertificationPath,
  CertificationStatus,
  ProviderCertification,
  ProviderModels,
} from '@/lib/api/services/admin';

export type ModelPathMode = 'default' | 'selected' | 'all';

export interface ProviderCertificationSetup {
  selectedModelIds: string[];
  pathModes: Record<string, ModelPathMode>;
  selectedModelPaths: Record<string, string[]>;
  checkCache: boolean;
}

export interface ModelPathTarget {
  path?: string;
  label: string;
}

export interface ModelRun {
  modelId: string;
  targets: ModelPathTarget[];
}

export interface CertificationProgress {
  modelId: string;
  modelIndex: number;
  modelTotal: number;
  pathCount: number;
}

export interface ModelCertificationResult {
  resultKey: string;
  providerId: number;
  modelId: string;
  pathLabel: string;
  report?: ProviderCertification;
  error?: string;
}

export const emptyCertificationSetup = (): ProviderCertificationSetup => ({
  selectedModelIds: [],
  pathModes: {},
  selectedModelPaths: {},
  checkCache: true,
});

export const getExactCertificationPaths = (
  models: ProviderModels | undefined,
  modelId: string
): CertificationPath[] =>
  (models?.certification_paths[modelId] ?? []).filter(
    (path) => path.endpoint_tag
  );

export const getCertificationPathLabel = (path: CertificationPath): string =>
  path.endpoint_name && path.endpoint_name !== path.endpoint_tag
    ? `${path.endpoint_name} (${path.endpoint_tag})`
    : path.endpoint_tag || 'Provider default';

export const getModelsNeedingPath = (
  setup: ProviderCertificationSetup
): string[] =>
  setup.selectedModelIds.filter(
    (modelId) =>
      setup.pathModes[modelId] === 'selected' &&
      (setup.selectedModelPaths[modelId]?.length ?? 0) === 0
  );

export const buildModelRuns = (
  setup: ProviderCertificationSetup,
  models: ProviderModels | undefined
): ModelRun[] =>
  setup.selectedModelIds.map((modelId) => {
    const paths = getExactCertificationPaths(models, modelId);
    const mode = setup.pathModes[modelId] ?? 'default';
    if (mode === 'all') {
      return {
        modelId,
        targets: paths.map((path) => ({
          path: path.path,
          label: getCertificationPathLabel(path),
        })),
      };
    }
    if (mode === 'selected') {
      const selected = new Set(setup.selectedModelPaths[modelId] ?? []);
      return {
        modelId,
        targets: paths
          .filter((path) => selected.has(path.path))
          .map((path) => ({
            path: path.path,
            label: getCertificationPathLabel(path),
          })),
      };
    }
    return { modelId, targets: [{ label: 'Provider default' }] };
  });

export const countCertificationTargets = (runs: ModelRun[]): number =>
  runs.reduce((total, run) => total + run.targets.length, 0);

export const getCertificationResultStatus = (
  result: ModelCertificationResult
): CertificationStatus | 'error' => {
  if (result.error || !result.report) return 'error';
  if (result.report.rows.some((row) => row.status === 'fail')) return 'fail';
  if (result.report.rows.some((row) => row.status === 'warn')) return 'warn';
  return 'ok';
};

export const getErrorMessage = (error: unknown): string =>
  getApiErrorMessage(error, 'Certification request failed');
