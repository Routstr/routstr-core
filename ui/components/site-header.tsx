'use client';

import { Separator } from '@/components/ui/separator';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { Button } from '@/components/ui/button';
import { LogOut } from 'lucide-react';
import { usePathname, useRouter } from 'next/navigation';
import { adminLogout } from '@/lib/api/services/auth';
import { toast } from 'sonner';
import { ThemeToggle } from '@/components/theme-toggle';
import { CurrencyToggle } from '@/components/currency-toggle';

const PAGE_META: Record<string, { title: string; description: string }> = {
  '/': {
    title: 'Dashboard',
    description: 'Usage, errors, revenue, and system health.',
  },
  '/balances': {
    title: 'Balances',
    description: 'Wallet and temporary balance overview.',
  },
  '/logs': {
    title: 'System Logs',
    description: 'Inspect request and application logs.',
  },
  '/model': {
    title: 'Models',
    description: 'Manage model catalog and provider mappings.',
  },
  '/providers': {
    title: 'Providers',
    description: 'Configure upstream providers and model sync.',
  },
  '/settings': {
    title: 'Settings',
    description: 'Admin, routing, and system-level configuration.',
  },
};

export function SiteHeader() {
  const router = useRouter();
  const pathname = usePathname();
  const meta = PAGE_META[pathname] ?? {
    title: 'Routstr Node',
    description: 'Administration panel',
  };

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
    <header className='border-border/60 bg-background/80 sticky top-0 z-20 border-b backdrop-blur-xl'>
      <div className='flex h-16 items-center justify-between gap-3 px-3 sm:px-5 md:px-6'>
        <div className='flex min-w-0 items-center gap-2'>
          <SidebarTrigger className='shrink-0 md:-ml-1' />
          <Separator orientation='vertical' className='hidden h-5 md:block' />
          <div className='min-w-0'>
            <h1 className='truncate text-sm font-semibold tracking-tight sm:text-base'>
              {meta.title}
            </h1>
            <p className='text-muted-foreground hidden truncate text-xs sm:block'>
              {meta.description}
            </p>
          </div>
        </div>
        <div className='flex shrink-0 items-center gap-1.5'>
          <CurrencyToggle />
          <ThemeToggle />
          <Button
            variant='outline'
            size='sm'
            onClick={handleLogout}
            className='h-8 gap-2 px-2.5 sm:px-3'
          >
            <LogOut className='h-4 w-4' />
            <span className='hidden sm:inline'>Logout</span>
          </Button>
        </div>
      </div>
    </header>
  );
}
