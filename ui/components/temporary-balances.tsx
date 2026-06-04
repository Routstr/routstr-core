'use client';

import { useEffect, useState } from 'react';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import {
  RefreshCw,
  AlertCircle,
  Key,
  Clock,
  DollarSign,
  Activity,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react';
import { AdminService } from '@/lib/api/services/admin';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { cn } from '@/lib/utils';
import type { DisplayUnit } from '@/lib/types/units';
import { formatFromMsat } from '@/lib/currency';
import { format } from 'date-fns';

const PAGE_SIZE = 50;

const formatCreatedAt = (createdAt: number | null | undefined) =>
  createdAt ? format(createdAt * 1000, 'yyyy-MM-dd HH:mm:ss') : '—';

export function TemporaryBalances({
  refreshInterval = 10000,
  displayUnit,
  usdPerSat,
}: {
  refreshInterval?: number;
  displayUnit: DisplayUnit;
  usdPerSat: number | null;
}) {
  const [searchTerm, setSearchTerm] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [page, setPage] = useState(0);

  // Debounce the search input so we don't refetch on every keystroke.
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedSearch(searchTerm), 300);
    return () => clearTimeout(handle);
  }, [searchTerm]);

  // Reset to the first page whenever the active search changes.
  useEffect(() => {
    setPage(0);
  }, [debouncedSearch]);

  const searchParam = debouncedSearch || undefined;

  const { data, isLoading, isError, error, isFetching, refetch } = useQuery({
    queryKey: ['temporary-balances', searchParam, page],
    queryFn: async () =>
      AdminService.getTemporaryBalances(
        searchParam,
        PAGE_SIZE,
        page * PAGE_SIZE
      ),
    refetchInterval: refreshInterval,
    placeholderData: keepPreviousData,
  });

  const formatBalance = (msat: number) =>
    formatFromMsat(msat, displayUnit, usdPerSat);

  const rows = data?.balances ?? [];
  const total = data?.total ?? 0;
  const totals = data?.totals ?? {
    total_balance: 0,
    total_spent: 0,
    total_requests: 0,
  };
  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <Card>
      <CardHeader className='pb-4'>
        <div className='flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between'>
          <div className='space-y-1.5'>
            <CardTitle>API Keys</CardTitle>
            <CardDescription className='max-w-2xl'>
              API keys with their current balances and usage statistics, newest
              first
            </CardDescription>
          </div>

          <div className='flex w-full gap-2 sm:w-auto sm:pt-0.5'>
            <div className='min-w-0 flex-1 sm:w-72 sm:flex-none'>
              <Input
                type='text'
                placeholder='Search by key or address...'
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                name='temporary_balance_search'
                autoComplete='off'
              />
            </div>
            <Button
              variant='ghost'
              size='icon'
              onClick={() => refetch()}
              disabled={isLoading || isFetching}
            >
              <RefreshCw
                className={cn(
                  'h-4 w-4',
                  (isFetching || isLoading) && 'animate-spin'
                )}
              />
              <span className='sr-only'>Refresh API keys</span>
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className='space-y-4'>
            <div className='grid gap-3 md:grid-cols-3'>
              {Array.from({ length: 3 }).map((_, index) => (
                <Card key={`temp-stat-skeleton-${index}`}>
                  <CardHeader className='space-y-2 pb-1'>
                    <Skeleton className='h-3.5 w-24' />
                    <Skeleton className='h-3 w-8' />
                  </CardHeader>
                  <CardContent className='pt-0'>
                    <Skeleton className='h-7 w-24' />
                  </CardContent>
                </Card>
              ))}
            </div>
            <div className='space-y-2'>
              {Array.from({ length: 6 }).map((_, index) => (
                <Skeleton
                  key={`temp-row-skeleton-${index}`}
                  className='h-11 w-full'
                />
              ))}
            </div>
          </div>
        ) : isError ? (
          <Alert variant='destructive'>
            <AlertCircle className='h-5 w-5' />
            <AlertDescription>
              Error loading API keys: {(error as Error).message}
            </AlertDescription>
          </Alert>
        ) : (
          <div className='space-y-6'>
            <div className='grid gap-3 md:grid-cols-3'>
              <Card>
                <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
                  <CardTitle className='text-muted-foreground text-sm font-medium'>
                    Total Balance
                  </CardTitle>
                  <span className='inline-flex size-8 items-center justify-center'>
                    <DollarSign className='size-4 text-green-600 dark:text-green-300' />
                  </span>
                </CardHeader>
                <CardContent className='pt-0'>
                  <p className='text-2xl font-semibold tracking-tight tabular-nums'>
                    {formatBalance(totals.total_balance)}
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
                  <CardTitle className='text-muted-foreground text-sm font-medium'>
                    Total Spent
                  </CardTitle>
                  <span className='inline-flex size-8 items-center justify-center'>
                    <Activity className='size-4 text-blue-600 dark:text-blue-300' />
                  </span>
                </CardHeader>
                <CardContent className='pt-0'>
                  <p className='text-2xl font-semibold tracking-tight tabular-nums'>
                    {formatBalance(totals.total_spent)}
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardHeader className='flex flex-row items-center justify-between space-y-0 pb-2'>
                  <CardTitle className='text-muted-foreground text-sm font-medium'>
                    Total Requests
                  </CardTitle>
                  <span className='inline-flex size-8 items-center justify-center'>
                    <Key className='size-4 text-purple-600 dark:text-purple-300' />
                  </span>
                </CardHeader>
                <CardContent className='pt-0'>
                  <p className='text-2xl font-semibold tracking-tight tabular-nums'>
                    {totals.total_requests.toLocaleString()}
                  </p>
                </CardContent>
              </Card>
            </div>

            {totalPages > 1 && (
              <div className='flex flex-col gap-2 border-b pb-3 sm:flex-row sm:items-center sm:justify-between'>
                <span className='text-muted-foreground text-xs sm:text-sm'>
                  {page * PAGE_SIZE + 1}–
                  {Math.min((page + 1) * PAGE_SIZE, total)} of {total}
                </span>
                <div className='flex items-center gap-2'>
                  <Button
                    variant='outline'
                    size='sm'
                    disabled={page === 0}
                    onClick={() => setPage(page - 1)}
                  >
                    <ChevronLeft className='h-4 w-4' />
                    <span className='hidden sm:inline'>Previous</span>
                  </Button>
                  <span className='text-xs sm:text-sm'>
                    {page + 1} / {totalPages}
                  </span>
                  <Button
                    variant='outline'
                    size='sm'
                    disabled={page >= totalPages - 1}
                    onClick={() => setPage(page + 1)}
                  >
                    <span className='hidden sm:inline'>Next</span>
                    <ChevronRight className='h-4 w-4' />
                  </Button>
                </div>
              </div>
            )}

            {rows.length > 0 ? (
              <>
                <div className='hidden md:block'>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Hashed Key</TableHead>
                        <TableHead className='text-right'>Balance</TableHead>
                        <TableHead className='text-right'>
                          Total Spent
                        </TableHead>
                        <TableHead className='text-right'>
                          Total Requests
                        </TableHead>
                        <TableHead>Created</TableHead>
                        <TableHead>Refund Address</TableHead>
                        <TableHead className='text-right'>
                          Expiry Time
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {rows.map((balance, index) => {
                        const isChild = Boolean(balance.parent_key_hash);
                        return (
                          <TableRow
                            key={`${balance.hashed_key}-${balance.parent_key_hash ?? 'root'}-${index}`}
                            className={cn(
                              balance.balance === 0 && !isChild && 'opacity-60',
                              isChild && 'bg-muted/30'
                            )}
                          >
                            <TableCell className='max-w-[16rem] font-mono text-xs break-all whitespace-normal'>
                              <div className='flex items-center gap-2'>
                                {isChild && (
                                  <Badge
                                    variant='outline'
                                    className='h-4 px-1 text-[10px] uppercase'
                                  >
                                    Child
                                  </Badge>
                                )}
                                <span>{balance.hashed_key}</span>
                              </div>
                            </TableCell>
                            <TableCell className='text-right font-mono'>
                              {isChild ? (
                                <span className='text-muted-foreground italic'>
                                  (Parent)
                                </span>
                              ) : (
                                formatBalance(balance.balance)
                              )}
                            </TableCell>
                            <TableCell className='text-right font-mono'>
                              {formatBalance(balance.total_spent)}
                            </TableCell>
                            <TableCell className='text-right font-mono'>
                              {balance.total_requests.toLocaleString()}
                            </TableCell>
                            <TableCell className='font-mono text-xs whitespace-nowrap'>
                              {formatCreatedAt(balance.created_at)}
                            </TableCell>
                            <TableCell className='max-w-[14rem] font-mono text-xs break-all whitespace-normal'>
                              {balance.refund_address || '-'}
                            </TableCell>
                            <TableCell className='text-right font-mono text-xs'>
                              {balance.key_expiry_time ? (
                                <div className='inline-flex items-center justify-end gap-1'>
                                  <Clock className='h-3 w-3' />
                                  <span>
                                    {new Date(
                                      balance.key_expiry_time * 1000
                                    ).toLocaleDateString()}
                                  </span>
                                </div>
                              ) : (
                                '-'
                              )}
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </div>

                <div className='space-y-2 md:hidden'>
                  {rows.map((balance, index) => {
                    const isChild = Boolean(balance.parent_key_hash);
                    return (
                      <Card
                        key={`${balance.hashed_key}-${balance.parent_key_hash ?? 'root'}-mobile-${index}`}
                        className={cn(
                          balance.balance === 0 && !isChild && 'opacity-80',
                          isChild && 'bg-muted/30'
                        )}
                      >
                        <CardHeader className='p-4 pb-2'>
                          <div className='flex items-center justify-between gap-2'>
                            <CardDescription className='font-mono text-xs break-all'>
                              {balance.hashed_key}
                            </CardDescription>
                            {isChild && (
                              <Badge
                                variant='outline'
                                className='h-4 px-1.5 text-[10px] uppercase'
                              >
                                Child
                              </Badge>
                            )}
                          </div>
                        </CardHeader>
                        <CardContent className='grid grid-cols-2 gap-3 p-4 pt-0'>
                          <div>
                            <p className='text-muted-foreground text-xs'>
                              Balance
                            </p>
                            <p className='font-mono text-sm'>
                              {isChild
                                ? '(Uses Parent)'
                                : formatBalance(balance.balance)}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-xs'>
                              Spent
                            </p>
                            <p className='font-mono text-sm'>
                              {formatBalance(balance.total_spent)}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-xs'>
                              Requests
                            </p>
                            <p className='font-mono text-sm'>
                              {balance.total_requests.toLocaleString()}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-xs'>
                              Created
                            </p>
                            <p className='font-mono text-xs'>
                              {formatCreatedAt(balance.created_at)}
                            </p>
                          </div>
                          <div>
                            <p className='text-muted-foreground text-xs'>
                              Expires
                            </p>
                            <p className='font-mono text-xs'>
                              {balance.key_expiry_time ? (
                                <span className='inline-flex items-center gap-1'>
                                  <Clock className='h-3 w-3' />
                                  {new Date(
                                    balance.key_expiry_time * 1000
                                  ).toLocaleDateString()}
                                </span>
                              ) : (
                                '-'
                              )}
                            </p>
                          </div>
                          {balance.refund_address && (
                            <div className='col-span-2'>
                              <p className='text-muted-foreground text-xs'>
                                Refund Address
                              </p>
                              <p className='font-mono text-xs break-all'>
                                {balance.refund_address}
                              </p>
                            </div>
                          )}
                        </CardContent>
                      </Card>
                    );
                  })}
                </div>
              </>
            ) : (
              <Empty className='py-8'>
                <EmptyHeader>
                  <EmptyMedia variant='icon'>
                    {debouncedSearch ? (
                      <AlertCircle className='h-4 w-4' />
                    ) : (
                      <Key className='h-4 w-4' />
                    )}
                  </EmptyMedia>
                  <EmptyTitle>
                    {debouncedSearch
                      ? 'No API keys match your search'
                      : 'No API keys found'}
                  </EmptyTitle>
                  <EmptyDescription>
                    {debouncedSearch
                      ? 'Try a different key hash or refund address.'
                      : 'API keys will appear here once they are created.'}
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}

            {total > 0 && (
              <p className='text-muted-foreground text-xs'>
                Showing {rows.length} of {total} API keys
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
