'use client';

import { Button } from '@/components/ui/button';
import { useRouter } from 'next/navigation';
import { ShieldAlertIcon } from 'lucide-react';

export default function UnauthorizedPage() {
  const router = useRouter();

  return (
    <div className='bg-background flex min-h-screen flex-col items-center justify-center p-4'>
      <div className='flex max-w-md flex-col items-center space-y-6 text-center'>
        <ShieldAlertIcon className='text-destructive h-24 w-24' />

        <h1 className='text-4xl font-bold'>Access Denied</h1>

        <p className='text-muted-foreground text-lg'>
          You don&apos;t have permission to access this page. Please contact
          your administrator if you believe this is an error.
        </p>

        <div className='flex gap-4'>
          <Button onClick={() => router.push('/')}>Go to Dashboard</Button>

          <Button variant='outline' onClick={() => router.back()}>
            Go Back
          </Button>
        </div>
      </div>
    </div>
  );
}
