'use client';

import { useAuth } from '@/lib/auth/AuthContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useRouter } from 'next/navigation';
import { useState } from 'react';
import { toast } from 'sonner';
import Link from 'next/link';
import { ArrowUpCircleIcon } from 'lucide-react';
import { registerUser, SchemaRegisterProps } from '@/lib/api/services/auth';

export default function NostrRegisterPage() {
  const router = useRouter();
  const { connectNostr } = useAuth();
  const [isLoading, setIsLoading] = useState(false);
  const [formData, setFormData] = useState<SchemaRegisterProps>({
    npub: '',
    name: '',
  });

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleNostrConnect = async () => {
    setIsLoading(true);
    try {
      const publicKey = await connectNostr();
      if (publicKey) {
        setFormData((prev) => ({ ...prev, npub: publicKey }));
        toast.success(
          'Nostr connected. Please enter your name to complete registration.'
        );
      } else {
        toast.error(
          'Failed to connect. Please make sure your Nostr extension is installed and enabled.'
        );
      }
    } catch (error) {
      console.error('Nostr connection error:', error);
      toast.error('Failed to connect to Nostr. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.npub || formData.npub.length < 10) {
      toast.error('Please enter a valid Nostr public key');
      return;
    }

    if (!formData.name) {
      toast.error('Please enter your name');
      return;
    }

    setIsLoading(true);
    try {
      // Register the user
      const result = await registerUser(formData);
      console.log('Registration successful:', result);
      toast.success('Account created successfully');
      router.push('/login');
    } catch (error) {
      console.error('Registration error:', error);
      toast.error('Registration failed. Please try again.');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className='flex min-h-screen items-center justify-center p-4'>
      <div className='w-full max-w-md'>
        <div className='flex flex-col gap-6'>
          <form onSubmit={handleRegister}>
            <div className='flex flex-col gap-6'>
              <div className='flex flex-col items-center gap-2'>
                <Link
                  href='/'
                  className='flex flex-col items-center gap-2 font-medium'
                >
                  <div className='flex h-8 w-8 items-center justify-center rounded-md'>
                    <ArrowUpCircleIcon className='size-6' />
                  </div>
                  <span className='sr-only'>Routstr</span>
                </Link>
                <h1 className='text-xl font-bold'>Create an Account</h1>
                <div className='text-center text-sm'>
                  Already have an account?{' '}
                  <Link href='/login' className='underline underline-offset-4'>
                    Sign in
                  </Link>
                </div>
              </div>
              <div className='flex flex-col gap-6'>
                <div className='grid gap-2'>
                  <Label htmlFor='npub'>Nostr Public Key (npub)</Label>
                  <Input
                    id='npub'
                    name='npub'
                    type='text'
                    placeholder='npub1...'
                    value={formData.npub}
                    onChange={handleInputChange}
                    required
                  />
                </div>
                <div className='grid gap-2'>
                  <Label htmlFor='name'>Name</Label>
                  <Input
                    id='name'
                    name='name'
                    type='text'
                    placeholder='John Doe'
                    value={formData.name}
                    onChange={handleInputChange}
                    required
                  />
                </div>
                <Button type='submit' className='w-full' disabled={isLoading}>
                  {isLoading ? 'Creating account...' : 'Create Account'}
                </Button>
              </div>
              <div className='after:border-border relative text-center text-sm after:absolute after:inset-0 after:top-1/2 after:z-0 after:flex after:items-center after:border-t'>
                <span className='bg-background text-muted-foreground relative z-10 px-2'>
                  Or
                </span>
              </div>
              <div className='grid gap-4'>
                <Button
                  variant='outline'
                  className='w-full'
                  onClick={handleNostrConnect}
                  disabled={isLoading}
                  type='button'
                >
                  <svg
                    className='mr-2 h-4 w-4'
                    viewBox='0 0 256 256'
                    xmlns='http://www.w3.org/2000/svg'
                  >
                    <path
                      d='M158.4 28.4c-31.8-31.8-83.1-31.8-114.9 0s-31.8 83.1 0 114.9l57.4 57.4 57.4-57.4c31.8-31.8 31.8-83.1 0-114.9z'
                      fill='currentColor'
                    />
                    <path
                      d='M215.8 199.3c-31.8-31.8-83.1-31.8-114.9 0L43.6 256l57.4-57.4c31.8-31.8 31.8-83.1 0-114.9L158.4 28.4 101 85.8c-31.8 31.8-31.8 83.1 0 114.9l114.8-1.4z'
                      fill='currentColor'
                    />
                  </svg>
                  Connect with Nostr Extension
                </Button>
              </div>
            </div>
          </form>
          <div className='text-muted-foreground hover:[&_a]:text-primary text-center text-xs text-balance [&_a]:underline [&_a]:underline-offset-4'>
            By clicking create account, you agree to our{' '}
            <Link href='#'>Terms of Service</Link> and{' '}
            <Link href='#'>Privacy Policy</Link>.
          </div>
        </div>
      </div>
    </div>
  );
}
