export type DisplayUnit = 'msat' | 'sat' | 'usd';

export const DISPLAY_UNITS: DisplayUnit[] = ['msat', 'sat', 'usd'];

export function getDisplayUnitLabel(unit: DisplayUnit): string {
  switch (unit) {
    case 'msat':
      return 'mSAT';
    case 'sat':
      return 'sat';
    case 'usd':
      return 'USD';
    default:
      return unit;
  }
}
