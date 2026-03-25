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
import { getLogLevelBadgeVariant } from '@/lib/utils/log-level';
import type { LogEntry } from './types';

interface LogDetailsDialogProps {
  log: LogEntry | null;
  isOpen: boolean;
  onClose: () => void;
}

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
      <DialogContent className='max-h-[92svh] w-full max-w-none overflow-hidden md:max-w-4xl'>
        <DialogHeader>
          <DialogTitle className='flex items-center gap-2'>
            <Badge
              variant={getLogLevelBadgeVariant(log.levelname)}
              className='uppercase'
            >
              {log.levelname}
            </Badge>
            <span>Log Entry Details</span>
          </DialogTitle>
          <DialogDescription>
            {log.asctime} • {log.name} • {log.pathname}:{log.lineno}
          </DialogDescription>
        </DialogHeader>

        <ScrollArea className='h-[70svh] w-full overflow-x-auto sm:h-[75vh]'>
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
