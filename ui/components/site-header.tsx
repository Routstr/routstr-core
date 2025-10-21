'use client';

import { Separator } from '@/components/ui/separator';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { LogOut } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { adminLogout } from '@/lib/api/services/auth';
import { toast } from 'sonner';

export function SiteHeader() {
  const router = useRouter();

  const handleLogout = async () => {
    try {
      await adminLogout();
      toast.success('Logged out successfully');
      router.push('/login');
    } catch (error) {
      console.error('Logout error:', error);
      toast.error('Failed to logout');
    }
  };

  return (
    <header className='flex h-12 shrink-0 items-center gap-2 border-b transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-12'>
      <div className='flex w-full items-center justify-between gap-1 px-4 lg:gap-2 lg:px-6'>
        <div className='flex items-center gap-1 lg:gap-2'>
          <SidebarTrigger className='-ml-1' />
          <Separator
            orientation='vertical'
            className='mx-2 data-[orientation=vertical]:h-4'
          />
          <h1 className='text-base font-medium'>Routstr</h1>
        </div>
        <Button
          variant='ghost'
          size='sm'
          onClick={handleLogout}
          className='gap-2'
        >
          <LogOut className='h-4 w-4' />
          <span className='hidden sm:inline'>Logout</span>
        </Button>
      </div>
    </header>
  );
}
