import { useEffect, useState, type ChangeEvent, type KeyboardEvent } from 'react';
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover';
import { Calendar } from '@/components/ui/calendar';
import { cn } from '@/lib/utils';
import { MultiSelectCommandFilter } from './multi-select-command-filter';

interface LogFiltersProps {
  selectedDate: string;
  selectedLevel: string;
  requestId: string;
  searchText: string;
  selectedStatusCodes: string[];
  selectedMethods: string[];
  selectedEndpoints: string[];
  limit: number;
  onDateChange: (date: string) => void;
  onLevelChange: (level: string) => void;
  onRequestIdChange: (requestId: string) => void;
  onSearchTextChange: (searchText: string) => void;
  onStatusCodesChange: (statusCodes: string[]) => void;
  onMethodsChange: (methods: string[]) => void;
  onEndpointsChange: (endpoints: string[]) => void;
  onLimitChange: (limit: number) => void;
  onClearFilters: () => void;
}

const LOG_LEVELS = ['TRACE', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];
const PRESET_LIMITS = ['25', '50', '100', '200', '500', '1000'];

const STATUS_CODE_OPTIONS = [
  '200',
  '201',
  '204',
  '400',
  '401',
  '402',
  '403',
  '404',
  '422',
  '429',
  '500',
  '502',
  '503',
  '504',
];

const METHOD_OPTIONS = [
  'GET',
  'POST',
  'PUT',
  'DELETE',
  'PATCH',
  'OPTIONS',
  'HEAD',
];

const ENDPOINT_OPTIONS = [
  '/chat/completions',
  '/v1/chat/completions',
  '/models',
  '/v1/models',
  '/responses',
  '/v1/responses',
  'v1/embeddings/models',
  '/embeddings/models',
];

const STATUS_4XX_CODES = STATUS_CODE_OPTIONS.filter((code) => code.startsWith('4'));
const STATUS_5XX_CODES = STATUS_CODE_OPTIONS.filter((code) => code.startsWith('5'));

export function LogFilters({
  selectedDate,
  selectedLevel,
  requestId,
  searchText,
  selectedStatusCodes,
  selectedMethods,
  selectedEndpoints,
  limit,
  onDateChange,
  onLevelChange,
  onRequestIdChange,
  onSearchTextChange,
  onStatusCodesChange,
  onMethodsChange,
  onEndpointsChange,
  onLimitChange,
  onClearFilters,
}: LogFiltersProps) {
  const isPreset = PRESET_LIMITS.includes(limit.toString());

  const [customLimit, setCustomLimit] = useState<string>(
    isPreset ? '' : limit.toString()
  );
  const [isCustom, setIsCustom] = useState<boolean>(!isPreset);
  const [date, setDate] = useState<Date | undefined>(
    selectedDate && selectedDate !== 'all'
      ? new Date(`${selectedDate}T00:00:00`)
      : undefined
  );

  const [statusSearch, setStatusSearch] = useState('');
  const [methodSearch, setMethodSearch] = useState('');
  const [endpointSearch, setEndpointSearch] = useState('');

  useEffect(() => {
    const currentIsPreset = PRESET_LIMITS.includes(limit.toString());
    setIsCustom(!currentIsPreset);

    if (!currentIsPreset) {
      setCustomLimit(limit.toString());
    }
  }, [limit]);

  useEffect(() => {
    if (selectedDate === 'all' || !selectedDate) {
      setDate(undefined);
      return;
    }

    const parsedDate = new Date(`${selectedDate}T00:00:00`);
    setDate(Number.isNaN(parsedDate.getTime()) ? undefined : parsedDate);
  }, [selectedDate]);

  const handleLimitChange = (value: string) => {
    if (value === 'custom') {
      setIsCustom(true);
      setCustomLimit(limit.toString());
      return;
    }

    setIsCustom(false);
    setCustomLimit('');
    onLimitChange(Number(value));
  };

  const handleCustomLimitChange = (event: ChangeEvent<HTMLInputElement>) => {
    setCustomLimit(event.target.value);
  };

  const handleCustomLimitApply = () => {
    const numericValue = Number.parseInt(customLimit, 10);

    if (!Number.isNaN(numericValue) && numericValue > 0) {
      onLimitChange(numericValue);
      return;
    }

    setIsCustom(false);
    setCustomLimit('');
    onLimitChange(100);
  };

  const handleCustomLimitKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      handleCustomLimitApply();
    }
  };

  const handleDateSelect = (nextDate: Date | undefined) => {
    setDate(nextDate);

    if (nextDate) {
      onDateChange(format(nextDate, 'yyyy-MM-dd'));
      return;
    }

    onDateChange('all');
  };

  const handleQuickStatusCode = (range: '4xx' | '5xx') => {
    const rangeCodes = range === '4xx' ? STATUS_4XX_CODES : STATUS_5XX_CODES;
    const nextSelection = new Set(selectedStatusCodes);
    const allSelected = rangeCodes.every((code) => selectedStatusCodes.includes(code));

    if (allSelected) {
      rangeCodes.forEach((code) => nextSelection.delete(code));
    } else {
      rangeCodes.forEach((code) => nextSelection.add(code));
    }

    onStatusCodesChange(Array.from(nextSelection));
  };

  return (
    <Card className='mb-6'>
      <CardHeader>
        <CardTitle className='flex items-center gap-2'>
          <Filter className='h-5 w-5' />
          Filters
        </CardTitle>
        <CardDescription>
          Filter logs by date, level, request ID, text search, status code,
          method, endpoint and limit
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

          <MultiSelectCommandFilter
            label='Status Codes'
            emptyLabel='All codes'
            selectedValues={selectedStatusCodes}
            onSelectedValuesChange={onStatusCodesChange}
            options={STATUS_CODE_OPTIONS}
            searchValue={statusSearch}
            onSearchValueChange={setStatusSearch}
            searchPlaceholder='Search or add status code...'
            popoverClassName='w-[min(16rem,calc(100vw-2rem))] p-0'
            optionsGroupLabel='Common Codes'
            canAddCustom={(value) => /^\d+$/.test(value)}
            quickFilters={[
              {
                label: '4xx Errors',
                checked: STATUS_4XX_CODES.every((code) =>
                  selectedStatusCodes.includes(code)
                ),
                onSelect: () => handleQuickStatusCode('4xx'),
              },
              {
                label: '5xx Errors',
                checked: STATUS_5XX_CODES.every((code) =>
                  selectedStatusCodes.includes(code)
                ),
                onSelect: () => handleQuickStatusCode('5xx'),
              },
            ]}
          />

          <MultiSelectCommandFilter
            label='HTTP Methods'
            emptyLabel='All methods'
            selectedValues={selectedMethods}
            onSelectedValuesChange={onMethodsChange}
            options={METHOD_OPTIONS}
            searchValue={methodSearch}
            onSearchValueChange={setMethodSearch}
            searchPlaceholder='Search or add method...'
            popoverClassName='w-[min(16rem,calc(100vw-2rem))] p-0'
            optionsGroupLabel='Methods'
            normalizeCustomValue={(value) => value.toUpperCase()}
          />

          <MultiSelectCommandFilter
            label='Endpoints'
            emptyLabel='All endpoints'
            selectedValues={selectedEndpoints}
            onSelectedValuesChange={onEndpointsChange}
            options={ENDPOINT_OPTIONS}
            searchValue={endpointSearch}
            onSearchValueChange={setEndpointSearch}
            searchPlaceholder='Search or add endpoint pattern...'
            popoverClassName='w-[min(20rem,calc(100vw-2rem))] p-0'
            optionsGroupLabel='Common Endpoints'
          />

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
              placeholder='Search in message and name'
              value={searchText}
              onChange={(event) => onSearchTextChange(event.target.value)}
            />
          </div>

          <div className='space-y-2'>
            <Label htmlFor='limit'>Limit</Label>
            {isCustom ? (
              <div className='flex flex-col gap-2 sm:flex-row'>
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
                  className='flex-1 sm:flex-auto'
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
                  {PRESET_LIMITS.map((preset) => (
                    <SelectItem key={preset} value={preset}>
                      {preset}
                    </SelectItem>
                  ))}
                  <SelectItem value='custom'>Custom...</SelectItem>
                </SelectContent>
              </Select>
            )}
            {!isCustom && !isPreset && (
              <p className='text-muted-foreground text-xs'>Custom: {limit}</p>
            )}
          </div>

          <div className='flex items-end sm:col-span-2 lg:col-span-1'>
            <Button onClick={onClearFilters} variant='outline' className='w-full'>
              Clear Filters
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
