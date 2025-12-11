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
import { Copy, Check } from 'lucide-react';
import { useState } from 'react';

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
  const [copiedField, setCopiedField] = useState<string | null>(null);

  if (!log) return null;

  const copyToClipboard = (text: string, fieldName?: string) => {
    navigator.clipboard.writeText(text);
    if (fieldName) {
      setCopiedField(fieldName);
      setTimeout(() => setCopiedField(null), 2000);
    }
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
          <DialogTitle className='flex items-center gap-2'>
            <Badge variant='outline' className={getLevelColor(log.levelname)}>
              {log.levelname}
            </Badge>
            <span>Log Entry Details</span>
          </DialogTitle>
          <DialogDescription>
            {log.asctime} • {log.name} • {log.pathname}:{log.lineno}
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className='h-[75vh] w-full overflow-x-auto'>
          <div className='space-y-6'>
            <div>
              <h4 className='mb-2 text-sm font-medium'>Message</h4>
              <div className='bg-muted max-h-48 overflow-auto rounded-md p-3'>
                <pre className='font-mono text-sm break-all whitespace-pre'>
                  {log.message}
                </pre>
              </div>
            </div>

            <div>
              <h4 className='mb-3 text-sm font-medium'>Standard Fields</h4>
              <div className='grid grid-cols-1 gap-3'>
                {standardFields.map((field) => (
                  <div key={field} className='flex flex-col space-y-1'>
                    <div className='flex items-center justify-between gap-2'>
                      <span className='text-muted-foreground text-xs font-medium uppercase'>
                        {field}
                      </span>
                      {field === 'request_id' && (
                        <Button
                          variant='outline'
                          size='sm'
                          onClick={() =>
                            copyToClipboard(
                              String(log[field as keyof LogEntry] || ''),
                              field
                            )
                          }
                          className='h-6 flex-shrink-0 px-2'
                        >
                          {copiedField === field ? (
                            <>
                              <Check className='mr-1 h-3 w-3' />
                              Copied
                            </>
                          ) : (
                            <>
                              <Copy className='mr-1 h-3 w-3' />
                              Copy
                            </>
                          )}
                        </Button>
                      )}
                    </div>
                    <div className='bg-muted max-h-32 overflow-auto rounded p-2'>
                      <pre className='font-mono text-sm break-all whitespace-pre-wrap'>
                        {String(log[field as keyof LogEntry] || 'N/A')}
                      </pre>
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
                      <span className='text-muted-foreground truncate text-xs font-medium uppercase'>
                        {field}
                      </span>
                      <div className='bg-muted max-h-48 overflow-auto rounded p-2'>
                        {typeof log[field] === 'object' ? (
                          <pre className='font-mono text-xs break-all whitespace-pre-wrap'>
                            {JSON.stringify(log[field], null, 2)}
                          </pre>
                        ) : (
                          <pre className='font-mono text-sm break-all whitespace-pre-wrap'>
                            {String(log[field] || 'N/A')}
                          </pre>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div>
              <div className='mb-3 flex items-center justify-between'>
                <h4 className='text-sm font-medium'>Raw JSON</h4>
                <Button
                  variant='outline'
                  size='sm'
                  onClick={() =>
                    copyToClipboard(JSON.stringify(log, null, 2), 'json')
                  }
                  className='h-6 px-2'
                >
                  {copiedField === 'json' ? (
                    <>
                      <Check className='mr-1 h-3 w-3' />
                      Copied
                    </>
                  ) : (
                    <>
                      <Copy className='mr-1 h-3 w-3' />
                      Copy
                    </>
                  )}
                </Button>
              </div>
              <div className='bg-muted max-h-64 overflow-auto rounded-md p-4'>
                <pre className='text-xs break-all whitespace-pre-wrap'>
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
