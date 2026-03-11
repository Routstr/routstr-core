'use client';

import { useState, type ReactNode } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
  DatabaseIcon,
  FileTextIcon,
  LayoutDashboardIcon,
  LogOutIcon,
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
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from '@/components/ui/sheet';
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
  { title: 'Models', url: '/models', icon: DatabaseIcon },
  { title: 'Providers', url: '/providers', icon: ServerIcon },
  { title: 'Settings', url: '/settings', icon: SettingsIcon },
] as const;

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
  const [isMobileSidebarOpen, setIsMobileSidebarOpen] = useState(false);

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
            'border-border/60 hidden shrink-0 border-r py-5 transition-[width,padding] duration-300 ease-in-out md:flex md:h-full md:flex-col md:overflow-y-auto',
            isSidebarCollapsed ? 'w-16 px-2' : 'w-60 px-4'
          )}
        >
          <div
            className={cn(
              'px-1 transition-[padding] duration-300 ease-in-out',
              isSidebarCollapsed && 'px-0'
            )}
          >
            <div className='flex items-center gap-2'>
              <div className='flex min-w-0 flex-1 items-center gap-2 overflow-hidden'>
                <Image
                  src='/icon.ico'
                  alt='Routstr Node'
                  width={24}
                  height={24}
                  className='shrink-0 rounded-sm'
                />
                <div
                  className={cn(
                    'min-w-0 overflow-hidden transition-[max-width,opacity,transform] duration-300 ease-in-out',
                    isSidebarCollapsed
                      ? 'max-w-0 -translate-x-1 opacity-0'
                      : 'max-w-[11rem] translate-x-0 opacity-100'
                  )}
                >
                  <h1 className='truncate text-lg font-semibold tracking-tight whitespace-nowrap'>
                    Routstr Node
                  </h1>
                </div>
              </div>
              <Button
                variant='ghost'
                size='icon'
                className={cn(
                  'text-muted-foreground hover:text-foreground h-8 w-8 shrink-0 transition-transform duration-300 ease-in-out',
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
                    'h-10 rounded-lg transition-[width,padding] duration-300 ease-in-out',
                    isSidebarCollapsed
                      ? 'mx-auto w-10 justify-center px-0'
                      : 'w-full justify-start'
                  )}
                >
                  <Link href={item.url}>
                    <Icon className='h-4 w-4 shrink-0' />
                    <span
                      className={cn(
                        'overflow-hidden whitespace-nowrap transition-[max-width,opacity,margin] duration-300 ease-in-out',
                        isSidebarCollapsed
                          ? 'ml-0 max-w-0 opacity-0'
                          : 'ml-0.5 max-w-[9rem] opacity-100'
                      )}
                    >
                      {item.title}
                    </span>
                  </Link>
                </Button>
              );
            })}
          </nav>

          <div
            className={cn(
              'mt-auto pt-3 transition-[padding] duration-300 ease-in-out',
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
          <div className='bg-background/80 supports-[backdrop-filter]:bg-background/72 sticky top-0 z-30 flex items-center gap-2 px-3 py-2 backdrop-blur-xl md:hidden'>
            <Button
              type='button'
              variant='outline'
              size='sm'
              className='h-9 gap-2 rounded-lg px-3'
              onClick={() => setIsMobileSidebarOpen(true)}
            >
              <PanelLeftOpenIcon className='h-4 w-4' />
              Menu
            </Button>
          </div>
          <main
            className={cn(
              'w-full min-w-0 flex-1 overflow-x-clip p-3 pb-4 sm:p-4 md:min-h-0 md:overflow-y-auto md:p-6 md:pb-6',
              contentClassName,
              className
            )}
          >
            {children}
          </main>
        </section>
      </div>

      <Sheet open={isMobileSidebarOpen} onOpenChange={setIsMobileSidebarOpen}>
        <SheetContent
          side='left'
          showCloseButton={false}
          className='w-[min(88vw,18rem)] p-0 md:hidden'
        >
          <SheetTitle className='sr-only'>Navigation sidebar</SheetTitle>
          <SheetDescription className='sr-only'>
            Browse admin pages and access sidebar controls.
          </SheetDescription>
          <div className='flex h-full min-h-0 flex-col'>
            <div className='border-border/60 px-4 pt-4 pb-3'>
              <div className='flex items-center justify-between gap-3'>
                <div className='flex min-w-0 items-center gap-2'>
                  <Image
                    src='/icon.ico'
                    alt='Routstr Node'
                    width={24}
                    height={24}
                    className='rounded-sm'
                  />
                  <p className='truncate text-base font-medium tracking-tight'>
                    Routstr Node
                  </p>
                </div>
                <SheetClose asChild>
                  <Button
                    variant='ghost'
                    size='icon'
                    className='text-muted-foreground hover:text-foreground h-8 w-8 shrink-0'
                  >
                    <PanelLeftCloseIcon className='h-4 w-4' />
                    <span className='sr-only'>Close sidebar</span>
                  </Button>
                </SheetClose>
              </div>
            </div>
            <div className='flex h-full min-h-0 flex-col px-3 pb-3'>
              <nav className='space-y-1.5 py-3'>
                {NAV_ITEMS.map((item) => {
                  const Icon = item.icon;
                  const active = isActivePath(pathname, item.url);

                  return (
                    <Button
                      key={`mobile-sidebar-${item.url}`}
                      asChild
                      variant={active ? 'outline' : 'ghost'}
                      className='h-10 w-full justify-start rounded-lg'
                    >
                      <Link
                        href={item.url}
                        onClick={() => setIsMobileSidebarOpen(false)}
                      >
                        <Icon className='h-4 w-4' />
                        {item.title}
                      </Link>
                    </Button>
                  );
                })}
              </nav>

              <div className='border-border/60 bg-card/30 mt-auto space-y-1 rounded-lg border p-1'>
                <CurrencyToggle
                  menuSide='right'
                  menuAlign='start'
                  className='text-foreground/90 hover:bg-accent/35 border-border/60 bg-background/25 h-9 w-full justify-between rounded-md px-2.5 text-[11px]'
                />
                <ThemeToggle
                  menuSide='right'
                  menuAlign='start'
                  className='text-foreground/90 hover:bg-accent/35 border-border/60 bg-background/25 h-9 w-full justify-between rounded-md px-2.5 text-[11px]'
                />
                <Button
                  variant='ghost'
                  size='sm'
                  onClick={async () => {
                    setIsMobileSidebarOpen(false);
                    await handleLogout();
                  }}
                  className='text-muted-foreground hover:bg-destructive/10 hover:text-destructive h-9 w-full justify-start gap-1.5 rounded-md px-2.5 text-[11px]'
                >
                  <LogOutIcon className='h-4 w-4' />
                  Logout
                </Button>
              </div>
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
