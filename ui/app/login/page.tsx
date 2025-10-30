'use client';

import { useState, useEffect } from 'react';
import type { ChangeEvent, FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { adminLogin } from '@/lib/api/services/auth';
import { ConfigurationService } from '@/lib/api/services/configuration';
import { toast } from 'sonner';

export default function AdminLoginPage(): JSX.Element {
  const router = useRouter();
  const allowCustomBaseUrl = !ConfigurationService.isEnvBaseUrlConfigured();
  const [password, setPassword] = useState<string>('');
  const [baseUrl, setBaseUrl] = useState<string>('');
  const [isLoading, setIsLoading] = useState<boolean>(false);

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

    if (allowCustomBaseUrl) {
      const normalizedBaseUrl = baseUrl.trim();
      if (!normalizedBaseUrl) {
        toast.error('Please enter the API URL');
        return;
      }
      ConfigurationService.setManualBaseUrl(normalizedBaseUrl);
    }

    if (!password) {
      toast.error('Please enter your password');
      return;
    }

    setIsLoading(true);
    try {
      await adminLogin(password);
      toast.success('Successfully logged in');
      router.push('/');
    } catch (error) {
      console.error('Login error:', error);
      toast.error('Invalid password. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className='flex min-h-screen items-center justify-center bg-gray-50 p-4'>
      <Card className='w-full max-w-md'>
        <CardHeader className='space-y-1'>
          <CardTitle className='text-center text-2xl font-bold'>
            Admin Login
          </CardTitle>
          <CardDescription className='text-center'>
            Enter your admin password to access the dashboard
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className='space-y-4'>
            {allowCustomBaseUrl && (
              <div className='space-y-2'>
                <Input
                  type='text'
                  placeholder='API URL (https://api.example.com)'
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
              <Input
                type='password'
                placeholder='Admin Password'
                value={password}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  setPassword(event.target.value)
                }
                disabled={isLoading}
                autoFocus
                required
              />
            </div>
            <Button type='submit' className='w-full' disabled={isLoading}>
              {isLoading ? 'Logging in...' : 'Login'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
