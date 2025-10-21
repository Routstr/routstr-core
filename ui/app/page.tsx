import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { DetailedWalletBalance } from '@/components/detailed-wallet-balance';

export default function Page() {
  return (
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset className='p-0'>
        <SiteHeader />
        <div className='container max-w-6xl px-4 py-8 md:px-6 lg:px-8'>
          <div className='mb-8'>
            <h1 className='text-3xl font-bold tracking-tight'>
              Admin Dashboard
            </h1>
            <p className='text-muted-foreground mt-2'>
              Monitor and manage wallet balances
            </p>
          </div>

          <div className='grid gap-6'>
            <div className='col-span-full'>
              <DetailedWalletBalance refreshInterval={5000} />
            </div>
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
