'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AppSidebar } from '@/components/app-sidebar';
import { SiteHeader } from '@/components/site-header';
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
import { FileText, RefreshCw } from 'lucide-react';
import { apiClient } from '@/lib/api/client';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { LogEntry, LogsResponse } from './types';
import { LogFilters } from './log-filters';
import { LogEntryCard } from './log-entry-card';
import { LogDetailsDialog } from './log-details-dialog';

export default function LogsPage() {
  const [selectedDate, setSelectedDate] = useState<string>('all');
  const [selectedLevel, setSelectedLevel] = useState<string>('all');
  const [requestId, setRequestId] = useState<string>('');
  const [searchText, setSearchText] = useState<string>('');
  const [limit, setLimit] = useState<number>(100);
  const [selectedLog, setSelectedLog] = useState<LogEntry | null>(null);
  const [isDialogOpen, setIsDialogOpen] = useState<boolean>(false);

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
      limit,
    ],
    queryFn: () =>
      apiClient.get<LogsResponse>('/admin/api/logs', {
        date: selectedDate === 'all' ? undefined : selectedDate,
        level: selectedLevel === 'all' ? undefined : selectedLevel,
        request_id: requestId || undefined,
        search: searchText || undefined,
        limit: limit,
      }),
    refetchInterval: 30000,
  });

  const handleClearFilters = () => {
    setSelectedDate('all');
    setSelectedLevel('all');
    setRequestId('');
    setSearchText('');
    setLimit(100);
  };

  const handleLogClick = (entry: LogEntry) => {
    setSelectedLog(entry);
    setIsDialogOpen(true);
  };

  return (
    <SidebarProvider>
      <AppSidebar variant='inset' />
      <SidebarInset className='overflow-x-hidden p-0'>
        <SiteHeader />
        <div className='container max-w-6xl overflow-x-hidden px-3 py-4 sm:px-4 sm:py-8 md:px-6 lg:px-8'>
          <div className='mb-6 flex flex-col gap-3 sm:mb-8 sm:gap-4 lg:flex-row lg:items-start lg:justify-between'>
            <div>
              <h1 className='flex items-center gap-2 text-2xl font-bold tracking-tight sm:text-3xl'>
                <FileText className='h-6 w-6 sm:h-8 sm:w-8' />
                System Logs
              </h1>
              <p className='text-muted-foreground mt-1 text-sm sm:mt-2 sm:text-base'>
                View and filter application logs
              </p>
            </div>
            <Button
              onClick={() => refetchLogs()}
              variant='outline'
              size='sm'
              className='self-start'
            >
              <RefreshCw className='mr-2 h-4 w-4' />
              Refresh
            </Button>
          </div>

          <LogFilters
            selectedDate={selectedDate}
            selectedLevel={selectedLevel}
            requestId={requestId}
            searchText={searchText}
            limit={limit}
            onDateChange={setSelectedDate}
            onLevelChange={setSelectedLevel}
            onRequestIdChange={setRequestId}
            onSearchTextChange={setSearchText}
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
              {(selectedDate !== 'all' ||
                selectedLevel !== 'all' ||
                requestId ||
                searchText) && (
                <CardDescription className='text-xs sm:text-sm'>
                  Showing logs
                  {selectedDate !== 'all' && ` for ${selectedDate}`}
                  {selectedLevel !== 'all' && ` with level ${selectedLevel}`}
                  {requestId && ` with request ID ${requestId}`}
                  {searchText && ` matching "${searchText}"`}
                </CardDescription>
              )}
            </CardHeader>
            <CardContent className='overflow-hidden p-3 sm:p-6'>
              {isLoading ? (
                <div className='flex items-center justify-center py-8'>
                  <RefreshCw className='h-6 w-6 animate-spin' />
                  <span className='ml-2 text-sm sm:text-base'>
                    Loading logs...
                  </span>
                </div>
              ) : logsData?.logs && logsData.logs.length > 0 ? (
                <>
                  <ScrollArea className='h-[500px] w-full sm:h-[600px]'>
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
                </>
              ) : (
                <div className='text-muted-foreground py-8 text-center'>
                  <FileText className='mx-auto mb-4 h-10 w-10 opacity-50 sm:h-12 sm:w-12' />
                  <p className='text-sm sm:text-base'>No log entries found</p>
                  <p className='text-xs sm:text-sm'>
                    Try adjusting your filters or check back later
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          <LogDetailsDialog
            log={selectedLog}
            isOpen={isDialogOpen}
            onClose={() => setIsDialogOpen(false)}
          />
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
