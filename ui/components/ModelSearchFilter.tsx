'use client';

import React, { useState, useMemo } from 'react';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { Search, Filter, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Model } from '@/lib/api/schemas/models';

interface ModelSearchFilterProps {
  models: Model[];
  onFilteredModelsChange: (filteredModels: Model[]) => void;
  className?: string;
}

type SortOption = 'name-asc' | 'name-desc' | 'price-asc' | 'price-desc';

export function ModelSearchFilter({
  models,
  onFilteredModelsChange,
  className,
}: ModelSearchFilterProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [sortOption, setSortOption] = useState<SortOption>('name-asc');

  const filteredAndSortedModels = useMemo(() => {
    let filtered = [...models];

    // Apply search filter
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase().trim();
      filtered = filtered.filter((model) => {
        return (
          model.name.toLowerCase().includes(query) ||
          model.full_name.toLowerCase().includes(query) ||
          model.provider.toLowerCase().includes(query) ||
          (model.alias_ids &&
            model.alias_ids.some((alias) =>
              alias.toLowerCase().includes(query)
            )) ||
          (model.description &&
            model.description.toLowerCase().includes(query)) ||
          model.modelType.toLowerCase().includes(query)
        );
      });
    }

    // Apply sorting
    filtered.sort((a, b) => {
      switch (sortOption) {
        case 'name-asc':
          return a.name.localeCompare(b.name);
        case 'name-desc':
          return b.name.localeCompare(a.name);
        case 'price-asc': {
          const aPrice = a.is_free
            ? 0
            : (a.input_cost || 0) + (a.output_cost || 0);
          const bPrice = b.is_free
            ? 0
            : (b.input_cost || 0) + (b.output_cost || 0);
          return aPrice - bPrice;
        }
        case 'price-desc': {
          const aPrice = a.is_free
            ? 0
            : (a.input_cost || 0) + (a.output_cost || 0);
          const bPrice = b.is_free
            ? 0
            : (b.input_cost || 0) + (b.output_cost || 0);
          return bPrice - aPrice;
        }
        default:
          return 0;
      }
    });

    return filtered;
  }, [models, searchQuery, sortOption]);

  // Notify parent component when filtered models change
  React.useEffect(() => {
    onFilteredModelsChange(filteredAndSortedModels);
  }, [filteredAndSortedModels, onFilteredModelsChange]);

  const clearFilters = () => {
    setSearchQuery('');
    setSortOption('name-asc');
  };

  const hasActiveFilters =
    searchQuery.trim() !== '' || sortOption !== 'name-asc';

  return (
    <div
      className={cn(
        'flex flex-col gap-4 sm:flex-row sm:items-center sm:gap-3',
        className
      )}
    >
      <div className='relative flex-1'>
        <Search className='text-muted-foreground absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2' />
        <Input
          placeholder='Search models by name, provider, or description...'
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className='pl-9'
        />
      </div>

      <div className='flex items-center gap-2'>
        <Select
          value={sortOption}
          onValueChange={(value: SortOption) => setSortOption(value)}
        >
          <SelectTrigger className='w-full sm:w-[180px]'>
            <Filter className='mr-2 h-4 w-4' />
            <SelectValue placeholder='Sort by' />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value='name-asc'>Name (A-Z)</SelectItem>
            <SelectItem value='name-desc'>Name (Z-A)</SelectItem>
            <SelectItem value='price-asc'>Price (Low to High)</SelectItem>
            <SelectItem value='price-desc'>Price (High to Low)</SelectItem>
          </SelectContent>
        </Select>

        {hasActiveFilters && (
          <Button
            variant='outline'
            size='icon'
            onClick={clearFilters}
            className='h-9 w-9'
            title='Clear filters'
          >
            <X className='h-4 w-4' />
          </Button>
        )}
      </div>
    </div>
  );
}
