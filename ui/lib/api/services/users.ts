import { apiClient } from '../client';
import { User, CreateUser, UpdateUser } from '../schemas/users';

export class UserService {
  static async listUsers(organizationId?: string): Promise<User[]> {
    try {
      const params: Record<string, string | undefined> = {};
      if (organizationId) {
        params.organization_id = organizationId;
      }

      return await apiClient.get<User[]>('/api/users', params);
    } catch (error) {
      console.error('Error fetching users:', error);
      throw error;
    }
  }

  static async getUser(userId: string): Promise<User> {
    try {
      return await apiClient.get<User>(`/api/users/${userId}`);
    } catch (error) {
      console.error(`Error fetching user ${userId}:`, error);
      throw error;
    }
  }

  static async createUser(userData: CreateUser): Promise<User> {
    try {
      return await apiClient.post<User>(
        '/api/users',
        userData as Record<string, unknown>
      );
    } catch (error) {
      console.error('Error creating user:', error);
      throw error;
    }
  }

  static async updateUser(userId: string, userData: UpdateUser): Promise<User> {
    try {
      return await apiClient.put<User>(
        `/api/users/${userId}`,
        userData as Record<string, unknown>
      );
    } catch (error) {
      console.error(`Error updating user ${userId}:`, error);
      throw error;
    }
  }

  static async deleteUser(userId: string): Promise<void> {
    try {
      await apiClient.delete(`/api/users/${userId}`);
    } catch (error) {
      console.error(`Error deleting user ${userId}:`, error);
      throw error;
    }
  }
}
