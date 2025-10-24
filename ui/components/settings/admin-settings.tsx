'use client';

import * as React from 'react';
import { useState, useEffect } from 'react';
import { AdminService } from '@/lib/api/services/admin';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
  CardFooter,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { AlertCircle, Save } from 'lucide-react';
import { toast } from 'sonner';

export function AdminSettings() {
  const [settings, setSettings] = useState<string>('{}');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>('');

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    try {
      setLoading(true);
      setError('');
      const data = await AdminService.getSettings();
      setSettings(JSON.stringify(data, null, 2));
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load settings';
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      setError('');

      let payload: Record<string, unknown>;
      try {
        payload = JSON.parse(settings);
      } catch (e) {
        const message = 'Invalid JSON: ' + (e instanceof Error ? e.message : String(e));
        setError(message);
        toast.error(message);
        return;
      }

      ['upstream_api_key', 'admin_password', 'nsec'].forEach((k) => {
        if (payload && payload[k] === '[REDACTED]') {
          delete payload[k];
        }
      });

      const updatedData = await AdminService.updateSettings(payload);
      setSettings(JSON.stringify(updatedData, null, 2));
      toast.success('Settings saved successfully');
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to save settings';
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className='mb-6'>
        <h2 className='text-xl font-semibold tracking-tight'>
          Admin Settings
        </h2>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Server Configuration (JSON)</CardTitle>
          <CardDescription>
            Edit server settings in JSON format. Values shown as &quot;[REDACTED]&quot; will remain unchanged if left as-is.
          </CardDescription>
        </CardHeader>
        <CardContent className='space-y-4'>
          {loading ? (
            <div className='flex items-center justify-center py-8 text-muted-foreground'>
              Loading settings...
            </div>
          ) : (
            <>
              {error && (
                <Alert variant='destructive'>
                  <AlertCircle className='h-4 w-4' />
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}
              <textarea
                value={settings}
                onChange={(e) => setSettings(e.target.value)}
                className='w-full min-h-[400px] font-mono text-sm bg-muted p-4 rounded-md border focus:outline-none focus:ring-2 focus:ring-ring'
                placeholder='{}'
              />
            </>
          )}
        </CardContent>
        <CardFooter className='flex justify-between'>
          <Button
            variant='outline'
            onClick={loadSettings}
            disabled={loading || saving}
          >
            Reload
          </Button>
          <Button
            onClick={handleSave}
            disabled={loading || saving}
          >
            <Save className='mr-2 h-4 w-4' />
            {saving ? 'Saving...' : 'Save Settings'}
          </Button>
        </CardFooter>
      </Card>
    </>
  );
}

