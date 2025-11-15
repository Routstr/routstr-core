import { Badge } from '@/components/ui/badge';
import { Eye } from 'lucide-react';

interface LogEntry {
  asctime: string;
  name: string;
  levelname: string;
  message: string;
  pathname: string;
  lineno: number;
  version: string;
  request_id: string;
  [key: string]: string | number | object | undefined;
}

interface LogEntryCardProps {
  entry: LogEntry;
  onClick: (entry: LogEntry) => void;
}

const getLevelColor = (level: string): string => {
  switch (level.toUpperCase()) {
    case 'TRACE':
    case 'DEBUG':
      return 'bg-gray-100 text-gray-800 border-gray-200';
    case 'INFO':
      return 'bg-blue-100 text-blue-800 border-blue-200';
    case 'WARNING':
      return 'bg-yellow-100 text-yellow-800 border-yellow-200';
    case 'ERROR':
      return 'bg-red-100 text-red-800 border-red-200';
    case 'CRITICAL':
      return 'bg-purple-100 text-purple-800 border-purple-200';
    default:
      return 'bg-gray-100 text-gray-800 border-gray-200';
  }
};

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

  return (
    <div
      key={`${entry.request_id}-${entry.asctime}-${entry.lineno}`}
      className='bg-card hover:bg-accent/50 group mb-4 cursor-pointer overflow-hidden rounded-lg border p-3 transition-colors duration-200 sm:p-4'
      onClick={() => onClick(entry)}
    >
      <div className='mb-3 flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:justify-between'>
        <div className='flex min-w-0 flex-wrap items-center gap-2'>
          <Badge variant='outline' className={getLevelColor(entry.levelname)}>
            {entry.levelname}
          </Badge>
          <span className='text-muted-foreground truncate text-xs sm:text-sm'>
            {entry.asctime}
          </span>
          <Badge variant='secondary' className='truncate text-xs'>
            {entry.name}
          </Badge>
        </div>
        <div className='flex min-w-0 items-center gap-2'>
          <div className='text-muted-foreground truncate text-xs'>
            {entry.pathname}:{entry.lineno}
          </div>
          <Eye className='text-muted-foreground h-4 w-4 flex-shrink-0 opacity-0 transition-opacity group-hover:opacity-100' />
        </div>
      </div>

      <div className='mb-2 line-clamp-3 overflow-hidden font-mono text-xs break-words sm:text-sm'>
        {entry.message}
      </div>

      {entry.request_id && entry.request_id !== 'no-request-id' && (
        <div className='mb-2 min-w-0'>
          <div className='inline-block max-w-full'>
            <Badge variant='outline' className='text-xs'>
              <span className='inline-block max-w-[250px] truncate sm:max-w-[400px]'>
                Request ID: {entry.request_id}
              </span>
            </Badge>
          </div>
        </div>
      )}

      {extraFields.length > 0 && (
        <div className='mt-3 min-w-0 border-t pt-3'>
          <div className='mb-2 text-xs font-medium'>Additional Fields:</div>
          <div className='grid grid-cols-1 gap-2'>
            {extraFields.slice(0, 4).map((key) => (
              <div
                key={key}
                className='min-w-0 overflow-hidden text-xs break-words'
              >
                <span className='font-medium break-all'>{key}:</span>{' '}
                <span className='text-muted-foreground break-all'>
                  {typeof entry[key] === 'object'
                    ? JSON.stringify(entry[key])
                    : String(entry[key])}
                </span>
              </div>
            ))}
            {extraFields.length > 4 && (
              <div className='text-muted-foreground text-xs'>
                ...and {extraFields.length - 4} more fields
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
