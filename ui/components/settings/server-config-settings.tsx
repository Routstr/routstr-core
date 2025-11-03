'use client';

import * as React from 'react';
import { useState } from 'react';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { useConfiguration } from '@/lib/hooks/useConfiguration';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  CardFooter,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { CheckCircle, XCircle, AlertCircle } from 'lucide-react';
import { toast } from 'sonner';

export function ServerConfigSettings() {
  const { config, updateField, saveConfig, isSyncing } = useConfiguration();
  const [connectionStatus, setConnectionStatus] = useState<
    'idle' | 'testing' | 'success' | 'error'
  >('idle');

  const handleConfigChange = (
    field: 'endpoint' | 'apiKey' | 'enabled',
    value: string | boolean
  ) => {
    updateField(field, value);
    setConnectionStatus('idle');
  };

  const saveConfiguration = () => {
    try {
      saveConfig(config);
      toast.success('Server configuration saved successfully');
    } catch (error) {
      toast.error('Failed to save configuration');
      console.error('Configuration save error:', error);
    }
  };

  const testConnection = async () => {
    if (!config.endpoint) {
      return;
    }

    setConnectionStatus('testing');
    try {
      // Test connection using the ConfigurationService
      const isConnected = await ConfigurationService.testConnection(config);

      if (isConnected) {
        setConnectionStatus('success');
        toast.success('Connection successful!');
      } else {
        setConnectionStatus('error');
        toast.error('Connection failed. Please check your settings.');
      }
    } catch (error) {
      console.error('Connection test error:', error);
      setConnectionStatus('error');
      toast.error('Connection failed. Please check your settings.');
    }
  };

  const renderStatusBadge = () => {
    if (connectionStatus === 'idle') {
      return (
        <Badge variant='outline' className='flex items-center gap-1'>
          <AlertCircle className='h-4 w-4 text-yellow-500' />
          Not tested
        </Badge>
      );
    } else if (connectionStatus === 'testing') {
      return (
        <Badge variant='outline' className='flex items-center gap-1'>
          <AlertCircle className='h-4 w-4 animate-pulse text-blue-500' />
          Testing...
        </Badge>
      );
    } else if (connectionStatus === 'success') {
      return (
        <Badge variant='outline' className='flex items-center gap-1'>
          <CheckCircle className='h-4 w-4 text-green-500' />
          Connected
        </Badge>
      );
    } else {
      return (
        <Badge variant='outline' className='flex items-center gap-1'>
          <XCircle className='h-4 w-4 text-red-500' />
          Failed
        </Badge>
      );
    }
  };

  return (
    <>
      <div className='mb-6 flex items-center justify-between'>
        <h2 className='text-xl font-semibold tracking-tight'>
          External Server Configuration
        </h2>
        {isSyncing && (
          <Badge variant='outline' className='flex items-center gap-1'>
            <AlertCircle className='h-4 w-4 animate-spin text-blue-500' />
            Syncing...
          </Badge>
        )}
      </div>

      <Card>
        <CardHeader>
          <div className='flex items-center justify-between'>
            <div>
              <CardTitle>Request Forwarding Settings</CardTitle>
              <CardDescription>
                Configure external server endpoint and authentication for API
                request forwarding
              </CardDescription>
            </div>
            {config.enabled && renderStatusBadge()}
          </div>
        </CardHeader>
        <CardContent className='space-y-4'>
          <div className='space-y-2'>
            <Label htmlFor='server-endpoint'>Server Endpoint URL</Label>
            <div className='relative'>
              <Input
                id='server-endpoint'
                placeholder='https://ecash.routstr.info'
                value={config.endpoint}
                onChange={(e) => handleConfigChange('endpoint', e.target.value)}
                className='pr-24'
              />
              <Button
                type='button'
                variant='ghost'
                size='sm'
                className='absolute top-0 right-0 h-full px-3 py-2 text-xs'
                onClick={() =>
                  handleConfigChange('endpoint', 'https://ecash.routstr.info')
                }
              >
                Use Default
              </Button>
            </div>
          </div>
        </CardContent>
        <CardFooter className='flex justify-between'>
          <Button
            variant='outline'
            onClick={testConnection}
            disabled={!config.endpoint || connectionStatus === 'testing'}
          >
            Test Connection
          </Button>
          <Button onClick={saveConfiguration}>Save Configuration</Button>
        </CardFooter>
      </Card>
    </>
  );
}
