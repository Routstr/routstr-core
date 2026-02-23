import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Badge } from '@/components/ui/badge';
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command';
import { Checkbox } from '@/components/ui/checkbox';
import { Plus, X } from 'lucide-react';

interface QuickFilterOption {
  label: string;
  checked: boolean;
  onSelect: () => void;
}

interface MultiSelectCommandFilterProps {
  label: string;
  emptyLabel: string;
  selectedValues: string[];
  onSelectedValuesChange: (values: string[]) => void;
  options: string[];
  searchValue: string;
  onSearchValueChange: (value: string) => void;
  searchPlaceholder: string;
  popoverClassName?: string;
  selectedGroupLabel?: string;
  customGroupLabel?: string;
  quickGroupLabel?: string;
  optionsGroupLabel?: string;
  quickFilters?: QuickFilterOption[];
  normalizeCustomValue?: (value: string) => string;
  canAddCustom?: (value: string) => boolean;
}

function FilterBadge({ value }: { value: string }) {
  return (
    <Badge variant='secondary' className='flex items-center gap-1 px-1 font-normal'>
      {value}
      <X className='h-3 w-3 opacity-70' aria-hidden='true' />
    </Badge>
  );
}

export function MultiSelectCommandFilter({
  label,
  emptyLabel,
  selectedValues,
  onSelectedValuesChange,
  options,
  searchValue,
  onSearchValueChange,
  searchPlaceholder,
  popoverClassName = 'w-64 p-0',
  selectedGroupLabel = 'Selected',
  customGroupLabel = 'Custom',
  quickGroupLabel = 'Quick Filters',
  optionsGroupLabel = 'Options',
  quickFilters = [],
  normalizeCustomValue,
  canAddCustom,
}: MultiSelectCommandFilterProps) {
  const toggleSelection = (value: string) => {
    if (selectedValues.includes(value)) {
      onSelectedValuesChange(selectedValues.filter((item) => item !== value));
      return;
    }

    onSelectedValuesChange([...selectedValues, value]);
  };

  const normalizedSearch = normalizeCustomValue
    ? normalizeCustomValue(searchValue)
    : searchValue;

  const canShowCustomAction =
    normalizedSearch.length > 0 &&
    !options.includes(normalizedSearch) &&
    !selectedValues.includes(normalizedSearch) &&
    (canAddCustom ? canAddCustom(normalizedSearch) : true);

  return (
    <div className='space-y-2'>
      <Label>{label}</Label>
      <Popover>
        <PopoverTrigger asChild>
          <Button variant='outline' className='w-full justify-start text-left font-normal'>
            <div className='flex flex-wrap gap-1 overflow-hidden'>
              {selectedValues.length > 0 ? (
                selectedValues.map((value) => (
                  <FilterBadge key={value} value={value} />
                ))
              ) : (
                <span className='text-muted-foreground'>{emptyLabel}</span>
              )}
            </div>
          </Button>
        </PopoverTrigger>
        <PopoverContent className={popoverClassName} align='start'>
          <Command>
            <CommandInput
              placeholder={searchPlaceholder}
              value={searchValue}
              onValueChange={onSearchValueChange}
            />
            <CommandList>
              {selectedValues.length > 0 && (
                <CommandGroup heading={selectedGroupLabel}>
                  {selectedValues.map((value) => (
                    <CommandItem
                      key={`selected-${value}`}
                      onSelect={() => toggleSelection(value)}
                    >
                      <Checkbox checked={true} className='mr-2' />
                      {value}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}

              {canShowCustomAction && (
                <CommandGroup heading={customGroupLabel}>
                  <CommandItem
                    onSelect={() => {
                      toggleSelection(normalizedSearch);
                      onSearchValueChange('');
                    }}
                  >
                    <Plus className='mr-2 h-4 w-4' />
                    Add &quot;{normalizedSearch}&quot;
                  </CommandItem>
                </CommandGroup>
              )}

              <CommandEmpty>No results found.</CommandEmpty>

              {quickFilters.length > 0 && (
                <CommandGroup heading={quickGroupLabel}>
                  {quickFilters.map((filter) => (
                    <CommandItem key={filter.label} onSelect={filter.onSelect}>
                      <Checkbox checked={filter.checked} className='mr-2' />
                      {filter.label}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}

              <CommandGroup heading={optionsGroupLabel}>
                {options
                  .filter((option) => !selectedValues.includes(option))
                  .map((option) => (
                    <CommandItem key={option} onSelect={() => toggleSelection(option)}>
                      <Checkbox checked={false} className='mr-2' />
                      {option}
                    </CommandItem>
                  ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
    </div>
  );
}
