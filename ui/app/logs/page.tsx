'use client';

import { useState, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { ScrollArea } from '@/components/ui/scroll-area';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';
import { FileText, RefreshCw } from 'lucide-react';
import { apiClient } from '@/lib/api/client';
import { AppPageShell } from '@/components/app-page-shell';
import { PageHeader } from '@/components/page-header';
import { LogEntry, LogsResponse } from './types';
import { LogFilters } from './log-filters';
import { LogEntryCard } from './log-entry-card';
import { LogDetailsDialog } from './log-details-dialog';

const STORAGE_KEY = 'routstr-log-filters';

export default function LogsPage() {
  const [selectedDate, setSelectedDate] = useState<string>('all');
  const [selectedLevel, setSelectedLevel] = useState<string>('all');
  const [requestId, setRequestId] = useState<string>('');
  const [searchText, setSearchText] = useState<string>('');
  const [selectedStatusCodes, setSelectedStatusCodes] = useState<string[]>([]);
  const [selectedMethods, setSelectedMethods] = useState<string[]>([]);
  const [selectedEndpoints, setSelectedEndpoints] = useState<string[]>([]);
  const [limit, setLimit] = useState<number>(100);
  const [selectedLog, setSelectedLog] = useState<LogEntry | null>(null);
  const [isDialogOpen, setIsDialogOpen] = useState<boolean>(false);

  // Load filters from localStorage on mount
  useEffect(() => {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      try {
        const parsed = JSON.parse(saved);
        if (parsed.selectedDate) setSelectedDate(parsed.selectedDate);
        if (parsed.selectedLevel) setSelectedLevel(parsed.selectedLevel);
        if (parsed.requestId) setRequestId(parsed.requestId);
        if (parsed.searchText) setSearchText(parsed.searchText);
        if (parsed.selectedStatusCodes)
          setSelectedStatusCodes(parsed.selectedStatusCodes);
        if (parsed.selectedMethods) setSelectedMethods(parsed.selectedMethods);
        if (parsed.selectedEndpoints)
          setSelectedEndpoints(parsed.selectedEndpoints);
        if (parsed.limit) setLimit(parsed.limit);
      } catch (e) {
        console.error('Failed to load filters from localStorage', e);
      }
    }
  }, []);

  // Save filters to localStorage whenever they change
  useEffect(() => {
    const filters = {
      selectedDate,
      selectedLevel,
      requestId,
      searchText,
      selectedStatusCodes,
      selectedMethods,
      selectedEndpoints,
      limit,
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(filters));
  }, [
    selectedDate,
    selectedLevel,
    requestId,
    searchText,
    selectedStatusCodes,
    selectedMethods,
    selectedEndpoints,
    limit,
  ]);

  const {
    data: logsData,
    refetch: refetchLogs,
    isLoading,
  } = useQuery({
    queryKey: [
      'logs',
      selectedDate,
      selectedLevel,
      requestId,
      searchText,
      selectedStatusCodes,
      selectedMethods,
      selectedEndpoints,
      limit,
    ],
    queryFn: () =>
      apiClient.get<LogsResponse>('/admin/api/logs', {
        date: selectedDate === 'all' ? undefined : selectedDate,
        level: selectedLevel === 'all' ? undefined : selectedLevel,
        request_id: requestId || undefined,
        search: searchText || undefined,
        status_codes:
          selectedStatusCodes.length > 0
            ? selectedStatusCodes.join(',')
            : undefined,
        methods:
          selectedMethods.length > 0 ? selectedMethods.join(',') : undefined,
        endpoints:
          selectedEndpoints.length > 0
            ? selectedEndpoints.join(',')
            : undefined,
        limit: limit,
      }),
    refetchInterval: 30000,
  });

  const handleClearFilters = () => {
    setSelectedDate('all');
    setSelectedLevel('all');
    setRequestId('');
    setSearchText('');
    setSelectedStatusCodes([]);
    setSelectedMethods([]);
    setSelectedEndpoints([]);
    setLimit(100);
  };

  const handleLogClick = (entry: LogEntry) => {
    setSelectedLog(entry);
    setIsDialogOpen(true);
  };

  const hasActiveFilters =
    selectedDate !== 'all' ||
    selectedLevel !== 'all' ||
    Boolean(requestId) ||
    Boolean(searchText) ||
    selectedStatusCodes.length > 0 ||
    selectedMethods.length > 0 ||
    selectedEndpoints.length > 0;

  const activeFilterDescription = [
    selectedDate !== 'all' ? `date ${selectedDate}` : null,
    selectedLevel !== 'all' ? `level ${selectedLevel}` : null,
    requestId ? `request ID ${requestId}` : null,
    searchText ? `text "${searchText}"` : null,
    selectedStatusCodes.length > 0
      ? `status ${selectedStatusCodes.join(', ')}`
      : null,
    selectedMethods.length > 0 ? `method ${selectedMethods.join(', ')}` : null,
    selectedEndpoints.length > 0
      ? `endpoint ${selectedEndpoints.join(', ')}`
      : null,
  ]
    .filter(Boolean)
    .join(' • ');

  return (
    <AppPageShell contentClassName='mx-auto w-full max-w-5xl overflow-x-hidden'>
      <div className='space-y-6'>
        <PageHeader
          title='System Logs'
          description='View and filter application logs.'
          actions={
            <Button
              onClick={() => refetchLogs()}
              variant='outline'
              size='sm'
              className='w-full sm:w-auto'
            >
              <RefreshCw className='mr-2 h-4 w-4' />
              Refresh
            </Button>
          }
        />

        <LogFilters
          selectedDate={selectedDate}
          selectedLevel={selectedLevel}
          requestId={requestId}
          searchText={searchText}
          selectedStatusCodes={selectedStatusCodes}
          selectedMethods={selectedMethods}
          selectedEndpoints={selectedEndpoints}
          limit={limit}
          onDateChange={setSelectedDate}
          onLevelChange={setSelectedLevel}
          onRequestIdChange={setRequestId}
          onSearchTextChange={setSearchText}
          onStatusCodesChange={setSelectedStatusCodes}
          onMethodsChange={setSelectedMethods}
          onEndpointsChange={setSelectedEndpoints}
          onLimitChange={setLimit}
          onClearFilters={handleClearFilters}
        />

        <Card>
          <CardHeader>
            <CardTitle className='flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between'>
              <span className='text-lg sm:text-xl'>Log Entries</span>
              {logsData && (
                <Badge variant='secondary' className='text-xs sm:text-sm'>
                  {logsData.logs.length} entries
                </Badge>
              )}
            </CardTitle>
            {hasActiveFilters && (
              <CardDescription className='text-xs sm:text-sm'>
                Showing logs filtered by {activeFilterDescription}
              </CardDescription>
            )}
          </CardHeader>
          <CardContent className='overflow-hidden p-3 sm:p-6'>
            {isLoading ? (
              <div className='space-y-2'>
                {Array.from({ length: 8 }).map((_, index) => (
                  <Skeleton key={`logs-loading-${index}`} className='h-16 w-full rounded-lg' />
                ))}
              </div>
            ) : logsData?.logs && logsData.logs.length > 0 ? (
              <ScrollArea className='h-[55svh] min-h-[420px] w-full sm:h-[600px]'>
                <div className='space-y-2 pr-3'>
                  {logsData.logs.map((entry, index) => (
                    <LogEntryCard
                      key={`${entry.request_id}-${entry.asctime}-${entry.lineno}-${index}`}
                      entry={entry}
                      onClick={handleLogClick}
                    />
                  ))}
                </div>
              </ScrollArea>
            ) : (
              <Empty className='py-8'>
                <EmptyHeader>
                  <EmptyMedia variant='icon'>
                    <FileText className='h-4 w-4' />
                  </EmptyMedia>
                  <EmptyTitle>No log entries found</EmptyTitle>
                  <EmptyDescription>
                    Try adjusting your filters or check back later.
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
          </CardContent>
        </Card>

        <LogDetailsDialog
          log={selectedLog}
          isOpen={isDialogOpen}
          onClose={() => setIsDialogOpen(false)}
        />
      </div>
    </AppPageShell>
  );
}
