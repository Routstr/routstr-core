import { apiClient } from '../client';

export interface RoutstrRefundResult {
  ok: boolean;
  message: string;
  // The upstream key held nothing (or only sub-sat dust) to refund.
  no_balance?: boolean;
  // Set when the upstream paid out but the local wallet could not receive it.
  token?: string;
}

export class RoutstrProviderService {
  static async refundBalance(providerId: number): Promise<RoutstrRefundResult> {
    return await apiClient.post<RoutstrRefundResult>(
      `/admin/api/upstream-providers/${providerId}/routstr/refund`,
      {}
    );
  }
}
