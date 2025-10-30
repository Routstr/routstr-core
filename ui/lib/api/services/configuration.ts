import { z } from 'zod';
import axios from 'axios';
// import { apiClient } from '../client';

// Schema for server configuration
export const ServerConfigSchema = z.object({
  endpoint: z.string().url().or(z.literal('')),
  apiKey: z.string(),
  enabled: z.boolean(),
});

export type ServerConfig = z.infer<typeof ServerConfigSchema>;

/**
 * Configuration service to manage external server settings
 */
export class ConfigurationService {
  private static readonly manualBaseUrlStorageKey = 'admin_manual_base_url';

  /**
   * Get the current server configuration from localStorage
   */
  static getServerConfig(): ServerConfig {
    if (typeof window === 'undefined') {
      return {
        endpoint: '',
        apiKey: '',
        enabled: false,
      };
    }

    return {
      endpoint: localStorage.getItem('server_endpoint') || '',
      apiKey: localStorage.getItem('server_api_key') || '',
      enabled: localStorage.getItem('server_enabled') === 'true',
    };
  }

  /**
   * Save server configuration to localStorage
   */
  static saveServerConfig(config: ServerConfig): void {
    if (typeof window === 'undefined') {
      return;
    }

    localStorage.setItem('server_endpoint', config.endpoint);
    localStorage.setItem('server_api_key', config.apiKey);
    localStorage.setItem('server_enabled', config.enabled.toString());
  }

  /**
   * Check if the server configuration is valid and enabled
   */
  static isServerConfigValid(): boolean {
    const config = this.getServerConfig();
    return config.enabled && !!config.endpoint && !!config.apiKey;
  }

  /**
   * Get the local base URL (never use external configuration)
   */
  static getLocalBaseUrl(): string {
    const envBaseUrl = process.env.NEXT_PUBLIC_API_URL;

    console.log(envBaseUrl);
    if (envBaseUrl && envBaseUrl.trim()) {
      return envBaseUrl;
    }

    if (typeof window !== 'undefined') {
      const manualBaseUrl = this.getManualBaseUrl();
      if (manualBaseUrl) {
        return manualBaseUrl;
      }

      if (window.location.origin) {
        return window.location.origin;
      }
    }

    if (process.env.NODE_ENV === 'production') {
      return '';
    }

    return 'http://127.0.0.1:8000';
  }

  static isEnvBaseUrlConfigured(): boolean {
    const envBaseUrl = process.env.NEXT_PUBLIC_API_URL ?? '';
    return envBaseUrl.trim().length > 0;
  }

  static getManualBaseUrl(): string {
    if (typeof window === 'undefined') {
      return '';
    }

    return localStorage.getItem(this.manualBaseUrlStorageKey) ?? '';
  }

  static setManualBaseUrl(baseUrl: string): void {
    if (typeof window === 'undefined') {
      return;
    }

    const normalizedBaseUrl = baseUrl.trim();
    if (normalizedBaseUrl) {
      localStorage.setItem(this.manualBaseUrlStorageKey, normalizedBaseUrl);
    } else {
      localStorage.removeItem(this.manualBaseUrlStorageKey);
    }
  }

  static isProduction(): boolean {
    return process.env.NODE_ENV === 'production';
  }

  /**
   * Get the base URL for API requests, using the external server if configured
   */
  static getBaseUrl(): string {
    const config = this.getServerConfig();
    if (config.enabled && config.endpoint) {
      return config.endpoint;
    }
    return this.getLocalBaseUrl();
  }

  /**
   * Get authorization headers for requests including API key if configured
   * This ensures the server knows which external service to forward to if needed
   */
  static getAuthHeaders(): Record<string, string> {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
    };

    try {
      if (typeof window !== 'undefined') {
        const adminToken = localStorage.getItem('admin_token');
        const tokenExpiry = localStorage.getItem('admin_token_expiry');

        if (adminToken && tokenExpiry) {
          const expiryTime = parseInt(tokenExpiry, 10);
          if (Date.now() < expiryTime) {
            headers['Authorization'] = `Bearer ${adminToken}`;
            return headers;
          } else {
            localStorage.removeItem('admin_token');
            localStorage.removeItem('admin_token_expiry');
          }
        }
      }
    } catch (error) {
      console.warn('Error accessing localStorage:', error);
    }

    const adminApiKey = process.env.NEXT_PUBLIC_ADMIN_API_KEY || '';
    if (adminApiKey) {
      headers['Authorization'] = `Bearer ${adminApiKey}`;
    }

    return headers;
  }

  static isTokenValid(): boolean {
    if (typeof window === 'undefined') {
      return false;
    }

    const adminToken = localStorage.getItem('admin_token');
    const tokenExpiry = localStorage.getItem('admin_token_expiry');

    if (!adminToken || !tokenExpiry) {
      return false;
    }

    const expiryTime = parseInt(tokenExpiry, 10);
    return Date.now() < expiryTime;
  }

  static clearToken(): void {
    if (typeof window === 'undefined') {
      return;
    }

    localStorage.removeItem('admin_token');
    localStorage.removeItem('admin_token_expiry');
  }

  static setToken(token: string, expiresIn: number): void {
    if (typeof window === 'undefined') {
      return;
    }

    const expiryTime = Date.now() + expiresIn * 1000;
    localStorage.setItem('admin_token', token);
    localStorage.setItem('admin_token_expiry', String(expiryTime));
  }

  static async testConnection(config: ServerConfig): Promise<boolean> {
    if (!config.endpoint) {
      return false;
    }

    try {
      const response = await axios.get(`${config.endpoint}/health`, {
        headers: {
          'Content-Type': 'application/json',
        },
        timeout: 1000,
      });

      return response.status === 200;
    } catch (error) {
      console.error('Server connection test failed:', error);
      return false;
    }
  }

  static async saveServerConfigToBackend(
    config: ServerConfig
  ): Promise<boolean> {
    if (typeof window === 'undefined') {
      return false;
    }

    try {
      // Save to localStorage first
      this.saveServerConfig(config);

      // Always use the local server URL for saving configuration
      const localBaseUrl = this.getLocalBaseUrl();

      // Convert from camelCase to snake_case for the backend
      const backendConfig = {
        endpoint: config.endpoint,
        api_key: config.apiKey,
      };

      // Then save to the backend server using the local URL
      await axios.post(`${localBaseUrl}/api/server-config`, backendConfig, {
        headers: {
          'Content-Type': 'application/json',
        },
      });

      return true;
    } catch (error) {
      console.error('Failed to save configuration to backend:', error);
      return false;
    }
  }

  /**
   * Load server configuration from the backend server
   */
  static async loadServerConfigFromBackend(): Promise<ServerConfig | null> {
    try {
      // Always use the local server URL for loading configuration
      const localBaseUrl = this.getLocalBaseUrl();

      const response = await axios.get<{
        endpoint: string;
        api_key: string;
      }>(`${localBaseUrl}/api/server-config`, {
        headers: {
          'Content-Type': 'application/json',
        },
      });

      // Transform the response to match our client-side schema
      if (response && response.data) {
        const config: ServerConfig = {
          endpoint: response.data.endpoint || '',
          apiKey: response.data.api_key || '',
          enabled: !!response.data.endpoint && !!response.data.api_key, // Enable if both fields are present
        };

        // Save it to localStorage
        this.saveServerConfig(config);

        return config;
      }

      return null;
    } catch (error) {
      console.error('Failed to load configuration from backend:', error);
      return null;
    }
  }

  /**
   * Request a new API key from the backend
   * @param reason The reason for requesting a new key
   * @returns The new API key
   */
  static async requestNewApiKey(reason: string): Promise<string> {
    try {
      // Always use the local server URL for requesting new API key
      const localBaseUrl = this.getLocalBaseUrl();

      const response = await axios.post<{ api_key: string }>(
        `${localBaseUrl}/api/server-config/new-key`,
        { reason },
        {
          headers: {
            'Content-Type': 'application/json',
            ...this.getAuthHeaders(),
          },
        }
      );

      if (response.data && response.data.api_key) {
        return response.data.api_key;
      }
      throw new Error('No API key received from server');
    } catch (error) {
      console.error('Failed to request new API key:', error);
      throw error;
    }
  }
}
