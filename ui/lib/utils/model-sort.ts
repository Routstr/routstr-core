import { type Model } from '@/lib/api/schemas/models';

export function sortModelsByStatus(a: Model, b: Model): number {
  if (a.isEnabled && !b.isEnabled) return -1;
  if (!a.isEnabled && b.isEnabled) return 1;

  if (a.isEnabled === b.isEnabled) {
    if (!a.soft_deleted && b.soft_deleted) return -1;
    if (a.soft_deleted && !b.soft_deleted) return 1;
  }

  return 0;
}

export function sortModels(models: Model[]): Model[] {
  return [...models].sort(sortModelsByStatus);
}

export function groupAndSortModelsByProvider(
  models: Model[]
): Record<string, Model[]> {
  const grouped = models.reduce<Record<string, Model[]>>((acc, model) => {
    const provider = model.provider;
    if (!acc[provider]) {
      acc[provider] = [];
    }
    acc[provider].push(model);
    return acc;
  }, {});

  Object.keys(grouped).forEach((provider) => {
    grouped[provider].sort(sortModelsByStatus);
  });

  return grouped;
}
