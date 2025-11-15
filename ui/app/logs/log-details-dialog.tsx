import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Copy } from 'lucide-react';

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

interface LogDetailsDialogProps {
  log: LogEntry | null;
  isOpen: boolean;
  onClose: () => void;
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

export function LogDetailsDialog({
  log,
  isOpen,
  onClose,
}: LogDetailsDialogProps) {
  if (!log) return null;

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text);
  };

  const allFields = Object.keys(log).filter((key) => key !== 'key');
  const standardFields = [
    'asctime',
    'name',
    'levelname',
    'message',
    'pathname',
    'lineno',
    'version',
    'request_id',
  ];
  const extraFields = allFields.filter((key) => !standardFields.includes(key));

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className='max-h-[90vh] w-[95vw] max-w-[95vw] overflow-hidden'>
        <DialogHeader>
          <div className='flex items-center justify-between'>
            <DialogTitle className='flex items-center gap-2'>
              <Badge variant='outline' className={getLevelColor(log.levelname)}>
                {log.levelname}
              </Badge>
              <span>Log Entry Details</span>
            </DialogTitle>
            <Button
              variant='ghost'
              size='sm'
              onClick={() => copyToClipboard(JSON.stringify(log, null, 2))}
              className='h-8 w-8 p-0'
            >
              <Copy className='h-4 w-4' />
            </Button>
          </div>
          <DialogDescription>
            {log.asctime} • {log.name} • {log.pathname}:{log.lineno}
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className='h-[75vh] w-full overflow-x-auto'>
          <div className='space-y-6'>
            <div>
              <h4 className='mb-2 text-sm font-medium'>Message</h4>
              <div className='bg-muted rounded-md p-3'>
                <pre className='font-mono text-sm whitespace-pre-wrap'>
                  {log.message}
                </pre>
              </div>
            </div>

            <div>
              <h4 className='mb-3 text-sm font-medium'>Standard Fields</h4>
              <div className='grid grid-cols-1 gap-3'>
                {standardFields.map((field) => (
                  <div key={field} className='flex flex-col space-y-1'>
                    <span className='text-muted-foreground text-xs font-medium uppercase'>
                      {field}
                    </span>
                    <div className='bg-muted max-h-48 overflow-auto rounded p-2 font-mono text-sm'>
                      <div className='inline-block min-w-full whitespace-nowrap'>
                        {String(log[field as keyof LogEntry] || 'N/A')}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {extraFields.length > 0 && (
              <div>
                <h4 className='mb-3 text-sm font-medium'>Additional Fields</h4>
                <div className='grid grid-cols-1 gap-3'>
                  {extraFields.map((field) => (
                    <div key={field} className='flex flex-col space-y-1'>
                      <span className='text-muted-foreground text-xs font-medium uppercase'>
                        {field}
                      </span>
                      <div className='bg-muted max-h-48 overflow-auto rounded p-2 font-mono text-sm'>
                        {typeof log[field] === 'object' ? (
                          <pre className='inline-block min-w-full text-xs whitespace-pre'>
                            {JSON.stringify(log[field], null, 2)}
                          </pre>
                        ) : (
                          <div className='inline-block min-w-full whitespace-nowrap'>
                            {String(log[field] || 'N/A')}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div>
              <h4 className='mb-3 text-sm font-medium'>Raw JSON</h4>
              <div className='relative'>
                <Button
                  variant='ghost'
                  size='sm'
                  onClick={() => copyToClipboard(JSON.stringify(log, null, 2))}
                  className='absolute top-2 right-2 h-8 px-2'
                >
                  <Copy className='mr-1 h-3 w-3' />
                  Copy
                </Button>
                <pre className='bg-muted inline-block max-h-64 min-w-full overflow-auto rounded-md p-4 text-xs whitespace-pre'>
                  {JSON.stringify(log, null, 2)}
                </pre>
              </div>
            </div>
          </div>
        </ScrollArea>
      </DialogContent>
    </Dialog>
  );
}
