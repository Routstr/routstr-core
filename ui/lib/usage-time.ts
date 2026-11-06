export function parseBucketDate(value: string): Date | null {
  const normalized = value.includes('T')
    ? value
    : `${value.replace(' ', 'T')}Z`;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
