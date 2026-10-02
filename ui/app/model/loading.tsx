import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';
import { Skeleton } from '@/components/ui/skeleton';

/**
 * Route-level fallback so clicking "Models" lands on the page immediately
 * instead of holding the previous route until this one's chunk is parsed.
 */
export default function ModelPageLoading() {
  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl'>
      <div className='space-y-3 sm:space-y-4'>
        <PageHeader
          title='Model Management'
          description='Manage provider model catalogs and validate endpoints from one place.'
        />
        <Skeleton className='h-10 w-full' />
        <Skeleton className='h-16 w-full' />
        <Skeleton className='h-[420px] w-full' />
      </div>
    </AppPageShell>
  );
}
