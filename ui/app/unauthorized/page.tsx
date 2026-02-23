'use client';

import { Button } from '@/components/ui/button';
import { useRouter } from 'next/navigation';
import { ShieldAlertIcon } from 'lucide-react';
import { AuthPageShell } from '@/components/auth-page-shell';

export default function UnauthorizedPage() {
  const router = useRouter();

  return (
    <AuthPageShell
      title='Access Denied'
      description="You don't have permission to access this page."
    >
      <div className='flex flex-col items-center gap-6'>
        <ShieldAlertIcon className='text-destructive h-20 w-20' />
        <p className='text-muted-foreground text-center text-sm'>
          Contact your administrator if you believe this is an error.
        </p>
        <div className='flex w-full flex-col gap-3 sm:w-auto sm:flex-row'>
          <Button onClick={() => router.push('/')} className='w-full sm:w-auto'>
            Go to Dashboard
          </Button>
          <Button
            variant='outline'
            onClick={() => router.back()}
            className='w-full sm:w-auto'
          >
            Go Back
          </Button>
        </div>
      </div>
    </AuthPageShell>
  );
}
