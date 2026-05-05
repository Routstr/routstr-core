import { useQuery } from '@tanstack/react-query';
import type { WalletSnapshot } from '@/components/landing/key-info-details';

export function useWalletInfo(baseUrl: string, apiKey: string) {
  return useQuery({
    queryKey: ['walletInfo', baseUrl, apiKey],
    queryFn: async (): Promise<WalletSnapshot> => {
      const response = await fetch(`${baseUrl}/v1/balance/info`, {
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${apiKey}`,
        },
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || 'Unable to load wallet info');
      }

      const payload = await response.json();
      return {
        apiKey: payload.api_key || apiKey,
        balanceMsats: payload.balance ?? 0,
        reservedMsats: payload.reserved ?? 0,
        isChild: payload.is_child,
        parentKey: payload.parent_key,
        totalRequests: payload.total_requests,
        totalSpent: payload.total_spent,
        balanceLimit: payload.balance_limit,
        balanceLimitReset: payload.balance_limit_reset,
        validityDate: payload.validity_date,
        childKeys: payload.child_keys,
      };
    },
    enabled: !!baseUrl && !!apiKey,
    staleTime: 5000, // Consider data stale after 5 seconds
  });
}
