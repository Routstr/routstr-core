import type { Model } from '@/lib/api/schemas/models';
import type { DisplayUnit } from '@/lib/types/units';
import { formatUsdAmountForDisplayUnit } from '@/lib/currency';
import { cn } from '@/lib/utils';
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
import {
  ArrowRight,
  AudioLines,
  Ban,
  CheckCircle,
  Edit3,
  FileText,
  ImageIcon,
  Layers3,
  MoreVertical,
  Trash2,
  Type,
  Video,
  Waypoints,
} from 'lucide-react';

interface ModelItemCardProps {
  model: Model;
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
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

type ModelModality =
  | 'text'
  | 'image'
  | 'file'
  | 'audio'
  | 'video'
  | 'embedding'
  | 'multimodal';

const MODALITY_ORDER: ModelModality[] = [
  'text',
  'image',
  'file',
  'audio',
  'video',
  'embedding',
  'multimodal',
];

function extractModalities(part: string): ModelModality[] {
  const normalized = part.toLowerCase();
  const modalities = MODALITY_ORDER.filter((modality) =>
    normalized.includes(modality)
  );

  return modalities.length > 0 ? modalities : ['text'];
}

function getModelTypeParts(modelType: string): {
  inputs: ModelModality[];
  outputs: ModelModality[];
} {
  const [inputPart, outputPart] = modelType
    .split('->')
    .map((part) => part.trim());

  return {
    inputs: extractModalities(inputPart || modelType),
    outputs: outputPart ? extractModalities(outputPart) : [],
  };
}

function ModelTypeIcons({ modelType }: { modelType: string }) {
  const { inputs, outputs } = getModelTypeParts(modelType);

  const renderIcon = (modality: ModelModality, index: number) => {
    const props = {
      className: 'h-3 w-3 shrink-0',
      'aria-hidden': true as const,
    };

    switch (modality) {
      case 'image':
        return <ImageIcon key={`${modality}-${index}`} {...props} />;
      case 'file':
        return <FileText key={`${modality}-${index}`} {...props} />;
      case 'audio':
        return <AudioLines key={`${modality}-${index}`} {...props} />;
      case 'video':
        return <Video key={`${modality}-${index}`} {...props} />;
      case 'embedding':
        return <Waypoints key={`${modality}-${index}`} {...props} />;
      case 'multimodal':
        return <Layers3 key={`${modality}-${index}`} {...props} />;
      case 'text':
      default:
        return <Type key={`${modality}-${index}`} {...props} />;
    }
  };

  return (
    <span
      className='text-muted-foreground inline-flex max-w-full min-w-0 items-center gap-0.5 overflow-hidden'
      title={modelType}
      aria-label={modelType}
    >
      {inputs.map(renderIcon)}
      {outputs.length > 0 && (
        <>
          <ArrowRight className='h-2.5 w-2.5 shrink-0 opacity-55' aria-hidden />
          {outputs.map(renderIcon)}
        </>
      )}
      <span className='sr-only'>{modelType}</span>
    </span>
  );
}

export function ModelItemCard({
  model,
  displayUnit,
  usdPerSat,
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
  const isDisabled = Boolean(model.soft_deleted);
  const statusLabel = isDisabled ? 'Disabled' : 'Enabled';
  const isFreeModel = model.is_free;
  const statusDotClass = cn(
    'inline-block h-2.5 w-2.5 shrink-0 rounded-full',
    isDisabled
      ? 'bg-muted-foreground/50'
      : 'bg-emerald-500 shadow-[0_0_0_3px_rgba(16,185,129,0.14)]'
  );
  const modelSourceLabel =
    model.api_key_type === 'remote' ? 'Remote' : 'Database';
  const keySourceLabel = !hasEffectiveApiKey
    ? 'No API key'
    : hasIndividualSettings
      ? 'Individual key'
      : 'Group key';
  const inputPriceLabel = formatUsdAmountForDisplayUnit(
    model.input_cost,
    displayUnit,
    usdPerSat
  );
  const outputPriceLabel = formatUsdAmountForDisplayUnit(
    model.output_cost,
    displayUnit,
    usdPerSat
  );
  const pricingLabel = isFreeModel
    ? 'Free'
    : `Input ${inputPriceLabel} · Output ${outputPriceLabel}`;
  const renderActionsMenu = (className: string) => (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant='ghost'
          size='icon-xs'
          onClick={(event) => event.stopPropagation()}
          className={className}
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
  );

  return (
    <Card
      className={cn(
        "after:bg-border/80 relative overflow-hidden rounded-none border-0 bg-transparent py-0 shadow-none ring-0 transition-colors duration-150 after:absolute after:right-3 after:bottom-0 after:left-3 after:h-px after:content-[''] last:after:hidden md:after:hidden",
        'hover:bg-muted/25',
        isSelected && 'bg-primary/12',
        model.soft_deleted && 'bg-muted/20 opacity-80'
      )}
      onMouseEnter={onHoverStart}
      onMouseLeave={onHoverEnd}
    >
      <div className='px-2 py-2 md:flex md:items-center md:gap-2 md:py-1'>
        <div className='hidden md:flex md:items-center'>
          <Checkbox
            checked={isSelected}
            onCheckedChange={onToggleSelection}
            onClick={(event) => event.stopPropagation()}
            className='border-border/90 data-checked:ring-primary/40 size-4 flex-shrink-0 data-checked:ring-2'
          />
        </div>

        <div className='min-w-0 flex-1 px-0.5 text-left'>
          <div className='hidden min-w-0 items-center gap-x-3 gap-y-1 md:grid md:grid-cols-[minmax(210px,1.45fr)_112px_96px_120px] lg:grid-cols-[minmax(230px,1.65fr)_128px_96px_120px_minmax(220px,1fr)]'>
            <div className='min-w-0'>
              <div
                className='flex min-w-0 items-center gap-2'
                title={statusLabel}
                aria-label={statusLabel}
              >
                <span className={statusDotClass} />
                <h3
                  className={cn(
                    'min-w-0 truncate text-sm font-medium',
                    model.soft_deleted && 'text-muted-foreground'
                  )}
                  title={model.name}
                >
                  {model.name}
                </h3>
                <span className='sr-only'>{statusLabel}</span>
              </div>
            </div>

            <span
              className='hidden min-w-0 overflow-hidden md:flex md:items-center'
              title={model.modelType}
            >
              <ModelTypeIcons modelType={model.modelType} />
            </span>
            <span
              className='text-muted-foreground hidden truncate text-xs md:block'
              title={modelSourceLabel}
            >
              {modelSourceLabel}
            </span>
            <span
              className={cn(
                'text-muted-foreground hidden truncate text-xs md:block',
                !hasEffectiveApiKey && 'text-destructive'
              )}
              title={keySourceLabel}
            >
              {keySourceLabel}
            </span>
            <span
              className='text-muted-foreground hidden truncate text-right text-xs whitespace-nowrap lg:block'
              title={pricingLabel}
            >
              {pricingLabel}
            </span>
          </div>

          <div className='grid grid-cols-[16px_minmax(0,1fr)] gap-x-2.5 gap-y-2.5 md:hidden'>
            <Checkbox
              checked={isSelected}
              onCheckedChange={onToggleSelection}
              onClick={(event) => event.stopPropagation()}
              className='border-border/90 data-checked:ring-primary/40 mt-0.5 size-4 flex-shrink-0 self-start data-checked:ring-2'
            />
            <div className='grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-2'>
              <div
                className='flex min-w-0 items-center gap-2.5'
                title={statusLabel}
                aria-label={statusLabel}
              >
                <span className={statusDotClass} />
                <h3
                  className={cn(
                    'line-clamp-2 min-w-0 flex-1 text-[15px] leading-5 font-medium',
                    model.soft_deleted && 'text-muted-foreground'
                  )}
                  title={model.name}
                >
                  {model.name}
                </h3>
                <span className='sr-only'>{statusLabel}</span>
              </div>
              {renderActionsMenu(
                'hover:bg-muted/50 dark:hover:bg-muted/80 -mr-1 h-7 w-7 shrink-0 self-start'
              )}
            </div>

            <div aria-hidden />
            <div className='text-muted-foreground flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]'>
              <span
                className='text-foreground/80 inline-flex items-center'
                title={model.modelType}
              >
                <ModelTypeIcons modelType={model.modelType} />
              </span>
              <span className='text-border/80' aria-hidden>
                ·
              </span>
              <span className='inline-flex items-center'>
                {modelSourceLabel}
              </span>
              <span
                className={cn(
                  'inline-flex items-center',
                  !hasEffectiveApiKey && 'text-destructive'
                )}
              >
                <span className='text-border/80 mr-2' aria-hidden>
                  ·
                </span>
                {keySourceLabel}
              </span>
            </div>

            <div aria-hidden />
            {isFreeModel ? (
              <div className='text-muted-foreground text-sm font-medium'>
                Free
              </div>
            ) : (
              <div className='border-border/45 bg-background/20 grid grid-cols-2 overflow-hidden rounded-lg border'>
                <div className='px-3 py-2.5'>
                  <div className='text-muted-foreground text-[11px]'>Input</div>
                  <div className='pt-0.5 text-[15px] font-medium tabular-nums'>
                    {inputPriceLabel}
                  </div>
                </div>
                <div className='border-border/45 border-l px-3 py-2.5'>
                  <div className='text-muted-foreground text-[11px]'>
                    Output
                  </div>
                  <div className='pt-0.5 text-[15px] font-medium tabular-nums'>
                    {outputPriceLabel}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className='hidden md:block'>
          {renderActionsMenu(
            'hover:bg-muted/50 dark:hover:bg-muted/80 h-7 w-7 shrink-0 md:h-6 md:w-6'
          )}
        </div>
      </div>
    </Card>
  );
}
