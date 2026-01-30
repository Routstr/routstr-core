import { z } from 'zod';
import { apiClient } from '../client';
import { ConfigurationService } from './configuration';
import axios from 'axios';



export const adminLoginSchema = z.object({
  password: z.string().min(1, 'Password is required'),
});

export type AdminLoginRequest = z.infer<typeof adminLoginSchema>;

export const adminLoginResponseSchema = z.object({
  ok: z.boolean(),
  token: z.string(),
  expires_in: z.number(),
});

export type AdminLoginResponse = z.infer<typeof adminLoginResponseSchema>;

export async function adminLogin(
  password: string
): Promise<AdminLoginResponse> {
  try {
    const baseUrl = ConfigurationService.getLocalBaseUrl();
    const response = await axios.post<AdminLoginResponse>(
      `${baseUrl}/admin/api/login`,
      { password },
      {
        headers: { 'Content-Type': 'application/json' },
      }
    );

    if (response.data.token && response.data.expires_in) {
      ConfigurationService.setToken(
        response.data.token,
        response.data.expires_in
      );
    }

    return response.data;
  } catch (error) {
    console.error('Admin login error:', error);
    throw error;
  }
}

export async function adminLogout(): Promise<void> {
  try {
    const token =
      typeof window !== 'undefined'
        ? localStorage.getItem('admin_token')
        : null;
    if (token) {
      const baseUrl = ConfigurationService.getLocalBaseUrl();
      await axios.post(
        `${baseUrl}/admin/api/logout`,
        {},
        {
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
        }
      );
    }
  } catch (error) {
    console.error('Admin logout error:', error);
  } finally {
    ConfigurationService.clearToken();
  }
}


