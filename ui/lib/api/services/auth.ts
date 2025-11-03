import { z } from 'zod';
import { apiClient } from '../client';
import { ConfigurationService } from './configuration';
import axios from 'axios';

export const loginSchema = z.object({
  username: z.string().optional(),
  password: z.string().optional(),
});

export type LoginRequest = z.infer<typeof loginSchema>;

export const loginResponseSchema = z.object({
  id: z.string(),
});

export type LoginResponse = z.infer<typeof loginResponseSchema>;

export async function login(data: LoginRequest): Promise<LoginResponse> {
  try {
    return await apiClient.post<LoginResponse>('/api/login', data);
  } catch (error) {
    console.error('Login error:', error);
    throw error;
  }
}

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

export const registerSchema = z.object({
  npub: z.string().min(10, { message: 'must have at least 10 character' }),
  name: z.string().optional(),
});

export type RegisterRequest = z.infer<typeof registerSchema>;
export type SchemaRegisterProps = z.infer<typeof registerSchema>;

export const registerResponseSchema = z.object({
  user_id: z.string(),
  theme: z.string(),
});

export type RegisterResponse = z.infer<typeof registerResponseSchema>;

export async function register(
  data: RegisterRequest
): Promise<RegisterResponse> {
  try {
    return await apiClient.post<RegisterResponse>('/api/register', data);
  } catch (error) {
    console.error('Registration error:', error);
    throw error;
  }
}

export const registerUser = register;

export async function getUserSettings(): Promise<{ id: string }> {
  try {
    return await apiClient.get<{ id: string }>('/api/user/settings');
  } catch (error) {
    console.error('Error fetching user settings:', error);
    throw error;
  }
}
