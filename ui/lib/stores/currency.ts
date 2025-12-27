import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { DisplayUnit } from '@/lib/types/units';

interface CurrencyState {
  displayUnit: DisplayUnit;
  setDisplayUnit: (unit: DisplayUnit) => void;
}

export const useCurrencyStore = create<CurrencyState>()(
  persist(
    (set) => ({
      displayUnit: 'sat',
      setDisplayUnit: (unit) => set({ displayUnit: unit }),
    }),
    {
      name: 'currency-storage',
    }
  )
);
