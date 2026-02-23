'use client';

import * as React from 'react';
import { useState } from 'react';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { useConfiguration } from '@/lib/hooks/use-configuration';
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
import { CheckCircle, XCircle, AlertCircle, Loader2 } from 'lucide-react';
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
        <Badge variant='secondary' className='flex items-center gap-1'>
          <AlertCircle className='h-4 w-4' />
          Not tested
        </Badge>
      );
    } else if (connectionStatus === 'testing') {
      return (
        <Badge variant='outline' className='flex items-center gap-1'>
          <Loader2 className='h-4 w-4 animate-spin' />
          Testing...
        </Badge>
      );
    } else if (connectionStatus === 'success') {
      return (
        <Badge variant='default' className='flex items-center gap-1'>
          <CheckCircle className='h-4 w-4' />
          Connected
        </Badge>
      );
    } else {
      return (
        <Badge variant='destructive' className='flex items-center gap-1'>
          <XCircle className='h-4 w-4' />
          Failed
        </Badge>
      );
    }
  };

  return (
    <>
      <Card>
        <CardHeader>
          <div className='flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between'>
            <div className='min-w-0'>
              <CardTitle>Request Forwarding Settings</CardTitle>
              <CardDescription>
                Configure external server endpoint and authentication for API
                request forwarding
              </CardDescription>
            </div>
            <div className='flex items-center gap-2'>
              {isSyncing && (
                <Badge variant='outline' className='flex items-center gap-1'>
                  <Loader2 className='h-4 w-4 animate-spin' />
                  Syncing...
                </Badge>
              )}
              {config.enabled && renderStatusBadge()}
            </div>
          </div>
        </CardHeader>
        <CardContent className='space-y-4'>
          <div className='space-y-2'>
            <Label htmlFor='server-endpoint'>Server Endpoint URL</Label>
            <div className='relative flex flex-col gap-2 sm:block'>
              <Input
                id='server-endpoint'
                placeholder='https://ecash.routstr.info'
                value={config.endpoint}
                onChange={(e) => handleConfigChange('endpoint', e.target.value)}
                className='sm:pr-24'
              />
              <Button
                type='button'
                variant='ghost'
                size='sm'
                className='h-8 w-full px-3 py-2 text-xs sm:absolute sm:top-0 sm:right-0 sm:h-full sm:w-auto'
                onClick={() =>
                  handleConfigChange('endpoint', 'https://ecash.routstr.info')
                }
              >
                Use Default
              </Button>
            </div>
          </div>
        </CardContent>
        <CardFooter className='flex flex-col gap-2 sm:flex-row sm:justify-between'>
          <Button
            variant='outline'
            onClick={testConnection}
            disabled={!config.endpoint || connectionStatus === 'testing'}
            className='w-full sm:w-auto'
          >
            Test Connection
          </Button>
          <Button onClick={saveConfiguration} className='w-full sm:w-auto'>
            Save Configuration
          </Button>
        </CardFooter>
      </Card>
    </>
  );
}
