import { Badge } from '@/components/ui/badge';
import type { AdminModel } from '@/lib/api/services/admin';
import type { ReactNode } from 'react';

interface ProviderModelRowProps {
  model: AdminModel;
  showEnabledState?: boolean;
  actions?: ReactNode;
}

export function ProviderModelRow({
  model,
  showEnabledState = false,
  actions,
}: ProviderModelRowProps) {
  return (
    <div className='hover:bg-accent flex flex-col gap-2 rounded-lg border p-3 transition-colors sm:flex-row sm:items-center sm:justify-between'>
      <div className='min-w-0 flex-1'>
        <div className='flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2'>
          <span className='truncate font-mono text-sm font-medium'>
            {model.id}
          </span>
          {showEnabledState && (
            <Badge
              variant={model.enabled ? 'default' : 'secondary'}
              className='w-fit text-xs'
            >
              {model.enabled ? 'Enabled' : 'Disabled'}
            </Badge>
          )}
        </div>
        <div className='text-muted-foreground mt-1 text-xs break-words'>
          {model.description || model.name}
        </div>
      </div>
      <div className='flex flex-wrap items-center gap-2 sm:flex-nowrap'>
        <span className='text-muted-foreground text-xs'>
          {model.context_length
            ? `${model.context_length.toLocaleString()} tokens`
            : '-'}
        </span>
        {actions}
      </div>
    </div>
  );
}
