'use client';

import { useEffect, useState } from 'react';
import {
  AdminService,
  type AnalyticsSharingHistory,
} from '@/lib/api/services/admin';
import { Button } from '@/components/ui/button';
import { toast } from 'sonner';

export function AnalyticsHistory() {
  const [history, setHistory] = useState<AnalyticsSharingHistory | null>(null);
  const [publishing, setPublishing] = useState<number | null>(null);

  const load = () =>
    AdminService.getAnalyticsSharingHistory()
      .then(setHistory)
      .catch(() => setHistory(null));

  useEffect(() => {
    load();
  }, []);

  if (!history?.sharing_enabled || history.epochs.length === 0) return null;

  const publish = async (epoch: number) => {
    if (
      !confirm(
        'These stats were collected while public sharing was off or not yet active. ' +
          'Publishing them to Nostr relays is permanent. Continue?'
      )
    )
      return;
    setPublishing(epoch);
    try {
      await AdminService.releaseAnalyticsSharingHistory(
        [epoch],
        history.generation
      );
      toast.success('Older stats will be published on the next pass');
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : 'Failed to publish older stats'
      );
    } finally {
      setPublishing(null);
      load();
    }
  };

  return (
    <div className='space-y-2 pt-4'>
      <p className='text-sm font-medium'>Unpublished history</p>
      <p className='text-muted-foreground text-sm'>
        Stats collected while public sharing was off or not yet active. Only the
        last 365 days can be published.
      </p>
      {history.epochs.map((epoch) => (
        <div
          key={epoch.epoch}
          className='flex items-center justify-between gap-4 text-sm'
        >
          <span>
            {epoch.coverage_start} – {epoch.coverage_end}:{' '}
            {epoch.requests.toLocaleString()} requests, {epoch.publishable_days}{' '}
            publishable days
          </span>
          {epoch.released ? (
            <span className='text-muted-foreground'>Publishing</span>
          ) : (
            <Button
              size='sm'
              variant='outline'
              disabled={publishing !== null || epoch.publishable_days === 0}
              onClick={() => publish(epoch.epoch)}
            >
              Publish
            </Button>
          )}
        </div>
      ))}
    </div>
  );
}
