import axios, { AxiosRequestConfig, AxiosResponse, AxiosError } from 'axios';
import { ConfigurationService } from './services/configuration';

class ApiClient {
  private getBaseUrl(): string {
    return ConfigurationService.getLocalBaseUrl();
  }

  private getHeaders() {
    return ConfigurationService.getAuthHeaders();
  }

  private handleAuthError(error: unknown): void {
    if (axios.isAxiosError(error)) {
      const axiosError = error as AxiosError;
      if (
        axiosError.response?.status === 401 ||
        axiosError.response?.status === 403
      ) {
        ConfigurationService.clearToken();
        if (typeof window !== 'undefined') {
          window.location.href = '/login';
        }
      }
    }
  }

  async get<T>(
    endpoint: string,
    params?: Record<string, string | number | boolean | undefined>
  ): Promise<T> {
    const config: AxiosRequestConfig = {
      headers: this.getHeaders(),
      params,
      withCredentials: false,
    };

    try {
      console.log(`Making GET request to ${this.getBaseUrl()}${endpoint}`);
      const response: AxiosResponse<T> = await axios.get<T>(
        `${this.getBaseUrl()}${endpoint}`,
        config
      );
      return response.data;
    } catch (error) {
      this.handleAuthError(error);
      throw error;
    }
  }

  async post<T>(endpoint: string, data: Record<string, unknown>): Promise<T> {
    const config: AxiosRequestConfig = {
      headers: this.getHeaders(),
      withCredentials: false,
    };

    try {
      console.log(
        `Making POST request to ${this.getBaseUrl()}${endpoint}`,
        data
      );
      const response: AxiosResponse<T> = await axios.post<T>(
        `${this.getBaseUrl()}${endpoint}`,
        data,
        config
      );
      console.log(`POST response from ${endpoint}:`, {
        status: response.status,
        data: response.data,
        headers: response.headers,
      });
      return response.data;
    } catch (error) {
      this.handleAuthError(error);
      console.error(`Error posting to ${endpoint}:`, error);
      throw error;
    }
  }

  async put<T>(endpoint: string, data: Record<string, unknown>): Promise<T> {
    const config: AxiosRequestConfig = {
      headers: this.getHeaders(),
      withCredentials: false,
    };

    try {
      console.log(
        `Making PUT request to ${this.getBaseUrl()}${endpoint}`,
        data
      );
      const response: AxiosResponse<T> = await axios.put<T>(
        `${this.getBaseUrl()}${endpoint}`,
        data,
        config
      );
      return response.data;
    } catch (error) {
      this.handleAuthError(error);
      console.error(`Error updating ${endpoint}:`, error);
      throw error;
    }
  }

  async patch<T>(endpoint: string, data: Record<string, unknown>): Promise<T> {
    const config: AxiosRequestConfig = {
      headers: this.getHeaders(),
      withCredentials: false,
    };

    try {
      console.log(
        `Making PATCH request to ${this.getBaseUrl()}${endpoint}`,
        data
      );
      const response: AxiosResponse<T> = await axios.patch<T>(
        `${this.getBaseUrl()}${endpoint}`,
        data,
        config
      );
      return response.data;
    } catch (error) {
      this.handleAuthError(error);
      console.error(`Error patching ${endpoint}:`, error);
      throw error;
    }
  }

  async delete<T>(endpoint: string): Promise<T> {
    const config: AxiosRequestConfig = {
      headers: this.getHeaders(),
      withCredentials: false,
    };

    try {
      console.log(`Making DELETE request to ${this.getBaseUrl()}${endpoint}`);
      const response: AxiosResponse<T> = await axios.delete<T>(
        `${this.getBaseUrl()}${endpoint}`,
        config
      );
      return response.data;
    } catch (error) {
      this.handleAuthError(error);
      console.error(`Error deleting from ${endpoint}:`, error);
      throw error;
    }
  }
}

export const apiClient = new ApiClient();

export class ApiError extends Error {
  status: number;
  data?: unknown;

  constructor(message: string, status: number, data?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}
