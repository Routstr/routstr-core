'use client';

import { useState, useEffect } from 'react';
import type { ChangeEvent, FormEvent, ReactElement } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { adminLogin } from '@/lib/api/services/auth';
import {
  getApiErrorMessage,
  isNodeUnreachable,
  isUnauthorized,
} from '@/lib/api/errors';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { toast } from 'sonner';
import { AuthPageShell } from '@/components/auth-page-shell';

export default function AdminLoginPage(): ReactElement {
  const router = useRouter();
  const allowCustomBaseUrl = !ConfigurationService.isEnvBaseUrlConfigured();
  const [password, setPassword] = useState<string>('');
  const [baseUrl, setBaseUrl] = useState<string>('');
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [loginError, setLoginError] = useState<string>('');

  useEffect(() => {
    if (ConfigurationService.isTokenValid()) {
      router.push('/');
    }
  }, [router]);

  useEffect(() => {
    if (!allowCustomBaseUrl) {
      return;
    }

    const storedBaseUrl = ConfigurationService.getManualBaseUrl();
    if (storedBaseUrl) {
      setBaseUrl(storedBaseUrl);
      return;
    }

    if (typeof window !== 'undefined') {
      setBaseUrl(window.location.origin ?? '');
    }
  }, [allowCustomBaseUrl]);

  const handleSubmit = async (
    event: FormEvent<HTMLFormElement>
  ): Promise<void> => {
    event.preventDefault();
    setLoginError('');

    // The login request itself must target the entered URL, so it is written
    // before the attempt and rolled back if the node was unreachable.
    const previousBaseUrl = ConfigurationService.getManualBaseUrl();
    if (allowCustomBaseUrl) {
      const normalizedBaseUrl = baseUrl.trim();
      if (!normalizedBaseUrl) {
        setLoginError('Please enter the API URL');
        return;
      }
      ConfigurationService.setManualBaseUrl(normalizedBaseUrl);
    }

    if (!password) {
      setLoginError('Please enter your password');
      return;
    }

    setIsLoading(true);
    try {
      await adminLogin(password);
      toast.success('Successfully logged in');
      router.push('/');
    } catch (error) {
      console.error('Login error:', error);
      if (isNodeUnreachable(error)) {
        setLoginError(
          `Can't reach your node at ${ConfigurationService.getLocalBaseUrl()}. Is it running?`
        );
        if (allowCustomBaseUrl) {
          ConfigurationService.setManualBaseUrl(previousBaseUrl);
        }
      } else if (isUnauthorized(error)) {
        setLoginError('Incorrect password. Please try again.');
      } else {
        setLoginError(getApiErrorMessage(error, 'Login failed'));
      }
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <AuthPageShell
      title='Admin Login'
      description='Enter your admin password to access the dashboard.'
    >
      <form onSubmit={handleSubmit} className='space-y-4'>
        {allowCustomBaseUrl && (
          <div className='space-y-2'>
            <Label htmlFor='api-url'>API URL</Label>
            <Input
              id='api-url'
              name='url'
              type='text'
              autoComplete='url'
              placeholder='https://api.example.com'
              value={baseUrl}
              onChange={(event: ChangeEvent<HTMLInputElement>) =>
                setBaseUrl(event.target.value)
              }
              disabled={isLoading}
              required
            />
          </div>
        )}
        <div className='space-y-2'>
          <Label htmlFor='admin-password'>Admin password</Label>
          <Input
            id='admin-password'
            name='password'
            type='password'
            autoComplete='current-password'
            placeholder='Enter your admin password'
            value={password}
            onChange={(event: ChangeEvent<HTMLInputElement>) =>
              setPassword(event.target.value)
            }
            disabled={isLoading}
            autoFocus
            required
          />
        </div>
        {loginError && (
          <p role='alert' className='text-destructive text-sm'>
            {loginError}
          </p>
        )}
        <Button type='submit' className='w-full' disabled={isLoading}>
          {isLoading ? 'Logging in...' : 'Login'}
        </Button>
      </form>
    </AuthPageShell>
  );
}
