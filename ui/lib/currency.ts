import type { DisplayUnit } from './types/units';
import { formatCost } from './services/cost-validation';

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

function formatSatsFromUsd(amountUsd: number, usdPerSat: number): string {
  const sats = amountUsd / usdPerSat;

  if (sats > 0 && sats < 0.001) {
    return '<0.001 sats';
  }

  if (sats >= 1_000) {
    return `${new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(sats)} sats`;
  }

  const maximumFractionDigits = sats >= 1000 ? 0 : sats >= 1 ? 2 : 4;

  return `${sats.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits,
  })} sats`;
}

function formatMsatsFromUsd(amountUsd: number, usdPerSat: number): string {
  const msats = (amountUsd / usdPerSat) * 1000;

  if (msats > 0 && msats < 1) {
    return '<1 mSATs';
  }

  if (msats >= 1_000) {
    return `${new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
    }).format(msats)} mSATs`;
  }

  return `${Math.round(msats).toLocaleString()} mSATs`;
}

export function formatUsdAmountForDisplayUnit(
  amountUsd: number,
  displayUnit: DisplayUnit,
  usdPerSat: number | null
): string {
  if (Number.isNaN(amountUsd)) {
    return '—';
  }

  if (amountUsd === 0) {
    return 'Free';
  }

  if (displayUnit === 'usd' || usdPerSat === null || usdPerSat <= 0) {
    return formatCost(amountUsd);
  }

  if (displayUnit === 'msat') {
    return formatMsatsFromUsd(amountUsd, usdPerSat);
  }

  return formatSatsFromUsd(amountUsd, usdPerSat);
}
