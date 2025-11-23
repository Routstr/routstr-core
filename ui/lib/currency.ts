import type { DisplayUnit } from './types/units';

export type CashuUnit = 'sat' | 'msat';

export function convertToMsat(amount: number, unit: string): number {
  if (Number.isNaN(amount)) {
    return 0;
  }

  if (unit === 'sat') {
    return amount * 1000;
  }

  return amount;
}

export function formatFromMsat(
  amountMsat: number,
  displayUnit: DisplayUnit,
  usdPerSat: number | null
): string {
  if (displayUnit === 'msat') {
    return `${amountMsat.toLocaleString()} msat`;
  }

  if (displayUnit === 'sat') {
    const sats = amountMsat / 1000;
    // Format as integer for sats
    return `${Math.floor(sats).toLocaleString()} sats`;
  }

  if (usdPerSat === null) {
    return '—';
  }

  const sats = amountMsat / 1000;
  const usd = sats * usdPerSat;
  const formatter = new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return formatter.format(usd);
}
