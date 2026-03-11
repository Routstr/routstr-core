import { useState, useEffect, useCallback } from 'react';
import {
  ConfigurationService,
  ServerConfig,
} from '../api/services/configuration';
import { toast } from 'sonner';

/**
 * Hook for managing server configuration
 * Provides state and methods for working with the external server settings
 */
export function useConfiguration() {
  const [config, setConfig] = useState<ServerConfig>({
    endpoint: '',
    apiKey: '',
    enabled: false,
  });

  const [isLoaded, setIsLoaded] = useState(false);
  const [isSyncing, setIsSyncing] = useState(false);

  // Load configuration on initial mount
  useEffect(() => {
    const loadConfig = async () => {
      // First, load from localStorage
      const localConfig = ConfigurationService.getServerConfig();
      setConfig(localConfig);
      setIsLoaded(true);

      // Then try to load from backend and sync
      try {
        setIsSyncing(true);
        const backendConfig =
          await ConfigurationService.loadServerConfigFromBackend();
        if (backendConfig) {
          setConfig(backendConfig);
        }
      } catch (error) {
        console.error('Error loading configuration from backend:', error);
      } finally {
        setIsSyncing(false);
      }
    };

    loadConfig();
  }, []);

  // Save configuration to localStorage and backend
  const saveConfig = useCallback(async (newConfig: ServerConfig) => {
    // Save to localStorage
    ConfigurationService.saveServerConfig(newConfig);
    setConfig(newConfig);

    // Try to sync with backend
    try {
      setIsSyncing(true);
      const success =
        await ConfigurationService.saveServerConfigToBackend(newConfig);
      if (!success) {
        toast.warning(
          'Configuration saved locally, but failed to sync with server'
        );
      }
    } catch (error) {
      console.error('Error saving configuration to backend:', error);
      toast.error('Failed to sync configuration with server');
    } finally {
      setIsSyncing(false);
    }
  }, []);

  // Update a single field in the configuration
  const updateField = useCallback(
    (field: keyof ServerConfig, value: string | boolean) => {
      const newConfig = { ...config, [field]: value };
      saveConfig(newConfig);
    },
    [config, saveConfig]
  );

  // Toggle enabled state
  const toggleEnabled = useCallback(() => {
    const newConfig = { ...config, enabled: !config.enabled };
    saveConfig(newConfig);
  }, [config, saveConfig]);

  // Check if configuration is valid
  const isConfigValid = useCallback(() => {
    return ConfigurationService.isServerConfigValid();
  }, []);

  return {
    config,
    isLoaded,
    isSyncing,
    saveConfig,
    updateField,
    toggleEnabled,
    isConfigValid,
  };
}
