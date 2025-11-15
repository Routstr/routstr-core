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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Calendar, Filter } from 'lucide-react';
import { useState, useEffect } from 'react';

interface LogFiltersProps {
  selectedDate: string;
  selectedLevel: string;
  requestId: string;
  searchText: string;
  limit: number;
  availableDates: string[];
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
  availableDates,
  onDateChange,
  onLevelChange,
  onRequestIdChange,
  onSearchTextChange,
  onLimitChange,
  onClearFilters,
}: LogFiltersProps) {
  const isPreset = PRESET_LIMITS.includes(limit.toString());

  const [customLimit, setCustomLimit] = useState<string>(
    isPreset ? '' : limit.toString()
  );
  const [isCustom, setIsCustom] = useState<boolean>(!isPreset);

  useEffect(() => {
    const currentIsPreset = PRESET_LIMITS.includes(limit.toString());
    setIsCustom(!currentIsPreset);
    if (!currentIsPreset) {
      setCustomLimit(limit.toString());
    }
  }, [limit]);

  const handleLimitChange = (value: string) => {
    if (value === 'custom') {
      setIsCustom(true);
      setCustomLimit(limit.toString());
    } else {
      setIsCustom(false);
      setCustomLimit('');
      onLimitChange(Number(value));
    }
  };

  const handleCustomLimitChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setCustomLimit(value);
  };

  const handleCustomLimitApply = () => {
    const numValue = parseInt(customLimit);
    if (!isNaN(numValue) && numValue > 0) {
      onLimitChange(numValue);
    } else {
      setIsCustom(false);
      setCustomLimit('');
      onLimitChange(100);
    }
  };

  const handleCustomLimitKeyDown = (
    e: React.KeyboardEvent<HTMLInputElement>
  ) => {
    if (e.key === 'Enter') {
      handleCustomLimitApply();
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
            <Select value={selectedDate} onValueChange={onDateChange}>
              <SelectTrigger>
                <Calendar className='mr-2 h-4 w-4' />
                <SelectValue placeholder='Select date' />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value='all'>All dates</SelectItem>
                {availableDates.map((date) => (
                  <SelectItem key={date} value={date}>
                    {date}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
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
              onChange={(e) => onRequestIdChange(e.target.value)}
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
              placeholder='Search in message and name'
              value={searchText}
              onChange={(e) => onSearchTextChange(e.target.value)}
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
                value={isPreset ? limit.toString() : 'custom'}
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
            <Label>&nbsp;</Label>
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
