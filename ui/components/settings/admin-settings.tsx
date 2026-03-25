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
import { Skeleton } from '@/components/ui/skeleton';
import { AlertCircle, Eye, EyeOff } from 'lucide-react';
import { toast } from 'sonner';
import { Switch } from '@/components/ui/switch';

interface SettingsData {
  name?: string;
  description?: string;
  npub?: string;
  nsec?: string;
  enable_analytics_sharing?: boolean;
  upstream_api_key?: string;
  http_url?: string;
  onion_url?: string;
  cashu_mints?: string[];
  relays?: string[];
  [key: string]: unknown;
}

const HANDLED_KEYS = [
  'name',
  'description',
  'http_url',
  'onion_url',
  'npub',
  'nsec',
  'cashu_mints',
  'relays',
  'enable_analytics_sharing',
  'admin_password',
  'id',
  'updated_at',
];

const IGNORED_KEYS = [
  'upstream_base_url',
  'upstream_api_key',
  'upstream_provider_fee',
  'exchange_fee',
  'models_path',
];

interface PasswordData {
  current_password: string;
  new_password: string;
  confirm_password: string;
}

export function AdminSettings() {
  const [settings, setSettings] = useState<SettingsData>({});
  const [initialSettings, setInitialSettings] = useState<SettingsData>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>('');
  const [showSecrets, setShowSecrets] = useState(false);
  const [newMint, setNewMint] = useState('');
  const [newRelay, setNewRelay] = useState('');
  const [passwordData, setPasswordData] = useState<PasswordData>({
    current_password: '',
    new_password: '',
    confirm_password: '',
  });
  const [passwordError, setPasswordError] = useState<string>('');
  const [passwordSaving, setPasswordSaving] = useState(false);

  useEffect(() => {
    loadSettings();
  }, []);

  const loadSettings = async () => {
    try {
      setLoading(true);
      setError('');
      const data = await AdminService.getSettings();
      setSettings(data as SettingsData);
      setInitialSettings(data as SettingsData);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to load settings';
      setError(message);
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
      setInitialSettings(updatedData as SettingsData);
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

  const handlePasswordUpdate = async () => {
    try {
      setPasswordSaving(true);
      setPasswordError('');

      if (passwordData.new_password !== passwordData.confirm_password) {
        setPasswordError('New passwords do not match');
        return;
      }

      if (passwordData.new_password.length < 6) {
        setPasswordError('New password must be at least 6 characters');
        return;
      }

      const { apiClient } = await import('@/lib/api/client');

      await apiClient.patch('/admin/api/password', {
        current_password: passwordData.current_password,
        new_password: passwordData.new_password,
      });

      setPasswordData({
        current_password: '',
        new_password: '',
        confirm_password: '',
      });

      toast.success('Password updated successfully');
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to update password';
      setPasswordError(message);
      toast.error(message);
    } finally {
      setPasswordSaving(false);
    }
  };

  const clearPasswordForm = () => {
    setPasswordData({
      current_password: '',
      new_password: '',
      confirm_password: '',
    });
    setPasswordError('');
  };

  const handleInputChange = (field: string, value: unknown) => {
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

  const addRelay = () => {
    if (newRelay.trim()) {
      setSettings((prev) => ({
        ...prev,
        relays: [...(prev.relays || []), newRelay.trim()],
      }));
      setNewRelay('');
    }
  };

  const removeRelay = (index: number) => {
    setSettings((prev) => ({
      ...prev,
      relays: prev.relays?.filter((_, i) => i !== index) || [],
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
      <div key={field} className='space-y-2'>
        <Label htmlFor={field}>{label}</Label>
        <div className='flex flex-col gap-2 sm:flex-row'>
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

  const renderDynamicField = (key: string, value: unknown) => {
    const label = key
      .split('_')
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');

    if (typeof value === 'boolean') {
      return (
        <div
          key={key}
          className='flex items-center justify-between space-y-0 py-4'
        >
          <Label htmlFor={key}>{label}</Label>
          <Switch
            id={key}
            checked={value}
            onCheckedChange={(checked) => handleInputChange(key, checked)}
          />
        </div>
      );
    }

    if (typeof value === 'number') {
      return (
        <div key={key} className='space-y-2'>
          <Label htmlFor={key}>{label}</Label>
          <Input
            id={key}
            type='number'
            value={value}
            onChange={(e) => {
              const val = e.target.value === '' ? 0 : Number(e.target.value);
              handleInputChange(key, val);
            }}
          />
        </div>
      );
    }

    if (Array.isArray(value)) {
      const strValue = value.join(', ');
      return (
        <div key={key} className='space-y-2'>
          <Label htmlFor={key}>{label}</Label>
          <Textarea
            id={key}
            value={strValue}
            onChange={(e) => {
              const arr = e.target.value
                .split(',')
                .map((s) => s.trim())
                .filter((s) => s !== '');
              handleInputChange(key, arr);
            }}
            placeholder='Comma separated values'
            rows={2}
          />
        </div>
      );
    }

    const isSecret =
      key.includes('key') ||
      key.includes('password') ||
      key.includes('secret') ||
      key.includes('nsec');

    if (isSecret) {
      return renderSecretField(key, label);
    }

    return (
      <div key={key} className='space-y-2'>
        <Label htmlFor={key}>{label}</Label>
        <Input
          id={key}
          value={(value as string) || ''}
          onChange={(e) => handleInputChange(key, e.target.value)}
        />
      </div>
    );
  };

  const normalizeForCompare = (value: unknown): unknown => {
    if (value === null || value === undefined) {
      return '';
    }

    if (Array.isArray(value)) {
      return value;
    }

    if (typeof value === 'object') {
      return JSON.stringify(value);
    }

    return value;
  };

  const areValuesEqual = (a: unknown, b: unknown): boolean => {
    const normalizedA = normalizeForCompare(a);
    const normalizedB = normalizeForCompare(b);

    if (Array.isArray(normalizedA) && Array.isArray(normalizedB)) {
      return JSON.stringify(normalizedA) === JSON.stringify(normalizedB);
    }

    return normalizedA === normalizedB;
  };

  const hasFieldChanged = (key: string): boolean =>
    !areValuesEqual(settings[key], initialSettings[key]);

  const basicInfoChanged = [
    'name',
    'description',
    'http_url',
    'onion_url',
  ].some(hasFieldChanged);
  const nostrChanged = ['npub', 'nsec'].some(hasFieldChanged);
  const cashuMintsChanged = hasFieldChanged('cashu_mints');
  const relaysChanged = hasFieldChanged('relays');
  const analyticsSharingChanged = hasFieldChanged('enable_analytics_sharing');
  const advancedKeys = Object.keys(settings).filter(
    (key) => !HANDLED_KEYS.includes(key) && !IGNORED_KEYS.includes(key)
  );
  const advancedChanged = advancedKeys.some(hasFieldChanged);
  const hasPasswordChanges = Boolean(
    passwordData.current_password ||
    passwordData.new_password ||
    passwordData.confirm_password
  );

  const resetFields = (keys: string[]) => {
    setSettings((prev) => {
      const next = { ...prev };
      keys.forEach((key) => {
        next[key] = initialSettings[key];
      });
      return next;
    });
  };

  const resetBasicInfo = () =>
    resetFields(['name', 'description', 'http_url', 'onion_url']);
  const resetNostr = () => resetFields(['npub', 'nsec']);
  const resetCashuMints = () => {
    resetFields(['cashu_mints']);
    setNewMint('');
  };
  const resetRelays = () => {
    resetFields(['relays']);
    setNewRelay('');
  };
  const resetAnalyticsSharing = () => resetFields(['enable_analytics_sharing']);
  const resetAdvanced = () => resetFields(advancedKeys);

  if (loading) {
    return (
      <div className='space-y-4'>
        <div className='space-y-2'>
          <Skeleton className='h-7 w-48' />
          <Skeleton className='h-4 w-64 max-w-full' />
        </div>
        <Card>
          <CardHeader className='space-y-2'>
            <Skeleton className='h-5 w-40' />
            <Skeleton className='h-4 w-72 max-w-full' />
          </CardHeader>
          <CardContent className='space-y-3'>
            <Skeleton className='h-10 w-full' />
            <Skeleton className='h-20 w-full' />
            <Skeleton className='h-10 w-full' />
          </CardContent>
        </Card>
        <Card>
          <CardHeader className='space-y-2'>
            <Skeleton className='h-5 w-36' />
            <Skeleton className='h-4 w-64 max-w-full' />
          </CardHeader>
          <CardContent className='space-y-3'>
            <Skeleton className='h-10 w-full' />
            <Skeleton className='h-10 w-full' />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <>
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
          {basicInfoChanged ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={resetBasicInfo}
                  disabled={loading || saving}
                >
                  Cancel
                </Button>
                <Button onClick={handleSave} disabled={loading || saving}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
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
          {nostrChanged ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={resetNostr}
                  disabled={loading || saving}
                >
                  Cancel
                </Button>
                <Button onClick={handleSave} disabled={loading || saving}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
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
              <div className='flex flex-col gap-2 sm:flex-row'>
                <Input
                  id='newMint'
                  value={newMint}
                  onChange={(e) => setNewMint(e.target.value)}
                  placeholder='https://mint.example.com'
                />
                <Button
                  onClick={addMint}
                  disabled={!newMint.trim()}
                  className='sm:w-auto'
                >
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
                      className='flex flex-col gap-2 rounded border p-2 sm:flex-row sm:items-center'
                    >
                      <span className='flex-1 text-sm'>{mint}</span>
                      <Button
                        variant='outline'
                        size='sm'
                        onClick={() => removeMint(index)}
                        className='w-full sm:w-auto'
                      >
                        Remove
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
          {cashuMintsChanged ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={resetCashuMints}
                  disabled={loading || saving}
                >
                  Cancel
                </Button>
                <Button onClick={handleSave} disabled={loading || saving}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
        </Card>

        {/* Relays */}
        <Card>
          <CardHeader>
            <CardTitle>Nostr Relays</CardTitle>
            <CardDescription>
              Configure Nostr relays for communication
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            <div className='space-y-2'>
              <Label htmlFor='newRelay'>Add Relay URL</Label>
              <div className='flex flex-col gap-2 sm:flex-row'>
                <Input
                  id='newRelay'
                  value={newRelay}
                  onChange={(e) => setNewRelay(e.target.value)}
                  placeholder='wss://relay.example.com'
                />
                <Button
                  onClick={addRelay}
                  disabled={!newRelay.trim()}
                  className='sm:w-auto'
                >
                  Add Relay
                </Button>
              </div>
            </div>

            {settings.relays && settings.relays.length > 0 && (
              <div className='space-y-2'>
                <Label>Configured Relays</Label>
                <div className='space-y-2'>
                  {settings.relays.map((relay, index) => (
                    <div
                      key={index}
                      className='flex flex-col gap-2 rounded border p-2 sm:flex-row sm:items-center'
                    >
                      <span className='flex-1 text-sm'>{relay}</span>
                      <Button
                        variant='outline'
                        size='sm'
                        onClick={() => removeRelay(index)}
                        className='w-full sm:w-auto'
                      >
                        Remove
                      </Button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
          {relaysChanged ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={resetRelays}
                  disabled={loading || saving}
                >
                  Cancel
                </Button>
                <Button onClick={handleSave} disabled={loading || saving}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
        </Card>

        {/* Analytics Sharing */}
        <Card>
          <CardHeader>
            <CardTitle>Analytics Sharing</CardTitle>
            <CardDescription>
              Publish aggregate usage stats to Nostr for external dashboards
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className='flex items-center justify-between space-y-0 py-1'>
              <div className='space-y-1'>
                <Label htmlFor='enable_analytics_sharing'>
                  Share analytics to Nostr
                </Label>
                <p className='text-muted-foreground text-sm'>
                  When enabled, Routstr periodically publishes aggregate model
                  usage and revenue stats.
                </p>
              </div>
              <Switch
                id='enable_analytics_sharing'
                checked={Boolean(settings.enable_analytics_sharing ?? true)}
                onCheckedChange={(checked) =>
                  handleInputChange('enable_analytics_sharing', checked)
                }
              />
            </div>
          </CardContent>
          {analyticsSharingChanged ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={resetAnalyticsSharing}
                  disabled={loading || saving}
                >
                  Cancel
                </Button>
                <Button onClick={handleSave} disabled={loading || saving}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
        </Card>

        {/* Other Settings */}
        <Card>
          <CardHeader>
            <CardTitle>Advanced Settings</CardTitle>
            <CardDescription>
              Configure additional node settings
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            {Object.keys(settings)
              .filter(
                (key) =>
                  !HANDLED_KEYS.includes(key) && !IGNORED_KEYS.includes(key)
              )
              .map((key) => renderDynamicField(key, settings[key]))}
          </CardContent>
          {advancedChanged ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={resetAdvanced}
                  disabled={loading || saving}
                >
                  Cancel
                </Button>
                <Button onClick={handleSave} disabled={loading || saving}>
                  {saving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
        </Card>

        {/* Password Change */}
        <Card>
          <CardHeader>
            <CardTitle>Change Admin Password</CardTitle>
            <CardDescription>
              Update your admin password for enhanced security
            </CardDescription>
          </CardHeader>
          <CardContent className='space-y-4'>
            {passwordError && (
              <Alert variant='destructive'>
                <AlertCircle className='h-4 w-4' />
                <AlertDescription>{passwordError}</AlertDescription>
              </Alert>
            )}

            <div className='space-y-2'>
              <Label htmlFor='current_password'>Current Password</Label>
              <Input
                id='current_password'
                type='password'
                value={passwordData.current_password}
                onChange={(e) =>
                  setPasswordData((prev) => ({
                    ...prev,
                    current_password: e.target.value,
                  }))
                }
                placeholder='Enter current password'
              />
            </div>

            <div className='space-y-2'>
              <Label htmlFor='new_password'>New Password</Label>
              <Input
                id='new_password'
                type='password'
                value={passwordData.new_password}
                onChange={(e) =>
                  setPasswordData((prev) => ({
                    ...prev,
                    new_password: e.target.value,
                  }))
                }
                placeholder='Enter new password (min 6 characters)'
              />
            </div>

            <div className='space-y-2'>
              <Label htmlFor='confirm_password'>Confirm New Password</Label>
              <Input
                id='confirm_password'
                type='password'
                value={passwordData.confirm_password}
                onChange={(e) =>
                  setPasswordData((prev) => ({
                    ...prev,
                    confirm_password: e.target.value,
                  }))
                }
                placeholder='Confirm new password'
              />
            </div>
          </CardContent>
          {hasPasswordChanges ? (
            <CardFooter className='justify-start'>
              <div className='flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center'>
                <Button
                  variant='outline'
                  onClick={clearPasswordForm}
                  disabled={passwordSaving}
                >
                  Cancel
                </Button>
                <Button
                  onClick={handlePasswordUpdate}
                  disabled={
                    passwordSaving ||
                    !passwordData.current_password ||
                    !passwordData.new_password ||
                    !passwordData.confirm_password
                  }
                >
                  {passwordSaving ? 'Saving...' : 'Save'}
                </Button>
              </div>
            </CardFooter>
          ) : null}
        </Card>
      </div>
    </>
  );
}
