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
import { CalendarIcon, Filter, X, Plus } from 'lucide-react';
import { useState, useEffect } from 'react';
import { format } from 'date-fns';
import { cn } from '@/lib/utils';

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

interface FilterBadgeProps {
  value: string;
  onRemove: (value: string) => void;
}

function FilterBadge({ value, onRemove }: FilterBadgeProps) {
  return (
    <Badge
      variant='secondary'
      className='flex items-center gap-1 px-1 font-normal'
    >
      {value}
      <button
        type='button'
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onRemove(value);
        }}
        className='hover:bg-muted-foreground/20 rounded-full'
      >
        <X className='h-3 w-3' />
      </button>
    </Badge>
  );
}

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
      ? new Date(selectedDate + 'T00:00:00')
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
    } else {
      const d = new Date(selectedDate + 'T00:00:00');
      setDate(isNaN(d.getTime()) ? undefined : d);
    }
  }, [selectedDate]);

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

  const handleDateSelect = (selectedDate: Date | undefined) => {
    setDate(selectedDate);
    if (selectedDate) {
      onDateChange(format(selectedDate, 'yyyy-MM-dd'));
    } else {
      onDateChange('all');
    }
  };

  const toggleSelection = (
    current: string[],
    value: string,
    onChange: (val: string[]) => void
  ) => {
    if (current.includes(value)) {
      onChange(current.filter((v) => v !== value));
    } else {
      onChange([...current, value]);
    }
  };

  const handleQuickStatusCode = (range: '4xx' | '5xx') => {
    const codes = STATUS_CODE_OPTIONS.filter((c) => c.startsWith(range[0]));
    const newSelection = new Set([...selectedStatusCodes]);
    const allIncluded = codes.every((c) => selectedStatusCodes.includes(c));

    if (allIncluded) {
      codes.forEach((c) => newSelection.delete(c));
    } else {
      codes.forEach((c) => newSelection.add(c));
    }
    onStatusCodesChange(Array.from(newSelection));
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

          <div className='space-y-2'>
            <Label>Status Codes</Label>
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant='outline'
                  className='w-full justify-start text-left font-normal'
                >
                  <div className='flex flex-wrap gap-1'>
                    {selectedStatusCodes.length > 0 ? (
                      selectedStatusCodes.map((code) => (
                        <FilterBadge
                          key={code}
                          value={code}
                          onRemove={(val) =>
                            toggleSelection(
                              selectedStatusCodes,
                              val,
                              onStatusCodesChange
                            )
                          }
                        />
                      ))
                    ) : (
                      <span className='text-muted-foreground'>All codes</span>
                    )}
                  </div>
                </Button>
              </PopoverTrigger>
              <PopoverContent className='w-64 p-0' align='start'>
                <Command>
                  <CommandInput
                    placeholder='Search or add status code...'
                    value={statusSearch}
                    onValueChange={setStatusSearch}
                  />
                  <CommandList>
                    {selectedStatusCodes.length > 0 && (
                      <CommandGroup heading='Selected'>
                        {selectedStatusCodes.map((code) => (
                          <CommandItem
                            key={`selected-${code}`}
                            onSelect={() =>
                              toggleSelection(
                                selectedStatusCodes,
                                code,
                                onStatusCodesChange
                              )
                            }
                          >
                            <Checkbox checked={true} className='mr-2' />
                            {code}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                    {statusSearch &&
                      !STATUS_CODE_OPTIONS.includes(statusSearch) &&
                      !selectedStatusCodes.includes(statusSearch) && (
                        <CommandGroup heading='Custom'>
                          <CommandItem
                            onSelect={() => {
                              if (/^\d+$/.test(statusSearch)) {
                                toggleSelection(
                                  selectedStatusCodes,
                                  statusSearch,
                                  onStatusCodesChange
                                );
                                setStatusSearch('');
                              }
                            }}
                          >
                            <Plus className='mr-2 h-4 w-4' />
                            Add &quot;{statusSearch}&quot;
                          </CommandItem>
                        </CommandGroup>
                      )}
                    <CommandEmpty>No results found.</CommandEmpty>
                    <CommandGroup heading='Quick Filters'>
                      <CommandItem
                        onSelect={() => handleQuickStatusCode('4xx')}
                      >
                        <Checkbox
                          checked={STATUS_CODE_OPTIONS.filter((c) =>
                            c.startsWith('4')
                          ).every((c) => selectedStatusCodes.includes(c))}
                          className='mr-2'
                        />
                        4xx Errors
                      </CommandItem>
                      <CommandItem
                        onSelect={() => handleQuickStatusCode('5xx')}
                      >
                        <Checkbox
                          checked={STATUS_CODE_OPTIONS.filter((c) =>
                            c.startsWith('5')
                          ).every((c) => selectedStatusCodes.includes(c))}
                          className='mr-2'
                        />
                        5xx Errors
                      </CommandItem>
                    </CommandGroup>
                    <CommandGroup heading='Common Codes'>
                      {STATUS_CODE_OPTIONS.filter(
                        (code) => !selectedStatusCodes.includes(code)
                      ).map((code) => (
                        <CommandItem
                          key={code}
                          onSelect={() =>
                            toggleSelection(
                              selectedStatusCodes,
                              code,
                              onStatusCodesChange
                            )
                          }
                        >
                          <Checkbox checked={false} className='mr-2' />
                          {code}
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </div>

          <div className='space-y-2'>
            <Label>HTTP Methods</Label>
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant='outline'
                  className='w-full justify-start text-left font-normal'
                >
                  <div className='flex flex-wrap gap-1'>
                    {selectedMethods.length > 0 ? (
                      selectedMethods.map((method) => (
                        <FilterBadge
                          key={method}
                          value={method}
                          onRemove={(val) =>
                            toggleSelection(
                              selectedMethods,
                              val,
                              onMethodsChange
                            )
                          }
                        />
                      ))
                    ) : (
                      <span className='text-muted-foreground'>All methods</span>
                    )}
                  </div>
                </Button>
              </PopoverTrigger>
              <PopoverContent className='w-64 p-0' align='start'>
                <Command>
                  <CommandInput
                    placeholder='Search or add method...'
                    value={methodSearch}
                    onValueChange={setMethodSearch}
                  />
                  <CommandList>
                    {selectedMethods.length > 0 && (
                      <CommandGroup heading='Selected'>
                        {selectedMethods.map((method) => (
                          <CommandItem
                            key={`selected-${method}`}
                            onSelect={() =>
                              toggleSelection(
                                selectedMethods,
                                method,
                                onMethodsChange
                              )
                            }
                          >
                            <Checkbox checked={true} className='mr-2' />
                            {method}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                    {methodSearch &&
                      !METHOD_OPTIONS.includes(methodSearch.toUpperCase()) &&
                      !selectedMethods.includes(methodSearch.toUpperCase()) && (
                        <CommandGroup heading='Custom'>
                          <CommandItem
                            onSelect={() => {
                              toggleSelection(
                                selectedMethods,
                                methodSearch.toUpperCase(),
                                onMethodsChange
                              );
                              setMethodSearch('');
                            }}
                          >
                            <Plus className='mr-2 h-4 w-4' />
                            Add &quot;{methodSearch.toUpperCase()}&quot;
                          </CommandItem>
                        </CommandGroup>
                      )}
                    <CommandEmpty>No results found.</CommandEmpty>
                    <CommandGroup>
                      {METHOD_OPTIONS.filter(
                        (method) => !selectedMethods.includes(method)
                      ).map((method) => (
                        <CommandItem
                          key={method}
                          onSelect={() =>
                            toggleSelection(
                              selectedMethods,
                              method,
                              onMethodsChange
                            )
                          }
                        >
                          <Checkbox checked={false} className='mr-2' />
                          {method}
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </div>

          <div className='space-y-2'>
            <Label>Endpoints</Label>
            <Popover>
              <PopoverTrigger asChild>
                <Button
                  variant='outline'
                  className='w-full justify-start text-left font-normal'
                >
                  <div className='flex flex-wrap gap-1 overflow-hidden'>
                    {selectedEndpoints.length > 0 ? (
                      selectedEndpoints.map((endpoint) => (
                        <FilterBadge
                          key={endpoint}
                          value={endpoint}
                          onRemove={(val) =>
                            toggleSelection(
                              selectedEndpoints,
                              val,
                              onEndpointsChange
                            )
                          }
                        />
                      ))
                    ) : (
                      <span className='text-muted-foreground'>
                        All endpoints
                      </span>
                    )}
                  </div>
                </Button>
              </PopoverTrigger>
              <PopoverContent className='w-80 p-0' align='start'>
                <Command>
                  <CommandInput
                    placeholder='Search or add endpoint pattern...'
                    value={endpointSearch}
                    onValueChange={setEndpointSearch}
                  />
                  <CommandList>
                    {selectedEndpoints.length > 0 && (
                      <CommandGroup heading='Selected'>
                        {selectedEndpoints.map((endpoint) => (
                          <CommandItem
                            key={`selected-${endpoint}`}
                            onSelect={() =>
                              toggleSelection(
                                selectedEndpoints,
                                endpoint,
                                onEndpointsChange
                              )
                            }
                          >
                            <Checkbox checked={true} className='mr-2' />
                            {endpoint}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                    {endpointSearch &&
                      !ENDPOINT_OPTIONS.includes(endpointSearch) &&
                      !selectedEndpoints.includes(endpointSearch) && (
                        <CommandGroup heading='Custom'>
                          <CommandItem
                            onSelect={() => {
                              toggleSelection(
                                selectedEndpoints,
                                endpointSearch,
                                onEndpointsChange
                              );
                              setEndpointSearch('');
                            }}
                          >
                            <Plus className='mr-2 h-4 w-4' />
                            Add &quot;{endpointSearch}&quot;
                          </CommandItem>
                        </CommandGroup>
                      )}
                    <CommandEmpty>No results found.</CommandEmpty>
                    <CommandGroup heading='Common Endpoints'>
                      {ENDPOINT_OPTIONS.filter(
                        (endpoint) => !selectedEndpoints.includes(endpoint)
                      ).map((endpoint) => (
                        <CommandItem
                          key={endpoint}
                          onSelect={() =>
                            toggleSelection(
                              selectedEndpoints,
                              endpoint,
                              onEndpointsChange
                            )
                          }
                        >
                          <Checkbox checked={false} className='mr-2' />
                          {endpoint}
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
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
