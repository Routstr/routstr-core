import { apiClient } from '../client';
import { ApiKey, CreateApiKey, UpdateApiKey } from '../schemas/api-keys';

// Commented out as it's not used
// const mockApiKeys: ApiKey[] = [
//   {
//     id: '1',
//     name: 'Production API Key',
//     key: 'sk_prod_' + crypto.randomBytes(16).toString('hex'),
//     user_id: '1',
//     organization_id: '1',
//     is_active: true,
//     created_at: new Date('2023-01-01').toISOString(),
//   },
//   {
//     id: '2',
//     name: 'Development API Key',
//     key: 'sk_dev_' + crypto.randomBytes(16).toString('hex'),
//     user_id: '2',
//     organization_id: '1',
//     is_active: true,
//     created_at: new Date('2023-02-15').toISOString(),
//   },
//   {
//     id: '3',
//     name: 'Testing API Key',
//     key: 'sk_test_' + crypto.randomBytes(16).toString('hex'),
//     user_id: '3',
//     organization_id: '1',
//     is_active: false,
//     created_at: new Date('2023-03-10').toISOString(),
//   },
// ];

export class ApiKeyService {
  static async listApiKeys(organizationId?: string): Promise<ApiKey[]> {
    try {
      const params: Record<string, string | undefined> = {};
      if (organizationId) {
        params.organization_id = organizationId;
      }

      return await apiClient.get<ApiKey[]>('/api/api-keys', params);
    } catch (error) {
      console.error('Error fetching API keys:', error);
      throw error;
    }
  }

  static async getApiKey(id: string): Promise<ApiKey> {
    try {
      return await apiClient.get<ApiKey>(`/api/api-keys/${id}`);
    } catch (error) {
      console.error(`Error fetching API key ${id}:`, error);
      throw error;
    }
  }

  static async createApiKey(data: CreateApiKey): Promise<ApiKey> {
    try {
      return await apiClient.post<ApiKey>(
        '/api/api-keys',
        data as Record<string, unknown>
      );
    } catch (error) {
      console.error('Error creating API key:', error);
      throw error;
    }
  }

  static async updateApiKey(id: string, data: UpdateApiKey): Promise<ApiKey> {
    try {
      return await apiClient.put<ApiKey>(
        `/api/api-keys/${id}`,
        data as Record<string, unknown>
      );
    } catch (error) {
      console.error(`Error updating API key ${id}:`, error);
      throw error;
    }
  }

  static async deleteApiKey(id: string): Promise<void> {
    try {
      await apiClient.delete(`/api/api-keys/${id}`);
    } catch (error) {
      console.error(`Error deleting API key ${id}:`, error);
      throw error;
    }
  }
}
