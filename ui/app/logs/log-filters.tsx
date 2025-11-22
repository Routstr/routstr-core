'use client';

import { useEffect, useState } from 'react';
import type { ChangeEvent, KeyboardEvent } from 'react';
import { format } from 'date-fns';
import { CalendarIcon, Filter, X } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Calendar } from '@/components/ui/calendar';
import { cn } from '@/lib/utils';

interface LogFiltersProps {
  selectedDate: string;
  selectedLevel: string;
  requestId: string;
  searchText: string;
  limit: number;
  onDateChange: (date: string) => void;
  onLevelChange: (level: string) => void;
  onRequestIdChange: (requestId: string) => void;
  onSearchTextChange: (searchText: string) => void;
  onLimitChange: (limit: number) => void;
  onClearFilters: () => void;
}

const LOG_LEVELS = ['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
const PRESET_LIMITS = ['25', '50', '100', '200', '500', '1000'];

export function LogFilters({
  selectedDate,
  selectedLevel,
  requestId,
  searchText,
  limit,
  onDateChange,
  onLevelChange,
  onRequestIdChange,
  onSearchTextChange,
  onLimitChange,
  onClearFilters,
}: LogFiltersProps) {
  const isPreset = PRESET_LIMITS.includes(String(limit));

  const [customLimit, setCustomLimit] = useState<string>(
    isPreset ? '' : String(limit)
  );
  const [isCustom, setIsCustom] = useState<boolean>(!isPreset);
  const [date, setDate] = useState<Date | undefined>(
    selectedDate && selectedDate !== 'all' ? new Date(selectedDate) : undefined
  );

  useEffect(() => {
    const preset = PRESET_LIMITS.includes(String(limit));
    setIsCustom(!preset);
    if (!preset) {
      setCustomLimit(String(limit));
    }
  }, [limit]);

  useEffect(() => {
    if (!selectedDate || selectedDate === 'all') {
      setDate(undefined);
      return;
    }
    try {
      setDate(new Date(selectedDate));
    } catch {
      setDate(undefined);
    }
  }, [selectedDate]);

  const handleLimitChange = (value: string) => {
    if (value === 'custom') {
      setIsCustom(true);
      setCustomLimit(String(limit));
    } else {
      setIsCustom(false);
      setCustomLimit('');
      onLimitChange(Number(value));
    }
  };

  const handleCustomLimitChange = (event: ChangeEvent<HTMLInputElement>) => {
    setCustomLimit(event.target.value);
  };

  const handleCustomLimitApply = () => {
    const parsed = parseInt(customLimit, 10);
    if (!Number.isNaN(parsed) && parsed > 0) {
      onLimitChange(parsed);
    } else {
      setIsCustom(false);
      setCustomLimit('');
      onLimitChange(100);
    }
  };

  const handleCustomLimitKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      handleCustomLimitApply();
    }
  };

  const handleDateSelect = (selected: Date | undefined) => {
    setDate(selected);
    if (selected) {
      onDateChange(format(selected, 'yyyy-MM-dd'));
    } else {
      onDateChange('all');
    }
  };

  return (
    <Card className='mb-6'>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <Filter className='h-5 w-5' />
          Filters
        </CardTitle>
        <CardDescription>
          Filter logs by date, level, request ID, text search, and limit
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className='grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3'>
          <div className='space-y-2'>
            <Label htmlFor='date'>Date</Label>
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant='outline'
                  className={cn(
                    'w-full justify-start text-left font-normal',
                    !date && 'text-muted-foreground'
                  )}
                >
                  <CalendarIcon className='mr-2 h-4 w-4' />
                  {date ? format(date, 'PPP') : <span>Pick a date</span>}
                </Button>
              </PopoverTrigger>
              <PopoverContent className='w-auto p-0' align='start'>
                <Calendar
                  mode='single'
                  selected={date}
                  onSelect={handleDateSelect}
                  initialFocus
                />
              </PopoverContent>
            </Popover>
            {date && (
              <Button
                type='button'
                variant='ghost'
                size='sm'
                onClick={() => handleDateSelect(undefined)}
                className='w-full'
              >
                <X className='mr-2 h-4 w-4' />
                Clear date
              </Button>
            )}
          </div>

          <div className='space-y-2'>
            <Label htmlFor='level'>Log Level</Label>
            <Select value={selectedLevel} onValueChange={onLevelChange}>
              <SelectTrigger>
                <SelectValue placeholder='Select level' />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value='all'>All levels</SelectItem>
                {LOG_LEVELS.map((level) => (
                  <SelectItem key={level} value={level}>
                    {level}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className='space-y-2'>
            <Label htmlFor='request-id'>Request ID</Label>
            <Input
              id='request-id'
              type='text'
              placeholder='Search by request ID'
              value={requestId}
              onChange={(event) => onRequestIdChange(event.target.value)}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='search-text' className='flex items-center gap-1'>
              <span>Text Search</span>
              <span className='text-muted-foreground text-xs font-normal'>
                (can be slow)
              </span>
            </Label>
            <Input
              id='search-text'
              type='text'
              placeholder='Search in message and logger'
              value={searchText}
              onChange={(event) => onSearchTextChange(event.target.value)}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='limit'>Limit</Label>
            {isCustom ? (
              <div className='flex gap-2'>
                <Input
                  id='limit'
                  type='number'
                  min='1'
                  placeholder='Enter custom limit'
                  value={customLimit}
                  onChange={handleCustomLimitChange}
                  onKeyDown={handleCustomLimitKeyDown}
                  onBlur={handleCustomLimitApply}
                  autoFocus
                  className='flex-1'
                />
                <Button
                  type='button'
                  variant='secondary'
                  size='sm'
                  onClick={() => {
                    setIsCustom(false);
                    setCustomLimit('');
                    if (!isPreset) {
                      onLimitChange(100);
                    }
                  }}
                >
                  Cancel
                </Button>
              </div>
            ) : (
              <Select
                value={isPreset ? String(limit) : 'custom'}
                onValueChange={handleLimitChange}
              >
                <SelectTrigger>
                  <SelectValue placeholder='Select limit' />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value='25'>25</SelectItem>
                  <SelectItem value='50'>50</SelectItem>
                  <SelectItem value='100'>100</SelectItem>
                  <SelectItem value='200'>200</SelectItem>
                  <SelectItem value='500'>500</SelectItem>
                  <SelectItem value='1000'>1000</SelectItem>
                  <SelectItem value='custom'>Custom...</SelectItem>
                </SelectContent>
              </Select>
            )}
            {!isCustom && !isPreset && (
              <p className='text-muted-foreground text-xs'>Custom: {limit}</p>
            )}
          </div>

          <div className='space-y-2'>
            <Label aria-hidden='true'>&nbsp;</Label>
            <Button
              onClick={onClearFilters}
              variant='outline'
              className='w-full'
            >
              Clear Filters
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
