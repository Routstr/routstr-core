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
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { AlertCircle, Save, RefreshCw, Eye, EyeOff } from 'lucide-react';
import { toast } from 'sonner';

interface SettingsData {
  name?: string;
  description?: string;
  npub?: string;
  nsec?: string;
  admin_password?: string;
  upstream_api_key?: string;
  http_url?: string;
  onion_url?: string;
  cashu_mints?: string[];
  [key: string]: unknown;
}

export function AdminSettings() {
  const [settings, setSettings] = useState<SettingsData>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>('');
  const [showSecrets, setShowSecrets] = useState(false);
  const [newMint, setNewMint] = useState('');

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    try {
      setLoading(true);
      setError('');
      const data = await AdminService.getSettings();
      setSettings(data as SettingsData);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to load settings';
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

      const updatedData = await AdminService.updateSettings(settings);
      setSettings(updatedData as SettingsData);
      toast.success('Settings saved successfully');
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to save settings';
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const handleInputChange = (field: string, value: string | boolean) => {
    setSettings((prev) => ({
      ...prev,
      [field]: value,
    }));
  };

  const addMint = () => {
    if (newMint.trim()) {
      setSettings((prev) => ({
        ...prev,
        cashu_mints: [...(prev.cashu_mints || []), newMint.trim()],
      }));
      setNewMint('');
    }
  };

  const removeMint = (index: number) => {
    setSettings((prev) => ({
      ...prev,
      cashu_mints: prev.cashu_mints?.filter((_, i) => i !== index) || [],
    }));
  };

  const renderSecretField = (
    field: string,
    label: string,
    placeholder?: string
  ) => {
    const value = (settings[field] as string) || '';
    const displayValue = showSecrets ? value : value ? '••••••••' : '';

    return (
      <div className='space-y-2'>
        <Label htmlFor={field}>{label}</Label>
        <div className='flex gap-2'>
          <Input
            id={field}
            type={showSecrets ? 'text' : 'password'}
            value={displayValue}
            onChange={(e) => handleInputChange(field, e.target.value)}
            placeholder={placeholder}
            className='flex-1'
          />
          <Button
            type='button'
            variant='outline'
            size='icon'
            onClick={() => setShowSecrets(!showSecrets)}
          >
            {showSecrets ? (
              <EyeOff className='h-4 w-4' />
            ) : (
              <Eye className='h-4 w-4' />
            )}
          </Button>
        </div>
      </div>
    );
  };

  if (loading) {
    return (
      <div className='flex items-center justify-center py-8'>
        <div className='text-muted-foreground'>Loading settings...</div>
      </div>
    );
  }

  return (
    <>
      <div className='mb-6'>
        <h2 className='text-xl font-semibold tracking-tight'>Admin Settings</h2>
        <p className='text-muted-foreground'>
          Configure your Routstr node settings
        </p>
      </div>

      {error && (
        <Alert variant='destructive' className='mb-6'>
          <AlertCircle className='h-4 w-4' />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className='space-y-6'>
        {/* Basic Information */}
        <Card>
          <CardHeader>
            <CardTitle>Basic Information</CardTitle>
            <CardDescription>
              Configure the basic node information and branding
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            <div className='space-y-2'>
              <Label htmlFor='name'>Node Name</Label>
              <Input
                id='name'
                value={settings.name || ''}
                onChange={(e) => handleInputChange('name', e.target.value)}
                placeholder='ARoutstrNode'
              />
            </div>
            <div className='space-y-2'>
              <Label htmlFor='description'>Description</Label>
              <Textarea
                id='description'
                value={settings.description || ''}
                onChange={(e) =>
                  handleInputChange('description', e.target.value)
                }
                placeholder='A Routstr Node'
                rows={3}
              />
            </div>
            <div className='space-y-2'>
              <Label htmlFor='http_url'>HTTP URL</Label>
              <Input
                id='http_url'
                value={settings.http_url || ''}
                onChange={(e) => handleInputChange('http_url', e.target.value)}
                placeholder='https://your-node.com'
              />
            </div>
            <div className='space-y-2'>
              <Label htmlFor='onion_url'>Onion URL (Optional)</Label>
              <Input
                id='onion_url'
                value={settings.onion_url || ''}
                onChange={(e) => handleInputChange('onion_url', e.target.value)}
                placeholder='http://your-node.onion'
              />
            </div>
          </CardContent>
        </Card>

        {/* Nostr Configuration */}
        <Card>
          <CardHeader>
            <CardTitle>Nostr Configuration</CardTitle>
            <CardDescription>
              Configure Nostr public and private keys
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            <div className='space-y-2'>
              <Label htmlFor='npub'>Public Key (npub)</Label>
              <Input
                id='npub'
                value={settings.npub || ''}
                onChange={(e) => handleInputChange('npub', e.target.value)}
                placeholder='npub1...'
              />
            </div>
            {renderSecretField('nsec', 'Private Key (nsec)', 'nsec1...')}
          </CardContent>
        </Card>

        {/* Security Settings */}
        <Card>
          <CardHeader>
            <CardTitle>Security Settings</CardTitle>
            <CardDescription>
              Configure authentication and API access
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            {renderSecretField(
              'admin_password',
              'Admin Password',
              'Enter admin password'
            )}
            {renderSecretField(
              'upstream_api_key',
              'Upstream API Key',
              'Enter API key'
            )}
          </CardContent>
        </Card>

        {/* Cashu Mints */}
        <Card>
          <CardHeader>
            <CardTitle>Cashu Mints</CardTitle>
            <CardDescription>
              Configure Cashu mint endpoints for payments
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            <div className='space-y-2'>
              <Label htmlFor='newMint'>Add Mint URL</Label>
              <div className='flex gap-2'>
                <Input
                  id='newMint'
                  value={newMint}
                  onChange={(e) => setNewMint(e.target.value)}
                  placeholder='https://mint.example.com'
                />
                <Button onClick={addMint} disabled={!newMint.trim()}>
                  Add Mint
                </Button>
              </div>
            </div>

            {settings.cashu_mints && settings.cashu_mints.length > 0 && (
              <div className='space-y-2'>
                <Label>Configured Mints</Label>
                <div className='space-y-2'>
                  {settings.cashu_mints.map((mint, index) => (
                    <div
                      key={index}
                      className='flex items-center gap-2 rounded border p-2'
                    >
                      <span className='flex-1 text-sm'>{mint}</span>
                      <Button
                        variant='outline'
                        size='sm'
                        onClick={() => removeMint(index)}
                      >
                        Remove
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className='mt-6'>
        <CardFooter className='flex justify-between'>
          <Button
            variant='outline'
            onClick={loadSettings}
            disabled={loading || saving}
          >
            <RefreshCw className='mr-2 h-4 w-4' />
            Reload
          </Button>
          <Button onClick={handleSave} disabled={loading || saving}>
            <Save className='mr-2 h-4 w-4' />
            {saving ? 'Saving...' : 'Save Settings'}
          </Button>
        </CardFooter>
      </Card>
    </>
  );
}
