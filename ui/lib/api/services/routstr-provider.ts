import { apiClient } from '../client';

export class RoutstrProviderService {
  static async refundBalance(
    providerId: number
  ): Promise<{ ok: boolean; message: string; refund_id?: string }> {
    return await apiClient.post<{
      ok: boolean;
      message: string;
      refund_id?: string;
    }>(`/admin/api/upstream-providers/${providerId}/routstr/refund`, {});
  }
}
