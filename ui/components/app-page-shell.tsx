'use client';

import { useState, type ReactNode } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
  DatabaseIcon,
  FileTextIcon,
  LayoutDashboardIcon,
  LogOutIcon,
  MoreHorizontalIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  ServerIcon,
  SettingsIcon,
  WalletIcon,
} from 'lucide-react';
import Image from 'next/image';
import { toast } from 'sonner';
import { adminLogout } from '@/lib/api/services/auth';
import { Button } from '@/components/ui/button';
import { CurrencyToggle } from '@/components/currency-toggle';
import { ThemeToggle } from '@/components/theme-toggle';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import { cn } from '@/lib/utils';

interface AppPageShellProps {
  children: ReactNode;
  className?: string;
  contentClassName?: string;
}

const NAV_ITEMS = [
  { title: 'Dashboard', url: '/', icon: LayoutDashboardIcon },
  { title: 'Balances', url: '/balances', icon: WalletIcon },
  { title: 'Logs', url: '/logs', icon: FileTextIcon },
  { title: 'Models', url: '/model', icon: DatabaseIcon },
  { title: 'Providers', url: '/providers', icon: ServerIcon },
  { title: 'Settings', url: '/settings', icon: SettingsIcon },
] as const;

const MOBILE_NAV_TAB_WIDTH_CLASS = 'auto-cols-[22%]';
const MOBILE_NAV_TAB_WIDTH_CLASS_XS = 'max-[359px]:auto-cols-[31%]';

function isActivePath(pathname: string, itemUrl: string): boolean {
  if (itemUrl === '/') {
    return pathname === '/';
  }

  return pathname === itemUrl || pathname.startsWith(`${itemUrl}/`);
}

export function AppPageShell({
  children,
  className,
  contentClassName,
}: AppPageShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [isMobileMoreOpen, setIsMobileMoreOpen] = useState(false);

  const handleLogout = async (): Promise<void> => {
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
    <div className='bg-background text-foreground min-h-dvh overflow-x-clip md:h-screen md:overflow-hidden'>
      <div className='flex min-h-dvh w-full min-w-0 overflow-x-clip md:h-full'>
        <aside
          className={cn(
            'border-border/60 hidden shrink-0 border-r py-5 transition-[width,padding] duration-200 md:flex md:h-full md:flex-col md:overflow-y-auto',
            isSidebarCollapsed ? 'w-16 px-2' : 'w-60 px-4'
          )}
        >
          <div className={cn('px-1', isSidebarCollapsed && 'px-0')}>
            <div
              className={cn(
                'flex items-center',
                isSidebarCollapsed ? 'justify-center' : 'justify-start gap-2'
              )}
            >
              {isSidebarCollapsed ? null : (
                <div className='flex min-w-0 items-center gap-2 overflow-hidden'>
                  <Image
                    src='/icon.ico'
                    alt='Routstr Node'
                    width={24}
                    height={24}
                    className='rounded-sm'
                  />
                  <h1 className='text-lg font-semibold tracking-tight'>
                    Routstr Node
                  </h1>
                </div>
              )}
              <Button
                variant='ghost'
                size='icon'
                className={cn(
                  'text-muted-foreground hover:text-foreground h-8 w-8',
                  isSidebarCollapsed ? 'mx-auto' : '-mr-1 ml-auto'
                )}
                onClick={() => setIsSidebarCollapsed((current) => !current)}
              >
                {isSidebarCollapsed ? (
                  <PanelLeftOpenIcon className='h-4 w-4' />
                ) : (
                  <PanelLeftCloseIcon className='h-4 w-4' />
                )}
                <span className='sr-only'>
                  {isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                </span>
              </Button>
            </div>
          </div>

          <nav
            className={cn(
              'mt-5',
              isSidebarCollapsed
                ? 'flex flex-col items-center space-y-2'
                : 'space-y-1.5'
            )}
          >
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = isActivePath(pathname, item.url);

              return (
                <Button
                  key={item.url}
                  asChild
                  variant={active ? 'outline' : 'ghost'}
                  className={cn(
                    'h-10 rounded-lg',
                    isSidebarCollapsed
                      ? 'mx-auto w-10 justify-center px-0'
                      : 'w-full justify-start'
                  )}
                >
                  <Link href={item.url}>
                    <Icon className='h-4 w-4' />
                    {isSidebarCollapsed ? (
                      <span className='sr-only'>{item.title}</span>
                    ) : (
                      item.title
                    )}
                  </Link>
                </Button>
              );
            })}
          </nav>

          <div
            className={cn(
              'mt-auto pt-3',
              isSidebarCollapsed
                ? 'flex flex-col items-center space-y-1.5'
                : 'space-y-2'
            )}
          >
            {isSidebarCollapsed ? (
              <>
                <CurrencyToggle
                  compact
                  menuSide='right'
                  menuAlign='start'
                  className='mx-auto'
                />
                <ThemeToggle
                  compact
                  menuSide='right'
                  menuAlign='start'
                  className='mx-auto'
                />
                <Button
                  variant='ghost'
                  size='sm'
                  onClick={handleLogout}
                  className='text-muted-foreground hover:text-foreground mx-auto h-8 w-10 justify-center px-0'
                >
                  <LogOutIcon className='h-4 w-4' />
                  <span className='sr-only'>Logout</span>
                </Button>
              </>
            ) : (
              <div className='border-border/60 bg-card/30 space-y-1 rounded-lg border p-1'>
                <CurrencyToggle
                  menuSide='right'
                  menuAlign='start'
                  className='text-foreground/90 hover:bg-accent/35 border-border/60 bg-background/25 h-8 w-full justify-between rounded-md px-2.5 text-[11px]'
                />
                <ThemeToggle
                  menuSide='right'
                  menuAlign='start'
                  className='text-foreground/90 hover:bg-accent/35 border-border/60 bg-background/25 h-8 w-full justify-between rounded-md px-2.5 text-[11px]'
                />
                <Button
                  variant='ghost'
                  size='sm'
                  onClick={handleLogout}
                  className='text-muted-foreground hover:bg-destructive/10 hover:text-destructive h-8 w-full justify-start gap-1.5 rounded-md px-2.5 text-[11px]'
                >
                  <LogOutIcon className='h-4 w-4' />
                  Logout
                </Button>
              </div>
            )}
          </div>
        </aside>

        <section className='relative flex w-full min-w-0 flex-1 flex-col overflow-x-clip md:h-full md:min-h-0'>
          <main
            className={cn(
              'pb-mobile-nav w-full min-w-0 flex-1 overflow-x-clip p-3 sm:p-4 md:min-h-0 md:overflow-y-auto md:p-6 md:pb-6',
              contentClassName,
              className
            )}
          >
            {children}
          </main>
        </section>
      </div>

      <div className='pointer-events-none fixed inset-x-0 bottom-0 z-40 px-4 pb-[calc(0.35rem+env(safe-area-inset-bottom))] md:hidden'>
        <nav className='border-border/65 bg-background/80 supports-[backdrop-filter]:bg-background/72 pointer-events-auto mx-auto w-full max-w-[34rem] overflow-x-auto overscroll-x-contain rounded-[1.5rem] border shadow-[0_-16px_36px_-22px_rgba(0,0,0,0.9)] backdrop-blur-2xl [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden'>
          <div
            className={cn(
              'grid min-w-full snap-x snap-mandatory grid-flow-col gap-2 p-2.5',
              MOBILE_NAV_TAB_WIDTH_CLASS,
              MOBILE_NAV_TAB_WIDTH_CLASS_XS
            )}
          >
            {NAV_ITEMS.map((item) => {
              const Icon = item.icon;
              const active = isActivePath(pathname, item.url);

              return (
                <Button
                  key={`mobile-bottom-${item.url}`}
                  asChild
                  size='sm'
                  variant={active ? 'outline' : 'ghost'}
                  className='h-12 w-full snap-start flex-col gap-0.5 rounded-2xl px-2 text-[10px] leading-none'
                >
                  <Link href={item.url}>
                    <Icon className='h-4 w-4' />
                    <span className='truncate'>{item.title}</span>
                  </Link>
                </Button>
              );
            })}
            <Button
              type='button'
              size='sm'
              variant={isMobileMoreOpen ? 'outline' : 'ghost'}
              className='h-12 w-full snap-start flex-col gap-0.5 rounded-2xl px-2 text-[10px] leading-none'
              onClick={() => setIsMobileMoreOpen(true)}
            >
              <MoreHorizontalIcon className='h-4 w-4' />
              <span className='truncate'>More</span>
            </Button>
          </div>
        </nav>
      </div>

      <Drawer open={isMobileMoreOpen} onOpenChange={setIsMobileMoreOpen}>
        <DrawerContent className='md:hidden'>
          <DrawerHeader className='px-4 pb-2 text-left'>
            <DrawerTitle>More</DrawerTitle>
            <DrawerDescription>
              Account and appearance settings.
            </DrawerDescription>
          </DrawerHeader>

          <div className='space-y-4 px-4 pb-4'>
            <div className='space-y-2'>
              <p className='text-muted-foreground text-xs font-semibold tracking-[0.08em] uppercase'>
                Preferences
              </p>
              <div className='grid grid-cols-2 gap-2'>
                <CurrencyToggle
                  menuSide='top'
                  menuAlign='start'
                  className='h-11 w-full justify-between rounded-lg px-3'
                />
                <ThemeToggle
                  menuSide='top'
                  menuAlign='end'
                  className='h-11 w-full justify-between rounded-lg px-3'
                />
              </div>
            </div>

            <div className='space-y-2'>
              <p className='text-muted-foreground text-xs font-semibold tracking-[0.08em] uppercase'>
                Account
              </p>
              <Button
                variant='outline'
                size='sm'
                onClick={async () => {
                  setIsMobileMoreOpen(false);
                  await handleLogout();
                }}
                className='h-11 w-full justify-start gap-2 rounded-lg px-3'
              >
                <LogOutIcon className='h-4 w-4' />
                Logout
              </Button>
            </div>
          </div>
        </DrawerContent>
      </Drawer>
    </div>
  );
}
