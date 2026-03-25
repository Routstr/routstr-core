import { Badge } from '@/components/ui/badge';
import { ChevronRight } from 'lucide-react';
import { getLogLevelBadgeVariant } from '@/lib/utils/log-level';
import type { LogEntry } from './types';

interface LogEntryCardProps {
  entry: LogEntry;
  onClick: (entry: LogEntry) => void;
}

export function LogEntryCard({ entry, onClick }: LogEntryCardProps) {
  const extraFields = Object.keys(entry).filter(
    (key) =>
      ![
        'asctime',
        'name',
        'levelname',
        'message',
        'pathname',
        'lineno',
        'version',
        'request_id',
      ].includes(key)
  );
  const hasRequestId =
    Boolean(entry.request_id) && entry.request_id !== 'no-request-id';
  const shortPath = entry.pathname.split('/').pop() || entry.pathname;

  return (
    <div
      className='bg-card hover:bg-accent/35 group mb-2 cursor-pointer rounded-lg border p-2.5 transition-colors duration-150 sm:p-3'
      onClick={() => onClick(entry)}
    >
      <div className='flex min-w-0 items-start justify-between gap-2'>
        <div className='min-w-0 flex-1 space-y-1.5'>
          <div className='flex min-w-0 flex-wrap items-center gap-1.5'>
            <Badge
              variant={getLogLevelBadgeVariant(entry.levelname)}
              className='h-5 px-1.5 text-[10px] uppercase'
            >
              {entry.levelname}
            </Badge>
            <span className='text-muted-foreground text-[11px]'>
              {entry.asctime}
            </span>
            <Badge
              variant='secondary'
              className='h-5 max-w-[11rem] truncate px-1.5 text-[10px]'
            >
              {entry.name}
            </Badge>
          </div>

          <p className='line-clamp-1 font-mono text-xs break-words sm:text-sm'>
            {entry.message}
          </p>

          <div className='text-muted-foreground flex min-w-0 flex-wrap items-center gap-1.5 text-[11px]'>
            {hasRequestId ? (
              <span className='inline-block max-w-[14rem] truncate rounded border px-1.5 py-0.5 font-mono text-[10px] sm:max-w-[20rem]'>
                {entry.request_id}
              </span>
            ) : null}
            <span className='truncate'>
              {shortPath}:{entry.lineno}
            </span>
            {extraFields.length > 0 ? (
              <span>{extraFields.length} extra</span>
            ) : null}
          </div>
        </div>

        <div className='pt-0.5'>
          <ChevronRight className='text-muted-foreground h-4 w-4 opacity-50 transition-opacity group-hover:opacity-90' />
        </div>
      </div>
    </div>
  );
}
