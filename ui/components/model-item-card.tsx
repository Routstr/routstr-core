import type { Model } from '@/lib/api/schemas/models';
import { formatCost } from '@/lib/services/cost-validation';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Ban, CheckCircle, Edit3, MoreVertical, Trash2 } from 'lucide-react';

interface ModelItemCardProps {
  model: Model;
  isSelected: boolean;
  hasEffectiveApiKey: boolean;
  hasIndividualSettings: boolean;
  onHoverStart: () => void;
  onHoverEnd: () => void;
  onToggleSelection: () => void;
  onEdit: () => void;
  onOverride: () => void;
  onDisable: () => void;
  onEnable: () => void;
  onDelete: () => void;
}

export function ModelItemCard({
  model,
  isSelected,
  hasEffectiveApiKey,
  hasIndividualSettings,
  onHoverStart,
  onHoverEnd,
  onToggleSelection,
  onEdit,
  onOverride,
  onDisable,
  onEnable,
  onDelete,
}: ModelItemCardProps) {
  const modelSourceLabel =
    model.api_key_type === 'remote' ? 'Remote' : 'Database';
  const keySourceLabel = !hasEffectiveApiKey
    ? 'No API key'
    : hasIndividualSettings
      ? 'Individual key'
      : 'Group key';
  const pricingLabel = model.is_free
    ? 'Free'
    : `Input ${formatCost(model.input_cost)} · Output ${formatCost(model.output_cost)}`;

  return (
    <Card
      className={cn(
        'relative overflow-hidden rounded-none border-0 bg-transparent py-0 shadow-none ring-0 transition-colors duration-150',
        'hover:bg-muted/25',
        isSelected && 'bg-primary/12',
        model.soft_deleted && 'bg-muted/20 opacity-80'
      )}
      onMouseEnter={onHoverStart}
      onMouseLeave={onHoverEnd}
    >
      <div className='flex items-center gap-2 px-2 py-1'>
        <div>
          <Checkbox
            checked={isSelected}
            onCheckedChange={onToggleSelection}
            onClick={(event) => event.stopPropagation()}
            className='border-border/90 data-checked:ring-primary/40 size-4 flex-shrink-0 data-checked:ring-2'
          />
        </div>

        <div className='min-w-0 flex-1 px-0.5 text-left'>
          <div className='grid min-w-0 items-center gap-x-3 gap-y-1 md:grid-cols-[minmax(170px,1fr)_72px_78px_92px_120px] lg:grid-cols-[minmax(170px,1fr)_72px_78px_92px_120px_240px]'>
            <div className='min-w-0'>
              <h3
                className={cn(
                  'min-w-0 truncate text-sm font-medium',
                  model.soft_deleted && 'text-muted-foreground'
                )}
              >
                {model.name}
              </h3>
            </div>

            <div className='hidden md:block'>
              <Badge
                variant={model.soft_deleted ? 'secondary' : 'outline'}
                className={cn(
                  'h-5 w-[64px] justify-center px-1 text-[10px] leading-none',
                  model.soft_deleted
                    ? 'text-muted-foreground'
                    : 'border-emerald-500/35 bg-emerald-500/10 text-emerald-500 dark:text-emerald-400'
                )}
              >
                {model.soft_deleted ? 'Disabled' : 'Enabled'}
              </Badge>
            </div>

            <span className='text-muted-foreground hidden text-xs capitalize md:block'>
              {model.modelType}
            </span>
            <span className='text-muted-foreground hidden text-xs md:block'>
              {modelSourceLabel}
            </span>
            <span
              className={cn(
                'text-muted-foreground hidden text-xs md:block',
                !hasEffectiveApiKey && 'text-destructive'
              )}
            >
              {keySourceLabel}
            </span>
            <span className='text-muted-foreground hidden truncate text-xs whitespace-nowrap lg:block'>
              {pricingLabel}
            </span>
          </div>

          <div className='text-muted-foreground mt-0.5 space-y-0.5 text-xs md:hidden'>
            <div className='flex min-w-0 items-center gap-1.5'>
              <Badge
                variant={model.soft_deleted ? 'secondary' : 'outline'}
                className={cn(
                  'h-5 shrink-0 px-1 text-[10px] leading-none',
                  model.soft_deleted
                    ? 'text-muted-foreground'
                    : 'border-emerald-500/35 bg-emerald-500/10 text-emerald-500 dark:text-emerald-400'
                )}
              >
                {model.soft_deleted ? 'Disabled' : 'Enabled'}
              </Badge>
              <span className='opacity-50'>•</span>
              <span className='capitalize'>{model.modelType}</span>
              <span className='opacity-50'>•</span>
              <span>{modelSourceLabel}</span>
            </div>
            <div className='flex min-w-0 items-center gap-1.5'>
              <span
                className={cn(
                  'min-w-0 flex-1 truncate',
                  !hasEffectiveApiKey && 'text-destructive'
                )}
              >
                {keySourceLabel}
              </span>
              <span className='opacity-50'>•</span>
              <span className='min-w-0 flex-1 truncate'>{pricingLabel}</span>
            </div>
          </div>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant='ghost'
              size='icon-xs'
              onClick={(event) => event.stopPropagation()}
              className='hover:bg-muted/50 dark:hover:bg-muted/80 h-7 w-7 shrink-0 md:h-6 md:w-6'
              aria-label={`Model actions for ${model.name}`}
              title={`Model actions for ${model.name}`}
            >
              <MoreVertical className='text-muted-foreground hover:text-foreground h-4 w-4' />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align='end' className='w-52'>
            {model.api_key_type !== 'remote' && (
              <DropdownMenuItem
                onClick={(event) => {
                  event.stopPropagation();
                  onEdit();
                }}
              >
                <Edit3 className='mr-2 h-4 w-4' />
                Edit Model
              </DropdownMenuItem>
            )}
            {model.api_key_type === 'remote' && (
              <DropdownMenuItem
                onClick={(event) => {
                  event.stopPropagation();
                  onOverride();
                }}
              >
                <Edit3 className='mr-2 h-4 w-4' />
                Override
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            {model.soft_deleted ? (
              <DropdownMenuItem
                onClick={(event) => {
                  event.stopPropagation();
                  onEnable();
                }}
                className='text-foreground'
              >
                <CheckCircle className='mr-2 h-4 w-4' />
                Enable Model
              </DropdownMenuItem>
            ) : (
              <DropdownMenuItem
                onClick={(event) => {
                  event.stopPropagation();
                  onDisable();
                }}
                className='text-foreground'
              >
                <Ban className='mr-2 h-4 w-4' />
                Disable Model
              </DropdownMenuItem>
            )}
            <DropdownMenuItem
              onClick={(event) => {
                event.stopPropagation();
                onDelete();
              }}
              className='text-destructive focus:text-destructive'
            >
              <Trash2 className='mr-2 h-4 w-4' />
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </Card>
  );
}
