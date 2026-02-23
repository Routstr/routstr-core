import type { Model } from '@/lib/api/schemas/models';
import type { AdminModelGroup } from '@/lib/api/services/admin';
import { ModelItemCard } from '@/components/model-item-card';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  CheckSquare,
  Edit3,
  Globe,
  Key,
  MoreVertical,
  RefreshCw,
  Users,
} from 'lucide-react';

interface ModelProviderSectionProps {
  provider: string;
  providerModels: Model[];
  filterProvider?: string;
  groupData?: AdminModelGroup;
  selectedModels: Set<string>;
  onSelectProviderModels: () => void;
  onDeselectProviderModels: () => void;
  onEditGroup: () => void;
  onRefreshProviderModels: () => void;
  onDeleteAllProviderModels: () => void;
  onModelHover: (modelId: string | null) => void;
  onModelToggleSelection: (modelId: string) => void;
  onEditModel: (model: Model) => void;
  onOverrideModel: (model: Model) => void;
  onEnableModel: (modelId: string) => void;
  onDisableModel: (modelId: string) => void;
  onDeleteModel: (modelId: string) => void;
  hasEffectiveApiKey: (model: Model) => boolean;
  hasIndividualSettings: (model: Model) => boolean;
}

export function ModelProviderSection({
  provider,
  providerModels,
  filterProvider,
  groupData,
  selectedModels,
  onSelectProviderModels,
  onDeselectProviderModels,
  onEditGroup,
  onRefreshProviderModels,
  onDeleteAllProviderModels,
  onModelHover,
  onModelToggleSelection,
  onEditModel,
  onOverrideModel,
  onEnableModel,
  onDisableModel,
  onDeleteModel,
  hasEffectiveApiKey,
  hasIndividualSettings,
}: ModelProviderSectionProps) {
  const allProviderSelected = providerModels.every((model) =>
    selectedModels.has(model.id)
  );
  const someProviderSelected = providerModels.some((model) =>
    selectedModels.has(model.id)
  );

  if (filterProvider) {
    return (
      <div className='bg-card/35 divide-border/55 border-border/60 divide-y overflow-hidden rounded-lg border'>
        {providerModels.map((model) => (
          <ModelItemCard
            key={model.id}
            model={model}
            isSelected={selectedModels.has(model.id)}
            hasEffectiveApiKey={hasEffectiveApiKey(model)}
            hasIndividualSettings={hasIndividualSettings(model)}
            onHoverStart={() => onModelHover(model.id)}
            onHoverEnd={() => onModelHover(null)}
            onToggleSelection={() => onModelToggleSelection(model.id)}
            onEdit={() => onEditModel(model)}
            onOverride={() => onOverrideModel(model)}
            onDisable={() => onDisableModel(model.id)}
            onEnable={() => onEnableModel(model.id)}
            onDelete={() => onDeleteModel(model.id)}
          />
        ))}
      </div>
    );
  }

  return (
    <Card className='overflow-hidden'>
      <CardHeader className='px-3 pb-2.5 sm:px-6 sm:pb-3'>
        <div className='flex items-start justify-between gap-2 sm:gap-3'>
          <div className='flex min-w-0 items-start gap-2.5 sm:gap-3'>
            <Checkbox
              checked={
                allProviderSelected
                  ? true
                  : someProviderSelected
                    ? 'indeterminate'
                    : false
              }
              onCheckedChange={(checked) => {
                if (checked === true) {
                  onSelectProviderModels();
                  return;
                }

                onDeselectProviderModels();
              }}
              className='border-border/90 data-checked:ring-primary/35 mt-0.5 size-4 data-checked:ring-2 sm:mt-1 sm:size-5'
              aria-label={`Select models for provider ${provider}`}
            />
            <div className='min-w-0'>
              <CardTitle className='flex items-center gap-1.5 text-base sm:gap-2 sm:text-lg'>
                <Users className='h-5 w-5' />
                <span className='truncate'>{provider}</span>
                <span className='text-muted-foreground text-sm font-normal'>
                  ({providerModels.length} models)
                </span>
              </CardTitle>
              <CardDescription className='mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs sm:text-sm'>
                {groupData?.group_url ? (
                  <span className='inline-flex items-center gap-1 break-all'>
                    <Globe className='h-3 w-3' />
                    {groupData.group_url}
                  </span>
                ) : (
                  'Using default endpoint'
                )}
                {groupData?.group_api_key && (
                  <span className='flex items-center gap-1'>
                    <Key className='h-3 w-3' />
                    Group API Key
                  </span>
                )}
              </CardDescription>
            </div>
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant='ghost'
                size='icon'
                className='h-7 w-7 sm:h-8 sm:w-8'
                aria-label={`Provider actions for ${provider}`}
                title={`Provider actions for ${provider}`}
              >
                <MoreVertical className='h-4 w-4' />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align='end' className='w-64 sm:w-72'>
              <DropdownMenuItem onClick={onEditGroup}>
                <Edit3 className='mr-2 h-4 w-4' />
                Edit Group
              </DropdownMenuItem>
              <DropdownMenuItem onClick={onSelectProviderModels}>
                <CheckSquare className='mr-2 h-4 w-4' />
                Select All
              </DropdownMenuItem>
              <DropdownMenuItem onClick={onRefreshProviderModels}>
                <RefreshCw className='mr-2 h-4 w-4' />
                Refresh Models
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={onDeleteAllProviderModels}
                className='text-muted-foreground focus:text-foreground'
              >
                Delete all overrides
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </CardHeader>

      <CardContent className='px-3 pt-0 pb-3 sm:px-6 sm:pb-6'>
        <div className='bg-card/35 divide-border/55 border-border/60 divide-y overflow-hidden rounded-lg border'>
          {providerModels.map((model) => (
            <ModelItemCard
              key={model.id}
              model={model}
              isSelected={selectedModels.has(model.id)}
              hasEffectiveApiKey={hasEffectiveApiKey(model)}
              hasIndividualSettings={hasIndividualSettings(model)}
              onHoverStart={() => onModelHover(model.id)}
              onHoverEnd={() => onModelHover(null)}
              onToggleSelection={() => onModelToggleSelection(model.id)}
              onEdit={() => onEditModel(model)}
              onOverride={() => onOverrideModel(model)}
              onDisable={() => onDisableModel(model.id)}
              onEnable={() => onEnableModel(model.id)}
              onDelete={() => onDeleteModel(model.id)}
            />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
