export interface ParsedVersion {
  raw: string;
  base: string;
  parts: readonly number[];
  commit: string | null;
}

export type StatusKind =
  | 'unknown'
  | 'outdated'
  | 'ahead'
  | 'commit-drift'
  | 'current';

export function parseVersion(
  raw: string | undefined | null
): ParsedVersion | null {
  if (!raw) return null;
  const trimmed = raw.trim().replace(/^v/i, '');
  if (!trimmed) return null;
  const [base, commitPart] = trimmed.split('+', 2);
  const parts = (base ?? '')
    .split('.')
    .map((segment) => Number.parseInt(segment, 10))
    .filter((value) => Number.isFinite(value));
  if (parts.length === 0) return null;
  return {
    raw,
    base: base ?? '',
    parts,
    commit: commitPart ?? null,
  };
}

export function compareVersionParts(
  a: readonly number[],
  b: readonly number[]
): number {
  const length = Math.max(a.length, b.length);
  for (let i = 0; i < length; i += 1) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

export function deriveStatus(
  current: ParsedVersion | null,
  latest: ParsedVersion | null
): StatusKind {
  if (!current || !latest) return 'unknown';
  const cmp = compareVersionParts(current.parts, latest.parts);
  if (cmp < 0) return 'outdated';
  if (cmp > 0) return 'ahead';
  return current.commit ? 'commit-drift' : 'current';
}

export function formatReleaseDate(iso: string | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

export function formatVersionLabel(version: ParsedVersion | null): string {
  if (!version) return 'unknown';
  return version.raw.startsWith('v') ? version.raw : `v${version.raw}`;
}
